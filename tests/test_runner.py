"""Orquestación: un ciclo completo con dobles (BLE → ingesta → outbox → MQTT → publicada).

Casos de uso reales; solo los bordes (BLE, broker, SQLite) son dobles.
"""
import asyncio
import unittest

from monitoring.application.control_plug import ControlPlugUseCase
from monitoring.application.ingest_reading import IngestReadingUseCase
from monitoring.application.publish_outbox import PublishOutboxUseCase
from monitoring.application.purge_outbox import PurgeOutboxUseCase
from monitoring.application.track_connectivity import TrackConnectivityUseCase
from monitoring.domain.services import ReadingFactory
from monitoring.domain.value_objects import AlertThresholds
from monitoring.infrastructure.ble_sensor_client import ReadingReceived, SessionStarted
from monitoring.infrastructure.ble_protocol import RawReading
from monitoring.interfaces.runner import EdgeRunner, build_runner
from shared.config import EdgeSettings
from tests.fakes import PUBLISHED, FakeClock, FakeOutbox, sequential_ids

DEVICE = "EMSafe-DOS-01"
TS = 1762959843


class FakeMqtt:
    """Broker simulado para el runner: telemetría con PUBACK y estados retained."""

    def __init__(self, connected=True):
        self.is_connected = connected
        self.telemetry = []
        self.statuses = []
        self.started = self.stopped = False
        self.on_connected = None

    def start(self, loop, on_connected):
        self.started = True
        self.on_connected = on_connected

    def stop(self):
        self.stopped = True

    async def publish_telemetry(self, message, published_at):
        if not self.is_connected:
            return False
        self.telemetry.append(message)
        return True

    def publish_device_status(self, device_id, connectivity, changed_at):
        if not self.is_connected:
            return False
        self.statuses.append((device_id, connectivity))
        return True


class FakeBle:
    """Sensor simulado: al 'conectar' encola la sesión y sus lecturas."""

    def __init__(self, queue, connectivity, readings):
        self.device_name = DEVICE
        self.queue, self.connectivity, self.readings = queue, connectivity, readings
        self._stopped = asyncio.Event()

    async def run(self):
        self.queue.put_nowait(SessionStarted(DEVICE))
        for raw in self.readings:
            self.queue.put_nowait(ReadingReceived(DEVICE, raw))
        self.connectivity.device_connected(DEVICE)
        await self._stopped.wait()
        self.connectivity.device_disconnected(DEVICE)

    async def stop(self):
        self._stopped.set()


def build(readings, connected=True, ingest=None):
    queue, clock, outbox = asyncio.Queue(), FakeClock(), FakeOutbox()
    mqtt = FakeMqtt(connected)
    connectivity = TrackConnectivityUseCase(mqtt, clock)
    runner = EdgeRunner(
        queue=queue,
        ingest=ingest or IngestReadingUseCase(outbox, ReadingFactory(790.0), clock),
        publish=PublishOutboxUseCase(outbox, mqtt, clock, new_message_id=sequential_ids()),
        purge=PurgeOutboxUseCase(outbox, clock, 7),
        connectivity=connectivity,
        mqtt=mqtt,
        ble_clients=[FakeBle(queue, connectivity, readings)],
        publish_interval_s=0.01,
    )
    return runner, outbox, mqtt


async def wait_until(condition, timeout=2.0):
    async def poll():
        while not condition():
            await asyncio.sleep(0.005)
    await asyncio.wait_for(poll(), timeout)


def raws(n, deferred=1, start=1):
    return [RawReading(seq=s, ts=TS + s, uT=10.0, deferred=deferred) for s in range(start, start + n)]


