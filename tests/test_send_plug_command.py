"""Script de prueba manual: la orden que genera cumple el contrato que acepta el Edge."""
import importlib.util
import json
import os
import unittest
from datetime import datetime, timedelta, timezone

from monitoring.infrastructure.mqtt_payloads import parse_plug_command

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "send_plug_command", os.path.join(ROOT, "tools", "send_plug_command.py")
)
tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tool)


class BuildCommandTest(unittest.TestCase):
    def test_la_orden_generada_la_acepta_el_edge(self):
        now = datetime(2026, 11, 13, 10, 10, 30, 500000, tzinfo=timezone(timedelta(hours=-5)))
        data = tool.build_command("off", "EMSafe-PLUG-01", now)

        self.assertEqual(data["requestedAt"], "2026-11-13T15:10:30Z")
        command = parse_plug_command("EMSafe-PLUG-01", json.dumps(data).encode())
        self.assertEqual(command.action.value, "OFF")
        self.assertEqual(command.command_id, data["commandId"])

    def test_cada_orden_tiene_un_command_id_nuevo(self):
        a = tool.build_command("ON", "P")["commandId"]
        b = tool.build_command("ON", "P")["commandId"]
        self.assertNotEqual(a, b)

    def test_accion_invalida(self):
        with self.assertRaises(ValueError):
            tool.build_command("TOGGLE", "P")


if __name__ == "__main__":
    unittest.main()
