"""Adaptador MQTT: LWT, PUBACK → futuro, desconexión y estados.

Sin broker: `FakePahoClient` registra las llamadas y el test dispara los
callbacks de paho a mano (incluso desde otro hilo, como hace paho).
"""
import asyncio
import json
import threading
import unittest

from monitoring.domain.entities import OutboxMessage
from monitoring.domain.services import ReadingFactory
from monitoring.infrastructure.mqtt_client import MqttGateway
from shared.config import EdgeSettings
from tests.fakes import T0, FakeClock

DEVICE = "EMSafe-DOS-01"
LOGGER = "monitoring.infrastructure.mqtt_client"


class FakeInfo:
    def __init__(self, mid, rc=0):
        self.mid, self.rc = mid, rc

    def wait_for_publish(self, timeout=None):
        return None


class FakeMessage:
    def __init__(self, topic, payload):
        self.topic, self.payload = topic, payload


class FakeReasonCode:
    def __init__(self, failure):
        self.is_failure = failure

    def __str__(self):
        return "Not authorized" if self.is_failure else "Success"


class FakePahoClient:
    def __init__(self):
        self.calls = []
        self.published = []
        self.subscribed = []
        self.next_mid = 0
        self.publish_rc = 0

    def username_pw_set(self, u, p):
        self.calls.append(("username_pw_set", u, p))

    def tls_set(self):
        self.calls.append(("tls_set",))

    def will_set(self, topic, payload, qos, retain):
        self.calls.append(("will_set", topic, json.loads(payload), qos, retain))

    def reconnect_delay_set(self, min_delay, max_delay):
        self.calls.append(("reconnect_delay_set", min_delay, max_delay))

    def connect_async(self, host, port, keepalive, clean_start):
        self.calls.append(("connect_async", host, port, clean_start))

    def loop_start(self):
        self.calls.append(("loop_start",))

    def loop_stop(self):
        self.calls.append(("loop_stop",))

    def disconnect(self):
        self.calls.append(("disconnect",))

    def subscribe(self, topic, qos):
        self.subscribed.append((topic, qos))

    def publish(self, topic, payload, qos=0, retain=False):
        self.next_mid += 1
        self.published.append((topic, json.loads(payload), qos, retain, self.next_mid))
        return FakeInfo(self.next_mid, self.publish_rc)


def message():
    reading = ReadingFactory(790.0).from_raw(DEVICE, 1, 1762959844, 12.5, 0, T0)
    return OutboxMessage("m-1", (reading,))


class GatewayTestCase(unittest.IsolatedAsyncioTestCase):
    def make(self, **env):
        self.paho = FakePahoClient()
        self.connected_calls = 0
        settings = EdgeSettings.from_env(env)
        self.gateway = MqttGateway(settings, FakeClock(), [DEVICE], client_factory=lambda s: self.paho)

        def on_connected():
            self.connected_calls += 1

        self.gateway.start(asyncio.get_running_loop(), on_connected)
        return self.gateway

    def connect(self):
        self.gateway._handle_connect(self.paho, None, None, FakeReasonCode(False), None)


class StartTest(GatewayTestCase):
    async def test_last_will_retained_antes_de_conectar(self):
        self.make()
        names = [c[0] for c in self.paho.calls]

        self.assertLess(names.index("will_set"), names.index("connect_async"))
        will = next(c for c in self.paho.calls if c[0] == "will_set")
        self.assertEqual(will, ("will_set", "edge/EDGE-HOGAR-01/status", {"status": "OFFLINE"}, 1, True))
        self.assertIn(("connect_async", "localhost", 1883, True), self.paho.calls)
        self.assertIn(("loop_start",), self.paho.calls)
        self.assertNotIn(("tls_set",), self.paho.calls)

    async def test_tls_y_credenciales_desde_el_entorno(self):
        self.make(EMSAFE_MQTT_TLS="true", EMSAFE_MQTT_PORT="8883",
                  EMSAFE_MQTT_USERNAME="edge07", EMSAFE_MQTT_PASSWORD="secreto")

        self.assertIn(("tls_set",), self.paho.calls)
        self.assertIn(("username_pw_set", "edge07", "secreto"), self.paho.calls)
        self.assertIn(("connect_async", "localhost", 8883, True), self.paho.calls)


class ConnectTest(GatewayTestCase):
    async def test_al_conectar_publica_online_y_avisa(self):
        self.make()
        self.connect()
        await asyncio.sleep(0)

        self.assertTrue(self.gateway.is_connected)
        topic, payload, qos, retain, _ = self.paho.published[0]
        self.assertEqual((topic, qos, retain), ("edge/EDGE-HOGAR-01/status", 1, True))
        self.assertEqual(payload, {"status": "ONLINE", "changedAt": "2026-11-12T15:04:04Z"})
        self.assertEqual(self.connected_calls, 1)

    async def test_conexion_rechazada_no_marca_conectado(self):
        self.make()
        with self.assertLogs(LOGGER, level="WARNING"):
            self.gateway._handle_connect(self.paho, None, None, FakeReasonCode(True), None)
        self.assertFalse(self.gateway.is_connected)
        self.assertEqual(self.paho.subscribed, [])


