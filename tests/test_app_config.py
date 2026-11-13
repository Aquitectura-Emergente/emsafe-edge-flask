"""Arranque Flask: modo debug, `/health` y lecturas locales."""
import os
import threading
import unittest

import app as edge_app
from monitoring.domain.services import ReadingFactory
from monitoring.domain.value_objects import AlertThresholds
from monitoring.interfaces.edge_thread import EdgeThread
from tests.fakes import T0


class DebugFlagTest(unittest.TestCase):
    def setUp(self):
        self._previo = os.environ.get("EMSAFE_EDGE_DEBUG")

    def tearDown(self):
        if self._previo is None:
            os.environ.pop("EMSAFE_EDGE_DEBUG", None)
        else:
            os.environ["EMSAFE_EDGE_DEBUG"] = self._previo

    def test_apagado_por_defecto(self):
        os.environ.pop("EMSAFE_EDGE_DEBUG", None)
        self.assertFalse(edge_app._debug_enabled())

    def test_se_puede_activar_a_mano(self):
        for valor in ("1", "true", "TRUE", "yes", " true "):
            with self.subTest(valor=valor):
                os.environ["EMSAFE_EDGE_DEBUG"] = valor
                self.assertTrue(edge_app._debug_enabled())

    def test_cualquier_otro_valor_lo_deja_apagado(self):
        for valor in ("0", "false", "no", "", "quizas"):
            with self.subTest(valor=valor):
                os.environ["EMSAFE_EDGE_DEBUG"] = valor
                self.assertFalse(edge_app._debug_enabled())


class FakeHistory:
    def __init__(self, readings):
        self.readings = readings
        self.calls = []

    def latest(self, device_id=None, limit=50):
        self.calls.append((device_id, limit))
        return self.readings[:limit]


class ApiTest(unittest.TestCase):
    def client(self, history=None, health=None):
        web = edge_app.create_app(history, health)
        web.config.update(TESTING=True)
        return web.test_client()

    def test_health_sin_edge(self):
        resp = edge_app.app.test_client().get("/health")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"status": "UP", "service": "emsafe-edge"})

    def test_health_incluye_ble_mqtt_y_actuador(self):
        health = lambda: {"edge": "UP", "ble": [{"device": "EMSafe-DOS-01", "connected": True}],
                          "mqtt": {"connected": False}, "actuator": {"enabled": True, "plug": "OFF"}}
        body = self.client(health=health).get("/health").get_json()
        self.assertEqual(body["status"], "UP")
        self.assertTrue(body["ble"][0]["connected"])
        self.assertFalse(body["mqtt"]["connected"])
        self.assertEqual(body["actuator"]["plug"], "OFF")

    def test_lecturas_incluyen_el_nivel(self):
        factory = ReadingFactory(790.0)
        readings = [
            factory.from_raw("EMSafe-DOS-01", 2, 0, 250.0, 0, T0, AlertThresholds(100, 200)),
            factory.from_raw("EMSafe-DOS-01", 1, 1762959843, 800.0, 1, T0, AlertThresholds(100, 200)),
        ]
        history = FakeHistory(readings)
        resp = self.client(history).get("/api/v1/emf-monitoring/readings?limit=5&deviceId=EMSafe-DOS-01")

        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(history.calls, [("EMSafe-DOS-01", 5)])
        self.assertEqual([r["level"] for r in body], ["DANGER", "DANGER"])
        self.assertEqual(body[0]["field_uT"], 250.0)
        self.assertEqual(body[0]["origin"], "REALTIME")
        self.assertEqual(body[0]["capturedAtSource"], "EDGE_RECEIVED")
        self.assertEqual(body[1]["quality"], "SATURATED")
        self.assertEqual(body[1]["origin"], "DEFERRED")

    def test_limite_acotado_y_validado(self):
        history = FakeHistory([])
        client = self.client(history)
        client.get("/api/v1/emf-monitoring/readings?limit=100000")
        client.get("/api/v1/emf-monitoring/readings?limit=0")
        self.assertEqual([c[1] for c in history.calls], [500, 1])
        self.assertEqual(client.get("/api/v1/emf-monitoring/readings?limit=x").status_code, 400)

    def test_sin_historial_responde_lista_vacia(self):
        self.assertEqual(self.client().get("/api/v1/emf-monitoring/readings").get_json(), [])


class FakeRunner:
    def __init__(self):
        self.stopped = None

    async def run(self):
        import asyncio
        self.stopped = asyncio.Event()
        await self.stopped.wait()

    def stop(self):
        self.stopped.set()

    def health(self):
        return {"mqtt": {"connected": True}}


class EdgeThreadTest(unittest.TestCase):
    def test_arranca_responde_health_y_se_detiene(self):
        edge = EdgeThread(settings=None, build=lambda _s: FakeRunner())
        edge.start(timeout_s=5)
        try:
            self.assertTrue(edge.alive)
            self.assertEqual(edge.health(), {"edge": "UP", "mqtt": {"connected": True}})
        finally:
            edge.stop(timeout_s=5)
        self.assertFalse(edge.alive)
        self.assertEqual(edge.health(), {"edge": "DOWN"})

    def test_error_al_armar_se_relanza(self):
        def boom(_s):
            raise ValueError("config rota")

        edge = EdgeThread(settings=None, build=boom)
        with self.assertRaises(ValueError):
            edge.start(timeout_s=5)
        self.assertNotIn("edge-asyncio", [t.name for t in threading.enumerate() if t.is_alive()])


if __name__ == "__main__":
    unittest.main()
