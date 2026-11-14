"""Adaptador BLE con el sensor fijo (bleak).

Secuencia al conectar: notify en Lectura, umbral y hora. El firmware
transmite apenas recibe la hora, así que la notificación tiene que estar
activa antes.

El callback de notificación solo decodifica y encola: el sensor vacía
su buffer a una lectura cada 15 ms y bloquear el callback haría perder
notificaciones.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional, Protocol

from monitoring.infrastructure import ble_protocol as proto
from shared.clock import Clock

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SessionStarted:
    device_id: str


@dataclass(frozen=True)
class ReadingReceived:
    device_id: str
    raw: proto.RawReading


class ConnectivityListener(Protocol):
    def device_connected(self, device_id: str) -> None: ...

    def device_disconnected(self, device_id: str) -> None: ...


def _default_find(name: str, timeout: float):
    from bleak import BleakScanner  # diferido: los tests no necesitan la pila BLE

    service = proto.SERVICE_UUID.lower()

    def matches(device, adv) -> bool:
        if (adv.local_name or device.name) != name:
            return False
        uuids = [u.lower() for u in (adv.service_uuids or [])]
        # El nombre puede llegar sin los UUID: entonces alcanza con el nombre.
        return not uuids or service in uuids

    return BleakScanner.find_device_by_filter(matches, timeout=timeout)


def _default_client_factory(device, disconnected_callback):
    from bleak import BleakClient

    return BleakClient(device, disconnected_callback=disconnected_callback)


class BleSensorClient:
    """Mantiene la conexión con el sensor y encola sus lecturas."""

    def __init__(
        self,
        device_name: str,
        queue: asyncio.Queue,
        threshold_for: Callable[[str], float],
        connectivity: ConnectivityListener,
        clock: Clock,
        *,
        find_device: Callable[[str, float], Awaitable[object]] = _default_find,
        client_factory: Callable[..., object] = _default_client_factory,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        scan_timeout_s: float = 10.0,
        min_backoff_s: float = 1.0,
        max_backoff_s: float = 30.0,
        device_id: str = "",
    ) -> None:
        self.device_name = device_name
        # deviceId de las lecturas y del estado; vacío = el nombre BLE.
        self.device_id = device_id or device_name
        self._queue = queue
        self._threshold_for = threshold_for
        self._connectivity = connectivity
        self._clock = clock
        self._find_device = find_device
        self._client_factory = client_factory
        self._sleep = sleep
        self._scan_timeout_s = scan_timeout_s
        self._min_backoff_s = min_backoff_s
        self._max_backoff_s = max_backoff_s
        self._backoff_s = min_backoff_s
        self._client = None
        self._connected = False
        self._stopping = False
        self._disconnected: Optional[asyncio.Event] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    @property
    def is_connected(self) -> bool:
        return self._connected

    # ---------- Ciclo de vida ----------

    async def run(self) -> None:
        """Bucle de conexión hasta `stop()`."""
        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        if self._stopping:
            return
        while not self._stopping:
            device = await self._scan()
            if self._stopping:
                break
            if device is None:
                await self._wait_backoff("no encontrado")
                continue
            if await self._session(device):
                self._backoff_s = self._min_backoff_s
                if not self._stopping:
                    await self._until_stopped(self._sleep(self._min_backoff_s))
            else:
                await self._wait_backoff("falló la conexión")

    async def stop(self) -> None:
        self._stopping = True
        if self._stop_event is not None:
            self._stop_event.set()
        if self._disconnected is not None:
            self._disconnected.set()
        client = self._client
        if client is not None:
            try:
                await client.disconnect()
            except Exception as exc:
                log.debug("Error al desconectar %s: %s", self.device_name, exc)

    async def _until_stopped(self, awaitable):
        """Espera `awaitable`; si llega `stop()`, lo cancela y devuelve None."""
        task = asyncio.ensure_future(awaitable)
        stopper = asyncio.ensure_future(self._stop_event.wait())
        try:
            await asyncio.wait({task, stopper}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stopper.cancel()
        if task.done():
            return task.result()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return None

    async def _scan(self):
        log.info("Buscando %s por BLE...", self.device_name)
        try:
            return await self._until_stopped(self._find_device(self.device_name, self._scan_timeout_s))
        except Exception as exc:  # Bluetooth apagado, permisos...
            log.warning("Error al escanear BLE buscando %s: %s", self.device_name, exc)
            return None

    async def _wait_backoff(self, reason: str) -> None:
        if self._stopping:
            return
        log.info("%s %s: reintento en %.0f s", self.device_name, reason, self._backoff_s)
        await self._until_stopped(self._sleep(self._backoff_s))
        self._backoff_s = min(self._backoff_s * 2, self._max_backoff_s)

    async def _session(self, device) -> bool:
        """Conecta, ejecuta la secuencia y espera la desconexión. True si la secuencia se completó."""
        self._disconnected = asyncio.Event()
        client = self._client_factory(device, self._on_disconnect)
        self._client = client
        try:
            await client.connect()
            await client.start_notify(proto.READING_UUID, self._on_notify)
            threshold = self._threshold_for(self.device_id)
            await client.write_gatt_char(
                proto.THRESHOLD_UUID, proto.encode_threshold(threshold), response=True
            )
            # Antes de la hora, para que quede delante de las lecturas de esta sesión.
            self._queue.put_nowait(SessionStarted(self.device_id))
            epoch = int(self._clock.now().timestamp())
            await client.write_gatt_char(proto.TIME_UUID, proto.encode_time(epoch), response=True)
        except Exception as exc:
            log.warning("No se pudo iniciar la sesión con %s: %s", self.device_name, exc)
            await self._safe_disconnect(client)
            self._client = None
            return False

        self._connected = True
        log.info("Conectado a %s (umbral %.1f µT, hora %s)", self.device_name, threshold, epoch)
        self._connectivity.device_connected(self.device_id)
        try:
            await self._disconnected.wait()
        finally:
            self._connected = False
            self._client = None
            self._connectivity.device_disconnected(self.device_id)
            log.info("Desconectado de %s", self.device_name)
        return True

    async def _safe_disconnect(self, client) -> None:
        try:
            await client.disconnect()
        except Exception:
            pass

    # ---------- Callbacks de bleak ----------

    def _on_notify(self, _characteristic, data) -> None:
        try:
            raw = proto.decode_reading(data)
        except proto.BleProtocolError as exc:
            log.warning("Notificación inválida de %s: %s", self.device_name, exc)
            return
        self._enqueue(ReadingReceived(self.device_id, raw))

    def _enqueue(self, item) -> None:
        # asyncio.Queue no es thread-safe: si bleak llama desde otro hilo, se pasa por el loop.
        loop = self._loop
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if loop is not None and running is not loop:
            loop.call_soon_threadsafe(self._queue.put_nowait, item)
        else:
            self._queue.put_nowait(item)

    def _on_disconnect(self, _client) -> None:
        # Según el backend, bleak puede llamar desde otro hilo.
        event, loop = self._disconnected, self._loop
        if event is None:
            return
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(event.set)
        else:
            event.set()
