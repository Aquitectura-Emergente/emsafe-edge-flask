"""Contrato MQTT: el código genera exactamente los ejemplos del README.

Lee los bloques ```json de la sección "Contrato MQTT" del README.md. Es la
copia versionada del contrato: `docs/contrato-mqtt.md` queda solo en local y
el CI no lo tiene. Orden de los bloques en el README:
  0 telemetría · 1 estado del dispositivo · 2 config · 3 Edge ONLINE · 4 Last Will

Si cambias un ejemplo del README o el serializador, cambia el otro: es el
idioma que comparten el Edge y la ingesta del backend.
"""
import json
import os
import re
import unittest
from datetime import datetime, timezone

from monitoring.domain.entities import OutboxMessage
from monitoring.domain.services import ReadingFactory
from monitoring.infrastructure import ble_protocol
from monitoring.infrastructure import mqtt_payloads as mp

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
README = os.path.join(ROOT, "README.md")


def contract_examples():
    with open(README, encoding="utf-8") as f:
        text = f.read()
    match = re.search(r"^## Contrato MQTT\s*$(.*?)(?=^## |\Z)", text, re.S | re.M)
    if not match:
        raise AssertionError("README.md no tiene la sección '## Contrato MQTT'")
    blocks = re.findall(r"```json\s*\n(.*?)```", match.group(1), re.S)
    return [json.loads(b) for b in blocks]


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def readme_example_message():
    """El escenario del ejemplo de telemetría, armado desde el dato BLE crudo."""
    captured = utc(2026, 11, 12, 15, 4, 4)
    raw_bytes = ble_protocol.encode_reading(
        ble_protocol.RawReading(seq=1234, ts=int(captured.timestamp()), uT=219.5, deferred=0)
    )
    raw = ble_protocol.decode_reading(raw_bytes)
    reading = ReadingFactory(saturation_ut=790.0).from_raw(
        "EMSafe-DOS-01", raw.seq, raw.ts, raw.uT, raw.deferred,
        received_at=utc(2026, 11, 12, 15, 4, 4, 380000),  # la fracción no viaja
    )
    return OutboxMessage("6f1c2a9e-8d2b-4c7e-9a51-2f0b7c3d4e10", (reading,))


def readme_example_payload():
    return mp.telemetry_payload(
        readme_example_message(),
        organization_id="org-demo",
        edge_node_id="EDGE-HOGAR-01",
        published_at=utc(2026, 11, 12, 15, 4, 5, 900000),
    )


class ReadmeSectionTest(unittest.TestCase):
    def test_la_seccion_tiene_los_cinco_ejemplos(self):
        self.assertEqual(len(contract_examples()), 5)


class TelemetryContractTest(unittest.TestCase):
    def test_payload_identico_al_ejemplo_del_readme(self):
        expected = contract_examples()[0]
        actual = readme_example_payload()

        self.assertEqual(actual, expected)
        # Mismo orden de campos que el ejemplo: el JSON publicado se lee igual.
        self.assertEqual(list(actual), list(expected))
        self.assertEqual(list(actual["readings"][0]), list(expected["readings"][0]))

    def test_el_json_publicado_vuelve_a_dar_el_ejemplo(self):
        wire = mp.to_json(readme_example_payload())
        self.assertIsInstance(wire, bytes)
        self.assertEqual(json.loads(wire.decode("utf-8")), contract_examples()[0])

    def test_lectura_sin_hora_y_diferida_saturada(self):
        reading = ReadingFactory(790.0).from_raw(
            "EMSafe-DOS-01", 7, 0, 812.25, 1, received_at=utc(2026, 11, 12, 15, 0, 0, 999999)
        )
        payload = mp.telemetry_payload(
            OutboxMessage("m-1", (reading,)), organization_id="org-demo",
            edge_node_id="EDGE-HOGAR-01", published_at=utc(2026, 11, 12, 15, 0, 1),
        )

        self.assertEqual(payload["readings"][0], {
            "sequence": 7,
            "capturedAt": "2026-11-12T15:00:00Z",
            "capturedAtSource": "EDGE_RECEIVED",
            "receivedAt": "2026-11-12T15:00:00Z",
            "magneticFluxDensityUT": 812.25,
            "origin": "DEFERRED",
            "quality": "SATURATED",
        })

    def test_to_json_no_admite_nan(self):
        with self.assertRaises(ValueError):
            mp.to_json({"x": float("nan")})


class StatusContractTest(unittest.TestCase):
    def test_estado_del_dispositivo_identico_al_readme(self):
        actual = mp.device_status_payload(
            "EMSafe-DOS-01", "EDGE-HOGAR-01", "ONLINE", utc(2026, 11, 12, 15, 0, 0)
        )
        expected = contract_examples()[1]
        self.assertEqual(actual, expected)
        self.assertEqual(list(actual), list(expected))

    def test_connectivity_invalida(self):
        with self.assertRaises(ValueError):
            mp.device_status_payload("D", "E", "MAYBE", utc(2026, 1, 1))

    def test_edge_online_identico_al_readme(self):
        self.assertEqual(mp.edge_online_payload(utc(2026, 11, 12, 15, 0, 0)), contract_examples()[3])

    def test_last_will_identico_al_readme_y_sin_changed_at(self):
        lwt = mp.edge_last_will_payload()
        self.assertEqual(lwt, contract_examples()[4])
        self.assertNotIn("changedAt", lwt)


class ConfigContractTest(unittest.TestCase):
    def test_el_ejemplo_de_config_del_readme_se_interpreta(self):
        raw = json.dumps(contract_examples()[2]).encode("utf-8")

        config = mp.parse_device_config("EMSafe-DOS-01", raw, saturation_ut=790.0)

        self.assertEqual(config.config_version, 3)
        self.assertEqual(config.alert_threshold_ut, 300.0)
        self.assertEqual(config.defined_by, "SSO_RESPONSIBLE")
        self.assertEqual(config.updated_at, utc(2026, 11, 12, 14, 0, 0))


class TopicsTest(unittest.TestCase):
    def test_topicos_del_contrato(self):
        self.assertEqual(mp.telemetry_topic("EMSafe-DOS-01"), "iot/EMSafe-DOS-01/telemetry")
        self.assertEqual(mp.device_status_topic("EMSafe-DOS-01"), "iot/EMSafe-DOS-01/status")
        self.assertEqual(mp.device_config_topic("EMSafe-DOS-01"), "iot/EMSafe-DOS-01/config")
        self.assertEqual(mp.edge_status_topic("EDGE-HOGAR-01"), "edge/EDGE-HOGAR-01/status")


class IsoFormatTest(unittest.TestCase):
    def test_trunca_la_fraccion_y_usa_z(self):
        self.assertEqual(mp.iso_utc(utc(2026, 11, 12, 15, 4, 4, 999999)), "2026-11-12T15:04:04Z")

    def test_rechaza_horas_sin_zona(self):
        with self.assertRaises(ValueError):
            mp.iso_utc(datetime(2026, 11, 12, 15, 4, 4))


if __name__ == "__main__":
    unittest.main()
