"""Configuración del dosímetro: validación, versiones y escritura del umbral."""
import asyncio
import json
import unittest
from datetime import datetime, timezone
from functools import partial

from monitoring.application.apply_device_config import ApplyDeviceConfigUseCase, ConfigResult
from monitoring.domain.entities import DeviceConfig, InvalidDeviceConfig
from monitoring.infrastructure.mqtt_payloads import parse_device_config
from tests.fakes import FakeConfigStore, FakeLink

DEVICE = "EMSafe-DOS-01"
LOGGER = "monitoring.application.apply_device_config"
parser = partial(parse_device_config, saturation_ut=790.0)


def message(**overrides):
    data = {
        "schemaVersion": "1.0",
        "deviceId": DEVICE,
        "configVersion": 3,
        "alertThresholdUT": 300.0,
        "definedBy": "SSO_RESPONSIBLE",
        "updatedAt": "2026-11-12T14:00:00Z",
    }
    data.update(overrides)
    return json.dumps(data).encode("utf-8")


def build(connected=(), fail=False):
    store, link = FakeConfigStore(), FakeLink(connected=connected, fail=fail)
    use_case = ApplyDeviceConfigUseCase(store, link, parser, default_threshold_ut=300.0)
    return use_case, store, link


def apply(use_case, raw, device=DEVICE):
    return asyncio.run(use_case.on_config_message(device, raw))


class ParseTest(unittest.TestCase):
    """Parser del contrato (README, config)."""

    def test_ejemplo_del_contrato(self):
        c = parser(DEVICE, message())
        self.assertEqual(c, DeviceConfig(DEVICE, 3, 300.0, "SSO_RESPONSIBLE",
                                         datetime(2026, 11, 12, 14, 0, tzinfo=timezone.utc)))

    def test_acepta_texto_y_campos_desconocidos(self):
        c = parser(DEVICE, message(samplingFrequencyMs=1000).decode("utf-8"))
        self.assertEqual(c.config_version, 3)

    def test_updated_at_y_defined_by_son_opcionales(self):
        raw = json.dumps({"schemaVersion": "1.0", "deviceId": DEVICE,
                          "configVersion": 1, "alertThresholdUT": 250}).encode()
        c = parser(DEVICE, raw)
        self.assertIsNone(c.updated_at)
        self.assertEqual(c.defined_by, "SSO_RESPONSIBLE")

    def test_updated_at_con_otra_zona_se_pasa_a_utc(self):
        c = parser(DEVICE, message(updatedAt="2026-11-12T09:00:00-05:00"))
        self.assertEqual(c.updated_at, datetime(2026, 11, 12, 14, 0, tzinfo=timezone.utc))

    def test_acepta_versiones_menores_del_mismo_esquema(self):
        self.assertEqual(parser(DEVICE, message(schemaVersion="1.3")).config_version, 3)

    def test_rechazos(self):
        cases = {
            "json roto": b"{no es json",
            "no es objeto": b"[1, 2]",
            "bytes no utf8": b"\xff\xfe",
            "versión mayor": message(schemaVersion="2.0"),
            "sin schemaVersion": json.dumps({"deviceId": DEVICE, "configVersion": 1,
                                             "alertThresholdUT": 300}).encode(),
            "otro dispositivo": message(deviceId="EMSafe-DOS-02"),
            "umbral cero": message(alertThresholdUT=0),
            "umbral en saturación": message(alertThresholdUT=790),
            "umbral texto": message(alertThresholdUT="300"),
            "versión negativa": message(configVersion=-1),
            "versión texto": message(configVersion="3"),
            "fecha rota": message(updatedAt="ayer"),
            "fecha sin zona": message(updatedAt="2026-11-12T14:00:00"),
        }
        for name, raw in cases.items():
            with self.assertRaises(InvalidDeviceConfig, msg=name):
                parser(DEVICE, raw)


