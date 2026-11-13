"""Adaptador HTTP del actuador, con un `get` falso."""
import unittest

from monitoring.infrastructure.http_actuator import HttpActuatorGateway


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class FakeGet:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def __call__(self, url, timeout):
        self.calls.append((url, timeout))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class HttpActuatorTest(unittest.TestCase):
    def test_off_y_on_llaman_a_las_rutas_del_firmware(self):
        get = FakeGet(FakeResponse(200))
        actuator = HttpActuatorGateway("http://172.20.10.5/", timeout_s=2, get=get)

        self.assertTrue(actuator.turn_off())
        self.assertTrue(actuator.turn_on())
        self.assertEqual(get.calls, [("http://172.20.10.5/off", 2), ("http://172.20.10.5/on", 2)])
        self.assertTrue(actuator.last_ok)

    def test_sin_respuesta_registra_y_devuelve_false(self):
        actuator = HttpActuatorGateway("http://172.20.10.5", get=FakeGet(OSError("timeout")))
        with self.assertLogs("monitoring.infrastructure.http_actuator", level="WARNING"):
            self.assertFalse(actuator.turn_off())
        self.assertFalse(actuator.last_ok)
        self.assertIn("timeout", actuator.last_error)

    def test_http_de_error_cuenta_como_fallo(self):
        actuator = HttpActuatorGateway("http://x", get=FakeGet(FakeResponse(500)))
        with self.assertLogs("monitoring.infrastructure.http_actuator", level="WARNING"):
            self.assertFalse(actuator.turn_on())
        self.assertIn("HTTP 500", actuator.last_error)


if __name__ == "__main__":
    unittest.main()