class FullCycleTest(unittest.IsolatedAsyncioTestCase):
    async def test_lectura_entra_se_guarda_se_publica_y_queda_marcada(self):
        # Flush típico del firmware: 60 diferidas (con un reenvío) y luego tiempo real.
        readings = raws(60) + raws(5) + raws(2, deferred=0, start=100)
        runner, outbox, mqtt = build(readings)
        task = asyncio.create_task(runner.run())

        await wait_until(lambda: len(outbox.entries) == 62 and all(s == PUBLISHED for s in outbox.statuses()))
        runner.stop()
        await asyncio.wait_for(task, 2)

        self.assertTrue(mqtt.started and mqtt.stopped)
        self.assertEqual(sum(len(m.readings) for m in mqtt.telemetry), 62)  # sin duplicados
        self.assertEqual(outbox.pending_count(), 0)
        self.assertEqual(mqtt.statuses, [(DEVICE, "ONLINE"), (DEVICE, "OFFLINE")])

    async def test_sin_broker_acumula_y_publica_al_reconectar(self):
        runner, outbox, mqtt = build(raws(3, deferred=0), connected=False)
        task = asyncio.create_task(runner.run())

        await wait_until(lambda: len(outbox.entries) == 3)
        await asyncio.sleep(0.05)
        self.assertEqual(mqtt.telemetry, [])
        self.assertEqual(outbox.pending_count(), 3)

        mqtt.is_connected = True
        mqtt.on_connected()  # paho avisa la reconexión
        await wait_until(lambda: outbox.pending_count() == 0)

        # El estado ONLINE ocurrido sin broker se republica al reconectar.
        self.assertIn((DEVICE, "ONLINE"), mqtt.statuses)
        runner.stop()
        await asyncio.wait_for(task, 2)

    async def test_un_error_en_la_ingesta_no_detiene_al_consumidor(self):
        class FlakyIngest:
            def __init__(self):
                self.calls = 0

            def session_started(self, device_id):
                pass

            def ingest(self, *args):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("fallo inesperado")
                return None

        flaky = FlakyIngest()
        runner, _, _ = build(raws(3), ingest=flaky)
        task = asyncio.create_task(runner.run())

        with self.assertLogs("monitoring.interfaces.runner", level="ERROR"):
            await wait_until(lambda: flaky.calls == 3)
        runner.stop()
        await asyncio.wait_for(task, 2)


class RecordingActuator:
    def __init__(self):
        self.calls = []

    def turn_off(self):
        self.calls.append("off")
        return True

    def turn_on(self):
        self.calls.append("on")
        return True


class PlugWorkerTest(unittest.IsolatedAsyncioTestCase):
    async def test_tiempo_real_en_danger_corta_y_las_diferidas_no(self):
        clock, actuator = FakeClock(), RecordingActuator()
        thresholds = lambda _d: AlertThresholds(100, 200)
        control = ControlPlugUseCase(actuator, None, clock, thresholds)
        holder = []
        ingest = IngestReadingUseCase(
            FakeOutbox(), ReadingFactory(790.0), clock, thresholds,
            on_stored=lambda r: holder[0].reading_stored(r),
        )
        readings = [
            RawReading(seq=1, ts=TS + 1, uT=500.0, deferred=1),
            RawReading(seq=2, ts=TS + 2, uT=250.0, deferred=0),
        ]
        runner, _, _ = build(readings, ingest=ingest)
        runner._control = control
        holder.append(runner)
        task = asyncio.create_task(runner.run())

        await wait_until(lambda: actuator.calls == ["off"])
        runner.stop()
        await asyncio.wait_for(task, 2)
        self.assertEqual(runner.health()["actuator"]["plug"], "OFF")


class BuildRunnerTest(unittest.TestCase):
    def test_arma_el_edge_real_sin_arrancarlo(self):
        settings = EdgeSettings.from_env({
            "EMSAFE_DB_PATH": ":memory:",
            "EMSAFE_DEVICE_NAME": "EMSafe-FIX-02",
            "EMSAFE_ACTUATOR_URL": "http://192.168.1.50",
        })
        runner = build_runner(settings)
        try:
            self.assertIsInstance(runner, EdgeRunner)
            self.assertEqual([c.device_name for c in runner._ble_clients], ["EMSafe-FIX-02"])
            self.assertFalse(runner._mqtt.is_connected)
            health = runner.health()
            self.assertEqual(health["ble"], [{"device": "EMSafe-FIX-02", "connected": False}])
            self.assertTrue(health["actuator"]["enabled"])
            self.assertEqual(health["outbox"], {"pending": 0})
            self.assertEqual(health["thresholds"], {"cautionUT": 100.0, "dangerUT": 200.0, "restoreUT": 160.0})
        finally:
            from shared.database import db
            db.close()


if __name__ == "__main__":
    unittest.main()
