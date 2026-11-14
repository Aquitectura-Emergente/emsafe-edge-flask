"""Orquestación del Edge: BLE, ingesta, enchufe, publicador y limpieza en un loop asyncio.

`build_runner(settings)` arma las piezas reales; los tests pasan dobles a `EdgeRunner`.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Dict, List, Optional, Sequence

from monitoring.application.control_plug import ControlPlugUseCase
from monitoring.application.ingest_reading import IngestReadingUseCase, IngestResult
from monitoring.application.publish_outbox import PublishOutboxUseCase
from monitoring.application.purge_outbox import PurgeOutboxUseCase
from monitoring.application.track_connectivity import TrackConnectivityUseCase
from monitoring.domain.entities import Reading
from monitoring.infrastructure.ble_sensor_client import ReadingReceived, SessionStarted

log = logging.getLogger(__name__)


class EdgeRunner:
    def __init__(
        self,
        *,
        queue: asyncio.Queue,
        ingest: IngestReadingUseCase,
        publish: PublishOutboxUseCase,
        purge: PurgeOutboxUseCase,
        connectivity: TrackConnectivityUseCase,
        mqtt,
        ble_clients: Sequence,
        control: Optional[ControlPlugUseCase] = None,
        outbox=None,
        thresholds_for: Optional[Callable[[str], Any]] = None,
        publish_interval_s: float = 1.0,
        purge_interval_s: float = 3600.0,
        shutdown_timeout_s: float = 5.0,
    ) -> None:
        self._queue = queue
        self._ingest = ingest
        self._publish = publish
        self._purge = purge
        self._connectivity = connectivity
        self._mqtt = mqtt
        self._ble_clients = list(ble_clients)
        self._control = control
        self._outbox = outbox
        self._thresholds_for = thresholds_for
        self._publish_interval_s = publish_interval_s
        self._purge_interval_s = purge_interval_s
        self._shutdown_timeout_s = shutdown_timeout_s
        self._stop: Optional[asyncio.Event] = None
        self._wake: Optional[asyncio.Event] = None
        self._plug_queue: Optional[asyncio.Queue] = None
        self._background: set = set()

    def stop(self) -> None:
        """Pide un apagado ordenado. Se llama desde el loop."""
        if self._stop is not None:
            self._stop.set()

    def reading_stored(self, reading: Reading) -> None:
        """Lectura nueva guardada: las de tiempo real pasan a las reglas del enchufe."""
        if self._control is None or reading.is_deferred or self._plug_queue is None:
            return
        self._plug_queue.put_nowait(reading)

    def health(self) -> Dict[str, Any]:
        """Estado para `/health`. Se lee desde el hilo de Flask."""
        data: Dict[str, Any] = {
            "ble": [
                {"device": c.device_name, "connected": bool(getattr(c, "is_connected", False))}
                for c in self._ble_clients
            ],
            "mqtt": {"connected": bool(getattr(self._mqtt, "is_connected", False))},
            "actuator": self._control.status() if self._control else {"enabled": False},
        }
        if self._outbox is not None:
            try:
                data["outbox"] = {"pending": self._outbox.pending_count()}
            except Exception as exc:
                data["outbox"] = {"error": str(exc)}
        if self._thresholds_for is not None and self._ble_clients:
            device_id = getattr(self._ble_clients[0], "device_id", self._ble_clients[0].device_name)
            t = self._thresholds_for(device_id)
            data["thresholds"] = {"cautionUT": t.caution_ut, "dangerUT": t.danger_ut}
            if self._control is not None:
                data["thresholds"]["restoreUT"] = round(self._control.restore_threshold(device_id), 3)
        return data

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._plug_queue = asyncio.Queue()
        self._mqtt.start(
            loop, self._on_broker_connected, self._on_command if self._control else None
        )

        ble_tasks = [
            asyncio.create_task(c.run(), name=f"ble:{c.device_name}") for c in self._ble_clients
        ]
        worker_tasks = [
            asyncio.create_task(self._consume(), name="ingesta"),
            asyncio.create_task(self._publish_loop(), name="publicador"),
            asyncio.create_task(self._purge_loop(), name="limpieza"),
        ]
        if self._control is not None:
            worker_tasks.append(asyncio.create_task(self._plug_loop(), name="enchufe"))
        log.info(
            "Edge hogar en marcha: sensor %s", ", ".join(c.device_name for c in self._ble_clients)
        )
        try:
            await self._stop.wait()
        finally:
            await self._shutdown(ble_tasks, worker_tasks)

    # ---------- Tareas ----------

    async def _consume(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                if isinstance(item, SessionStarted):
                    self._ingest.session_started(item.device_id)
                elif isinstance(item, ReadingReceived):
                    raw = item.raw
                    result = self._ingest.ingest(item.device_id, raw.seq, raw.ts, raw.uT, raw.deferred)
                    # Las de tiempo real salen enseguida; las diferidas esperan
                    # al próximo ciclo para formar lotes.
                    if result is IngestResult.STORED and raw.deferred != 1:
                        self._wake.set()
            except Exception:
                # Una lectura con error no detiene la ingesta.
                log.exception("Error al ingerir %r", item)
            finally:
                self._queue.task_done()

    async def _plug_loop(self) -> None:
        # El HTTP al actuador corre en un hilo para no frenar la ingesta. Si se
        # acumulan lecturas mientras tanto, se decide con todas juntas.
        while True:
            batch = [await self._plug_queue.get()]
            while not self._plug_queue.empty():
                batch.append(self._plug_queue.get_nowait())
            try:
                await asyncio.to_thread(self._control.evaluate_latest, batch)
            except Exception:
                log.exception("Error en las reglas del enchufe")

    async def _publish_loop(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._publish_interval_s)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            if not self._mqtt.is_connected:
                continue
            try:
                await self._publish.run_once()
            except Exception:
                log.exception("Error en el ciclo de publicación")

    async def _purge_loop(self) -> None:
        while True:
            try:
                self._purge.run_once()
            except Exception:
                log.exception("Error en la limpieza del outbox")
            await asyncio.sleep(self._purge_interval_s)

    async def _on_command(self, raw: bytes) -> None:
        try:
            await asyncio.to_thread(self._control.handle_command, raw)
        except Exception:
            log.exception("Error al procesar la orden del enchufe")

    def _on_broker_connected(self) -> None:
        # paho lo invoca vía call_soon_threadsafe: corre en el loop.
        self._connectivity.republish_all()
        if self._control is not None:
            # En un hilo: si hay una orden en curso, espera al actuador.
            task = asyncio.ensure_future(asyncio.to_thread(self._control.publish_state))
            self._background.add(task)
            task.add_done_callback(self._background.discard)
        if self._wake is not None:
            self._wake.set()

    # ---------- Apagado ----------

    async def _shutdown(self, ble_tasks: List[asyncio.Task], worker_tasks: List[asyncio.Task]) -> None:
        log.info("Apagando el Edge...")
        # BLE primero, para que el OFFLINE del sensor salga con MQTT arriba.
        for client in self._ble_clients:
            try:
                await client.stop()
            except Exception:
                log.exception("Error al detener %s", getattr(client, "device_name", client))
        if ble_tasks:
            done, pending = await asyncio.wait(ble_tasks, timeout=self._shutdown_timeout_s)
            for task in pending:
                task.cancel()
        # Lo no publicado queda en el outbox.
        for task in worker_tasks:
            task.cancel()
        await asyncio.gather(*ble_tasks, *worker_tasks, return_exceptions=True)
        try:
            self._mqtt.stop()
        except Exception:
            log.exception("Error al detener MQTT")
        log.info("Edge detenido")


def build_runner(settings) -> EdgeRunner:
    """Arma el Edge con los adaptadores reales."""
    from monitoring.domain.services import BatchPolicy, ReadingFactory
    from monitoring.domain.value_objects import AlertThresholds
    from monitoring.infrastructure.ble_sensor_client import BleSensorClient
    from monitoring.infrastructure.http_actuator import HttpActuatorGateway
    from monitoring.infrastructure.mqtt_client import MqttGateway
    from monitoring.infrastructure.mqtt_payloads import parse_plug_command
    from monitoring.infrastructure.sqlite_outbox import SqliteReadingOutbox
    from shared.clock import SystemClock
    from shared.database import init_db

    init_db(settings.db_path)
    clock = SystemClock()
    outbox = SqliteReadingOutbox()
    queue: asyncio.Queue = asyncio.Queue()
    mqtt = MqttGateway(settings, clock, settings.device_ids)
    connectivity = TrackConnectivityUseCase(mqtt, clock)
    thresholds = AlertThresholds(settings.caution_ut, settings.danger_ut)

    def thresholds_for(_device_id: str) -> AlertThresholds:
        return thresholds

    # El sensor recibe el umbral de DANGER: su alarma local coincide con el corte.
    def sensor_threshold(_device_id: str) -> float:
        return thresholds.danger_ut

    if not settings.actuator_url:
        log.warning("EMSAFE_ACTUATOR_URL vacío: las órdenes del enchufe no se pueden aplicar")
    control = ControlPlugUseCase(
        HttpActuatorGateway(settings.actuator_url) if settings.actuator_url else None,
        mqtt,
        clock,
        thresholds_for,
        settings.plug_id,
        parse_plug_command,
        auto_cutoff_enabled=settings.auto_cutoff_enabled,
        restore_ut=settings.restore_ut,
    )
    ble_clients = []
    for name, device_id in zip(settings.device_names, settings.device_ids):
        ble_clients.append(
            BleSensorClient(
                name, queue, sensor_threshold, connectivity, clock, device_id=device_id
            )
        )

    runner_ref: List[EdgeRunner] = []
    ingest = IngestReadingUseCase(
        outbox,
        ReadingFactory(settings.saturation_ut),
        clock,
        thresholds_for,
        on_stored=lambda reading: runner_ref[0].reading_stored(reading),
    )
    runner = EdgeRunner(
        queue=queue,
        ingest=ingest,
        publish=PublishOutboxUseCase(outbox, mqtt, clock, BatchPolicy()),
        purge=PurgeOutboxUseCase(outbox, clock, settings.outbox_retention_days),
        connectivity=connectivity,
        mqtt=mqtt,
        ble_clients=ble_clients,
        control=control,
        outbox=outbox,
        thresholds_for=thresholds_for,
    )
    runner_ref.append(runner)
    return runner
