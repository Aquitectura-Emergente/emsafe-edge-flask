"""Adaptador MQTT (paho 2.x, MQTT v5).

paho corre en su propio hilo; lo que vuelve al loop asyncio pasa por
`call_soon_threadsafe` o `run_coroutine_threadsafe`.

La entrega la garantiza el outbox, no la sesión de paho: se conecta con
`clean_start`, sin conexión no se publica (la memoria de paho se perdería
al reiniciar) y al desconectarse los PUBACK pendientes fallan para que el
outbox reintente.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from datetime import datetime
from typing import Awaitable, Callable, Dict, Optional, Sequence

from monitoring.domain.entities import OutboxMessage
from monitoring.domain.value_objects import DeviceType
from monitoring.infrastructure import mqtt_payloads as mp
from shared.clock import Clock
from shared.config import EdgeSettings

log = logging.getLogger(__name__)

ConfigHandler = Callable[[str, bytes], Awaitable[object]]


def _failed(reason_code) -> bool:
    """paho entrega ReasonCode o int según el caso."""
    if reason_code is None:
        return False
    if hasattr(reason_code, "is_failure"):
        return bool(reason_code.is_failure)
    return reason_code != 0


def _default_client_factory(settings: EdgeSettings):
    import paho.mqtt.client as mqtt

    return mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=settings.edge_node_id,
        protocol=mqtt.MQTTv5,
    )


class MqttGateway:
    def __init__(
        self,
        settings: EdgeSettings,
        clock: Clock,
        device_ids: Sequence[str],
        *,
        client_factory: Callable[[EdgeSettings], object] = _default_client_factory,
        device_type: DeviceType = DeviceType.FIXED_SENSOR,
    ) -> None:
        self._settings = settings
        self._clock = clock
        self._device_ids = list(device_ids)
        self._device_type = device_type
        self._client = client_factory(settings)
        self._lock = threading.Lock()
        self._pending: Dict[int, asyncio.Future] = {}
        self._connected = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._on_config: Optional[ConfigHandler] = None
        self._on_connected: Optional[Callable[[], None]] = None
        self._config_topics = {mp.device_config_topic(d): d for d in self._device_ids}

    # ---------- Arranque y parada ----------

    def start(
        self,
        loop: asyncio.AbstractEventLoop,
        on_config: ConfigHandler,
        on_connected: Optional[Callable[[], None]] = None,
    ) -> None:
        """Conecta en segundo plano; paho reconecta solo."""
        self._loop = loop
        self._on_config = on_config
        self._on_connected = on_connected
        client, s = self._client, self._settings

        client.on_connect = self._handle_connect
        client.on_disconnect = self._handle_disconnect
        client.on_publish = self._handle_publish
        client.on_message = self._handle_message

        if s.mqtt_username:
            client.username_pw_set(s.mqtt_username, s.mqtt_password)
        if s.mqtt_tls:
            client.tls_set()  # CA del sistema
        client.will_set(
            mp.edge_status_topic(s.edge_node_id),
            mp.to_json(mp.edge_last_will_payload()),
            qos=1,
            retain=True,
        )
        client.reconnect_delay_set(min_delay=1, max_delay=30)
        log.info(
            "Conectando al broker MQTT %s:%s (TLS %s) como %s",
            s.mqtt_host, s.mqtt_port, "sí" if s.mqtt_tls else "no", s.edge_node_id,
        )
        client.connect_async(s.mqtt_host, s.mqtt_port, keepalive=30, clean_start=True)
        client.loop_start()

    def stop(self, timeout_s: float = 2.0) -> None:
        """Publica OFFLINE y desconecta: en una desconexión limpia el broker no envía el Last Will."""
        client = self._client
        if self._connected:
            try:
                info = client.publish(
                    mp.edge_status_topic(self._settings.edge_node_id),
                    mp.to_json(mp.edge_last_will_payload()),
                    qos=1,
                    retain=True,
                )
                info.wait_for_publish(timeout=timeout_s)
            except Exception as exc:
                log.warning("No se pudo publicar OFFLINE del Edge al apagar: %s", exc)
        try:
            client.disconnect()
        finally:
            client.loop_stop()
            self._connected = False
            self._fail_pending("Edge apagándose")

    @property
    def is_connected(self) -> bool:
        return self._connected

    # ---------- TelemetryPublisher ----------

    async def publish_telemetry(self, message: OutboxMessage, published_at: datetime) -> bool:
        if not self._connected:
            return False
        payload = mp.telemetry_payload(
            message,
            organization_id=self._settings.organization_id,
            edge_node_id=self._settings.edge_node_id,
            published_at=published_at,
            device_type=self._device_type,
        )
        future = asyncio.get_running_loop().create_future()
        # Sin el lock, el PUBACK podría llegar antes de registrar el futuro.
        with self._lock:
            info = self._client.publish(
                mp.telemetry_topic(message.device_id), mp.to_json(payload), qos=1
            )
            if info.rc != 0:
                log.warning("paho rechazó la publicación de %s (rc=%s)", message.message_id, info.rc)
                return False
            self._pending[info.mid] = future
        try:
            return await future
        finally:
            with self._lock:
                self._pending.pop(info.mid, None)

    # ---------- StatusPublisher ----------

    def publish_device_status(self, device_id: str, connectivity: str, changed_at: datetime) -> bool:
        if not self._connected:
            return False
        payload = mp.device_status_payload(
            device_id, self._settings.edge_node_id, connectivity, changed_at
        )
        info = self._client.publish(
            mp.device_status_topic(device_id), mp.to_json(payload), qos=1, retain=True
        )
        return info.rc == 0

    # ---------- Callbacks de paho (hilo de red) ----------

    def _handle_connect(self, client, _userdata, _flags, reason_code, _properties=None) -> None:
        if _failed(reason_code):
            log.warning("El broker rechazó la conexión: %s", reason_code)
            return
        self._connected = True
        log.info("Conectado al broker MQTT")
        client.publish(
            mp.edge_status_topic(self._settings.edge_node_id),
            mp.to_json(mp.edge_online_payload(self._clock.now())),
            qos=1,
            retain=True,
        )
        # Con clean_start el broker no recuerda las suscripciones.
        for topic in self._config_topics:
            client.subscribe(topic, qos=1)
        if self._on_connected is not None and self._loop is not None:
            self._loop.call_soon_threadsafe(self._on_connected)

    def _handle_disconnect(self, _client, _userdata, _flags, reason_code, _properties=None) -> None:
        was_connected, self._connected = self._connected, False
        if was_connected:
            log.warning("Desconectado del broker MQTT (%s): paho reintentará", reason_code)
        self._fail_pending("desconexión del broker")

    def _handle_publish(self, _client, _userdata, mid, reason_code, _properties=None) -> None:
        with self._lock:
            future = self._pending.get(mid)
        if future is None:
            return  # estados, o PUBACK tardío de un intento abandonado
        acked = not _failed(reason_code)
        if not acked:
            log.warning("El broker respondió el PUBACK %s con error: %s", mid, reason_code)
        self._resolve(future, acked)

    def _handle_message(self, _client, _userdata, message) -> None:
        device_id = self._config_topics.get(message.topic)
        if device_id is None or self._on_config is None or self._loop is None:
            return
        asyncio.run_coroutine_threadsafe(
            self._on_config(device_id, bytes(message.payload)), self._loop
        )

    # ---------- Utilidades ----------

    def _resolve(self, future: asyncio.Future, value: bool) -> None:
        def set_result() -> None:
            if not future.done():
                future.set_result(value)

        loop = future.get_loop()
        if loop.is_closed():
            return
        loop.call_soon_threadsafe(set_result)

    def _fail_pending(self, reason: str) -> None:
        with self._lock:
            futures = list(self._pending.values())
            self._pending.clear()
        for future in futures:
            self._resolve(future, False)
        if futures:
            log.warning("%d publicaciones sin PUBACK por %s: se reintentarán", len(futures), reason)
