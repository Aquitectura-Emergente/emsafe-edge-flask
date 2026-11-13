"""DeviceConfig: el umbral de la nube, validado y versionado."""
import unittest
from datetime import datetime, timedelta, timezone

from monitoring.domain.entities import DeviceConfig, InvalidDeviceConfig

DEVICE = "EMSafe-DOS-01"
SAT = 790.0


def config(version=3, threshold=300.0, device=DEVICE, **kw):
    return DeviceConfig.create(device, version, threshold, saturation_ut=SAT, **kw)


class ValidationTest(unittest.TestCase):
    def test_config_valida(self):
        c = config(updated_at=datetime(2026, 11, 12, 14, 0, tzinfo=timezone.utc))

        self.assertEqual(c.device_id, DEVICE)
        self.assertEqual(c.config_version, 3)
        self.assertEqual(c.alert_threshold_ut, 300.0)
        self.assertEqual(c.defined_by, "SSO_RESPONSIBLE")

    def test_umbral_entero_se_normaliza_a_float(self):
        self.assertIsInstance(config(threshold=250).alert_threshold_ut, float)

    def test_umbral_cero_o_negativo_se_rechaza(self):
        for t in (0, -10.0):
            with self.assertRaises(InvalidDeviceConfig, msg=t):
                config(threshold=t)

    def test_umbral_en_o_sobre_la_saturacion_se_rechaza(self):
        # Nunca dispararía: el sensor no mide por encima de la saturación.
        for t in (790.0, 800.0):
            with self.assertRaises(InvalidDeviceConfig, msg=t):
                config(threshold=t)
        self.assertEqual(config(threshold=789.9).alert_threshold_ut, 789.9)

    def test_umbral_no_numerico_nan_o_infinito_se_rechaza(self):
        for t in ("300", None, True, float("nan"), float("inf")):
            with self.assertRaises(InvalidDeviceConfig, msg=repr(t)):
                config(threshold=t)

    def test_version_invalida(self):
        for v in (-1, 1.5, "3", None, True):
            with self.assertRaises(InvalidDeviceConfig, msg=repr(v)):
                config(version=v)

    def test_device_id_vacio(self):
        with self.assertRaises(InvalidDeviceConfig):
            config(device="")

    def test_updated_at_debe_estar_en_utc(self):
        with self.assertRaises(InvalidDeviceConfig):
            config(updated_at=datetime(2026, 11, 12, 14, 0))
        with self.assertRaises(InvalidDeviceConfig):
            config(updated_at=datetime(2026, 11, 12, 9, 0, tzinfo=timezone(timedelta(hours=-5))))

    def test_invalid_device_config_es_value_error(self):
        self.assertTrue(issubclass(InvalidDeviceConfig, ValueError))


class SupersedesTest(unittest.TestCase):
    def test_sin_config_vigente_cualquier_valida_aplica(self):
        self.assertTrue(config(version=0).supersedes(None))

    def test_version_mayor_reemplaza(self):
        self.assertTrue(config(version=4).supersedes(config(version=3)))

    def test_version_igual_o_menor_se_ignora(self):
        # Retained y QoS 1 pueden repetir o desordenar mensajes.
        current = config(version=3)
        self.assertFalse(config(version=3, threshold=200.0).supersedes(current))
        self.assertFalse(config(version=2).supersedes(current))

    def test_no_compara_dispositivos_distintos(self):
        with self.assertRaises(ValueError):
            config(version=4).supersedes(config(version=3, device="EMSafe-DOS-02"))


if __name__ == "__main__":
    unittest.main()