class ApplyTest(unittest.TestCase):
    def test_primera_config_se_guarda(self):
        use_case, store, _ = build()

        self.assertIs(apply(use_case, message()), ConfigResult.APPLIED)
        self.assertEqual(store.get(DEVICE).alert_threshold_ut, 300.0)

    def test_version_mayor_reemplaza(self):
        use_case, store, _ = build()
        apply(use_case, message(configVersion=3, alertThresholdUT=300))

        self.assertIs(apply(use_case, message(configVersion=4, alertThresholdUT=200)), ConfigResult.APPLIED)
        self.assertEqual(store.get(DEVICE).alert_threshold_ut, 200.0)

    def test_version_igual_o_menor_se_ignora(self):
        use_case, store, _ = build()
        apply(use_case, message(configVersion=3, alertThresholdUT=300))

        self.assertIs(apply(use_case, message(configVersion=3, alertThresholdUT=100)), ConfigResult.IGNORED)
        self.assertIs(apply(use_case, message(configVersion=2, alertThresholdUT=100)), ConfigResult.IGNORED)
        self.assertEqual(store.get(DEVICE).alert_threshold_ut, 300.0)
        self.assertEqual(store.saves, 1)

    def test_invalida_se_rechaza_con_log_y_conserva_la_vigente(self):
        use_case, store, link = build(connected=[DEVICE])
        apply(use_case, message(configVersion=3))

        with self.assertLogs(LOGGER, level="WARNING") as logs:
            result = apply(use_case, message(configVersion=9, alertThresholdUT=900))

        self.assertIs(result, ConfigResult.REJECTED)
        self.assertIn("rechazada", logs.output[0])
        self.assertEqual(store.get(DEVICE).config_version, 3)
        self.assertEqual(link.writes, [(DEVICE, 300.0)])  # solo la válida

    def test_config_de_otro_dispositivo_en_el_topico_equivocado(self):
        use_case, store, _ = build()
        with self.assertLogs(LOGGER, level="WARNING"):
            self.assertIs(apply(use_case, message(deviceId="EMSafe-DOS-02")), ConfigResult.REJECTED)
        self.assertIsNone(store.get(DEVICE))

    def test_retained_vacio_no_borra_la_config(self):
        use_case, store, _ = build()
        apply(use_case, message())

        self.assertIs(apply(use_case, b""), ConfigResult.CLEARED)
        self.assertEqual(store.get(DEVICE).config_version, 3)


class WriteToDosimeterTest(unittest.TestCase):
    def test_conectado_escribe_el_umbral_en_caliente(self):
        use_case, _, link = build(connected=[DEVICE])

        apply(use_case, message(alertThresholdUT=250))

        self.assertEqual(link.writes, [(DEVICE, 250.0)])

    def test_desconectado_no_escribe_pero_guarda(self):
        use_case, store, link = build(connected=[])

        apply(use_case, message(alertThresholdUT=250))

        self.assertEqual(link.writes, [])
        self.assertEqual(use_case.threshold_for(DEVICE), 250.0)  # se escribirá al conectar

    def test_ignorada_no_escribe(self):
        use_case, _, link = build(connected=[DEVICE])
        apply(use_case, message(configVersion=3))
        apply(use_case, message(configVersion=3, alertThresholdUT=100))

        self.assertEqual(link.writes, [(DEVICE, 300.0)])

    def test_fallo_de_escritura_ble_no_rompe_y_la_config_queda_guardada(self):
        use_case, store, _ = build(connected=[DEVICE], fail=True)

        with self.assertLogs(LOGGER, level="WARNING") as logs:
            result = apply(use_case, message())

        self.assertIs(result, ConfigResult.APPLIED)
        self.assertIn("al reconectar", logs.output[-1])
        self.assertIsNotNone(store.get(DEVICE))


class ThresholdForTest(unittest.TestCase):
    def test_sin_config_usa_el_umbral_de_danger_y_lo_informa_una_vez(self):
        use_case, _, _ = build()

        with self.assertLogs(LOGGER, level="INFO") as logs:
            self.assertEqual(use_case.threshold_for(DEVICE), 300.0)
        self.assertIn("EMSAFE_DANGER_UT", logs.output[0])

        with self.assertNoLogs(LOGGER, level="INFO"):
            self.assertEqual(use_case.threshold_for(DEVICE), 300.0)

    def test_con_config_usa_la_de_la_nube(self):
        use_case, _, _ = build()
        apply(use_case, message(alertThresholdUT=220))

        with self.assertNoLogs(LOGGER, level="WARNING"):
            self.assertEqual(use_case.threshold_for(DEVICE), 220.0)


class SqliteConfigStoreTest(unittest.TestCase):
    def setUp(self):
        from shared.database import init_db
        self.database = init_db(":memory:")

    def tearDown(self):
        self.database.close()

    def test_guarda_reemplaza_y_rehidrata(self):
        from monitoring.infrastructure.sqlite_config_store import SqliteConfigStore

        store = SqliteConfigStore()
        self.assertIsNone(store.get(DEVICE))

        first = parser(DEVICE, message(configVersion=3))
        store.save(first)
        self.assertEqual(store.get(DEVICE), first)

        second = parser(DEVICE, message(configVersion=4, alertThresholdUT=200, updatedAt=None))
        store.save(second)
        self.assertEqual(store.get(DEVICE), second)
        self.assertEqual(SqliteConfigStore().get(DEVICE).alert_threshold_ut, 200.0)

    def test_caso_de_uso_contra_sqlite(self):
        from monitoring.infrastructure.sqlite_config_store import SqliteConfigStore

        use_case = ApplyDeviceConfigUseCase(SqliteConfigStore(), FakeLink(), parser, 300.0)
        self.assertIs(apply(use_case, message(configVersion=5)), ConfigResult.APPLIED)
        self.assertIs(apply(use_case, message(configVersion=5)), ConfigResult.IGNORED)


if __name__ == "__main__":
    unittest.main()
