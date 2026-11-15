"""Arma el Edge hogar real (runner, casos de uso, outbox SQLite, adaptadores
MQTT y HTTP) con dobles solo para lo externo: el broker MQTT y la llamada HTTP
al actuador. Las lecturas entran a la misma cola que llena el cliente BLE.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading

import requests

from monitoring.application.control_plug import ControlPlugUseCase
from monitoring.application.ingest_reading import IngestReadingUseCase
from monitoring.application.publish_outbox import PublishOutboxUseCase
from monitoring.application.purge_outbox import PurgeOutboxUseCase
from monitoring.application.track_connectivity import TrackConnectivityUseCase
from monitoring.domain.services import BatchPolicy, ReadingFactory
from monitoring.domain.value_objects import AlertThresholds
from monitoring.infrastructure.http_actuator import HttpActuatorGateway
from monitoring.infrastructure.mqtt_client import MqttGateway
from monitoring.infrastructure.mqtt_payloads import parse_plug_command
from monitoring.infrastructure.sqlite_outbox import SqliteReadingOutbox
from monitoring.interfaces.runner import EdgeRunner
from shared.clock import SystemClock
from shared.config import EdgeSettings
from shared.database import init_db

ACTUATOR_URL = "http://actuador-enchufe.local"


class _Info:
    def __init__(self, mid: int) -> None:
        self.mid, self.rc = mid, 0

    def wait_for_publish(self, timeout=None) -> None:
        return None


class _Message:
    def __init__(self, topic: str, payload: bytes) -> None:
        self.topic, self.payload = topic, payload


class FakeBroker:
    """Doble del cliente paho: registra lo publicado, entrega mensajes por
    `on_message` y responde el PUBACK de la telemetría como lo haría paho."""

    def __init__(self) -> None:
        self.published = []   # (topic, payload dict)
        self.subscribed = []
        self.on_connect = self.on_disconnect = self.on_publish = self.on_message = None
        self._mid = 0
        self._lock = threading.Lock()

    def username_pw_set(self, *_):
        pass

    def tls_set(self):
        pass

    def will_set(self, *_, **__):
        pass

    def reconnect_delay_set(self, **_):
        pass

    def connect_async(self, *_, **__):
        pass

    def loop_start(self):
        pass

    def loop_stop(self):
        pass

    def disconnect(self):
        pass

    def subscribe(self, topic, qos=0):
        self.subscribed.append(topic)

    def publish(self, topic, payload, qos=0, retain=False):
        with self._lock:
            self._mid += 1
            mid = self._mid
            self.published.append((topic, json.loads(payload)))
        if topic.endswith("/telemetry"):
            # El adaptador espera el PUBACK en el loop; llega después de registrar la espera.
            asyncio.get_running_loop().call_soon(self.on_publish, self, None, mid, 0, None)
        return _Info(mid)

    # --- control desde los escenarios ---
    def connect(self) -> None:
        self.on_connect(self, None, None, 0, None)

    def deliver(self, topic: str, payload: dict) -> None:
        self.on_message(self, None, _Message(topic, json.dumps(payload).encode("utf-8")))

    def on_topic(self, topic: str):
        with self._lock:
            return [p for t, p in self.published if t == topic]


class FakeHttp:
    """Reemplaza a `requests.get` del adaptador del actuador."""

    def __init__(self) -> None:
        self.responds = True
        self.calls = []

    def __call__(self, url, timeout=None):
        self.calls.append(url)
        if not self.responds:
            raise requests.exceptions.ConnectTimeout(f"sin respuesta de {url}")
        response = requests.models.Response()
        response.status_code = 200
        return response


class _Records(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.records = []

    def emit(self, record) -> None:
        self.records.append(record)


def run_until(context, condition, timeout_s: float = 3.0) -> bool:
    """Deja correr el Edge hasta que se cumpla la condición o venza el plazo."""

    async def wait() -> bool:
        deadline = context.loop.time() + timeout_s
        while context.loop.time() < deadline:
            if condition():
                return True
            await asyncio.sleep(0.01)
        return condition()

    return context.loop.run_until_complete(wait())


def before_scenario(context, scenario):
    context.database = init_db(":memory:")
    context.loop = asyncio.new_event_loop()
    context.settings = EdgeSettings(actuator_url=ACTUATOR_URL)
    context.actuator_url = ACTUATOR_URL
    context.run_until = lambda condition, timeout_s=3.0: run_until(context, condition, timeout_s)
    context.device_id = context.settings.device_ids[0]
    clock = SystemClock()
    thresholds = AlertThresholds(context.settings.caution_ut, context.settings.danger_ut)

    context.broker = FakeBroker()
    context.http = FakeHttp()
    mqtt = MqttGateway(
        context.settings, clock, context.settings.device_ids,
        client_factory=lambda _s: context.broker,
    )
    control = ControlPlugUseCase(
        HttpActuatorGateway(ACTUATOR_URL, timeout_s=0.5, get=context.http),
        mqtt, clock, lambda _d: thresholds, context.settings.plug_id, parse_plug_command,
    )
    context.outbox = SqliteReadingOutbox()
    context.queue = asyncio.Queue()
    runner_ref = []
    ingest = IngestReadingUseCase(
        context.outbox, ReadingFactory(context.settings.saturation_ut), clock,
        lambda _d: thresholds, on_stored=lambda r: runner_ref[0].reading_stored(r),
    )
    context.runner = EdgeRunner(
        queue=context.queue,
        ingest=ingest,
        publish=PublishOutboxUseCase(context.outbox, mqtt, clock, BatchPolicy()),
        purge=PurgeOutboxUseCase(context.outbox, clock),
        connectivity=TrackConnectivityUseCase(mqtt, clock),
        mqtt=mqtt,
        ble_clients=[],
        control=control,
        outbox=context.outbox,
        publish_interval_s=0.05,
    )
    runner_ref.append(context.runner)
    context.mqtt = mqtt

    context.logs = _Records()
    logging.getLogger().addHandler(context.logs)

    asyncio.set_event_loop(context.loop)
    context.task = context.loop.create_task(context.runner.run())
    run_until(context, lambda: context.broker.on_connect is not None)
    context.broker.connect()
    run_until(context, lambda: mqtt.is_connected)


def after_scenario(context, scenario):
    logging.getLogger().removeHandler(context.logs)
    context.runner.stop()
    context.loop.run_until_complete(asyncio.wait_for(context.task, timeout=5))
    context.loop.close()
    asyncio.set_event_loop(None)
    context.database.close()