class PublishTelemetryTest(GatewayTestCase):
    async def test_sin_conexion_falla_de_inmediato_sin_encolar_en_paho(self):
        self.make()
        self.assertFalse(await self.gateway.publish_telemetry(message(), T0))
        self.assertEqual(self.paho.published, [])

    async def test_puback_resuelve_el_futuro_del_mid_correcto(self):
        self.make()
        self.connect()
        task = asyncio.create_task(self.gateway.publish_telemetry(message(), T0))
        await asyncio.sleep(0)

        topic, payload, qos, retain, mid = self.paho.published[-1]
        self.assertEqual((topic, qos, retain), ("iot/EMSafe-DOS-01/telemetry", 1, False))
        self.assertEqual(payload["messageId"], "m-1")
        self.assertEqual(payload["deviceType"], "FIXED_SENSOR")
        # PUBACK de otro mid: no resuelve este.
        self.gateway._handle_publish(self.paho, None, 999, FakeReasonCode(False), None)
        await asyncio.sleep(0)
        self.assertFalse(task.done())

        # PUBACK correcto, desde el hilo de red como en paho.
        t = threading.Thread(target=self.gateway._handle_publish,
                             args=(self.paho, None, mid, FakeReasonCode(False), None))
        t.start()
        t.join()

        self.assertTrue(await asyncio.wait_for(task, 1))
        self.assertEqual(self.gateway._pending, {})

    async def test_puback_con_codigo_de_error_es_fallo(self):
        self.make()
        self.connect()
        task = asyncio.create_task(self.gateway.publish_telemetry(message(), T0))
        await asyncio.sleep(0)
        mid = self.paho.published[-1][4]

        with self.assertLogs(LOGGER, level="WARNING"):
            self.gateway._handle_publish(self.paho, None, mid, FakeReasonCode(True), None)

        self.assertFalse(await asyncio.wait_for(task, 1))

    async def test_desconexion_falla_los_pendientes(self):
        self.make()
        self.connect()
        task = asyncio.create_task(self.gateway.publish_telemetry(message(), T0))
        await asyncio.sleep(0)

        with self.assertLogs(LOGGER, level="WARNING"):
            self.gateway._handle_disconnect(self.paho, None, None, FakeReasonCode(True), None)

        self.assertFalse(await asyncio.wait_for(task, 1))
        self.assertFalse(self.gateway.is_connected)

    async def test_paho_rechaza_la_publicacion(self):
        self.make()
        self.connect()
        self.paho.publish_rc = 4  # MQTT_ERR_NO_CONN
        with self.assertLogs(LOGGER, level="WARNING"):
            self.assertFalse(await self.gateway.publish_telemetry(message(), T0))
        self.assertEqual(self.gateway._pending, {})


class DeviceStatusTest(GatewayTestCase):
    async def test_publica_estado_retained(self):
        self.make()
        self.connect()

        self.assertTrue(self.gateway.publish_device_status(DEVICE, "OFFLINE", T0))

        topic, payload, qos, retain, _ = self.paho.published[-1]
        self.assertEqual((topic, qos, retain), ("iot/EMSafe-DOS-01/status", 1, True))
        self.assertEqual(payload["connectivity"], "OFFLINE")

    async def test_sin_conexion_devuelve_false(self):
        self.make()
        self.assertFalse(self.gateway.publish_device_status(DEVICE, "ONLINE", T0))


class StopTest(GatewayTestCase):
    async def test_apagado_publica_offline_y_desconecta(self):
        self.make()
        self.connect()

        self.gateway.stop()

        topic, payload, qos, retain, _ = self.paho.published[-1]
        self.assertEqual((topic, payload, qos, retain),
                         ("edge/EDGE-HOGAR-01/status", {"status": "OFFLINE"}, 1, True))
        self.assertIn(("disconnect",), self.paho.calls)
        self.assertEqual(self.paho.calls[-1], ("loop_stop",))


class PlugTest(GatewayTestCase):
    def make_with_commands(self):
        self.paho = FakePahoClient()
        self.commands = []
        settings = EdgeSettings.from_env({})
        self.gateway = MqttGateway(settings, FakeClock(), [DEVICE], client_factory=lambda s: self.paho)

        async def on_command(raw):
            self.commands.append(raw)

        self.gateway.start(asyncio.get_running_loop(), None, on_command)

    async def test_sin_handler_no_se_suscribe(self):
        self.make()
        self.connect()
        self.assertEqual(self.paho.subscribed, [])

    async def test_se_suscribe_a_las_ordenes_y_las_pasa_al_loop(self):
        self.make_with_commands()
        self.connect()
        self.assertEqual(self.paho.subscribed, [("iot/EMSafe-PLUG-01/command", 1)])

        t = threading.Thread(target=self.gateway._handle_message,
                             args=(self.paho, None, FakeMessage("iot/EMSafe-PLUG-01/command", b'{"x":1}')))
        t.start()
        t.join()
        for _ in range(10):
            await asyncio.sleep(0.001)
        self.assertEqual(self.commands, [b'{"x":1}'])

    async def test_topico_ajeno_se_ignora(self):
        self.make_with_commands()
        self.gateway._handle_message(self.paho, None, FakeMessage("iot/OTRO/command", b"{}"))
        await asyncio.sleep(0.01)
        self.assertEqual(self.commands, [])

    async def test_estado_del_enchufe_retained_qos_1(self):
        from monitoring.domain.value_objects import PlugState, PlugStateSource

        self.make()
        self.assertFalse(self.gateway.publish_plug_state(
            "EMSafe-PLUG-01", PlugState.OFF, "c-1", PlugStateSource.USER, T0))
        self.connect()
        self.assertTrue(self.gateway.publish_plug_state(
            "EMSafe-PLUG-01", PlugState.OFF, "c-1", PlugStateSource.USER, T0))
        topic, payload, qos, retain, _ = self.paho.published[-1]
        self.assertEqual((topic, qos, retain), ("iot/EMSafe-PLUG-01/state", 1, True))
        self.assertEqual(payload["state"], "OFF")
        self.assertEqual(payload["source"], "USER")
        self.assertEqual(payload["commandId"], "c-1")


if __name__ == "__main__":
    unittest.main()
