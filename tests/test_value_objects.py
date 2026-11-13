"""Value objects: ninguno puede existir con un valor inválido."""
import unittest

from monitoring.domain.value_objects import (
    CapturedAtSource,
    DeviceType,
    InvalidReading,
    MagneticFluxDensity,
    ReadingIdentity,
    ReadingQuality,
    SyncOrigin,
)


class MagneticFluxDensityTest(unittest.TestCase):
    def test_acepta_cero_y_positivos(self):
        self.assertEqual(MagneticFluxDensity.from_raw(0).micro_tesla, 0.0)
        self.assertEqual(MagneticFluxDensity.from_raw(219.5).micro_tesla, 219.5)
        self.assertEqual(MagneticFluxDensity.from_raw("12.5").micro_tesla, 12.5)

    def test_normaliza_enteros_a_float(self):
        self.assertIsInstance(MagneticFluxDensity(300).micro_tesla, float)

    def test_rechaza_no_numericos(self):
        for raw in ("abc", None, [], object()):
            with self.assertRaises(InvalidReading, msg=repr(raw)):
                MagneticFluxDensity.from_raw(raw)

    def test_rechaza_booleanos_aunque_python_los_trate_como_int(self):
        with self.assertRaises(InvalidReading):
            MagneticFluxDensity.from_raw(True)
        with self.assertRaises(InvalidReading):
            MagneticFluxDensity(True)

    def test_rechaza_nan_e_infinitos(self):
        # Un float del BLE puede traer NaN o inf; JSON no los representa.
        for raw in (float("nan"), float("inf"), float("-inf"), "nan"):
            with self.assertRaises(InvalidReading, msg=repr(raw)):
                MagneticFluxDensity.from_raw(raw)

    def test_rechaza_negativos(self):
        with self.assertRaises(InvalidReading):
            MagneticFluxDensity.from_raw(-0.1)

    def test_invalid_reading_es_value_error(self):
        self.assertTrue(issubclass(InvalidReading, ValueError))


class SyncOriginTest(unittest.TestCase):
    def test_flag_1_es_diferida(self):
        self.assertIs(SyncOrigin.from_flag(1), SyncOrigin.DEFERRED)

    def test_cualquier_otro_flag_es_tiempo_real(self):
        for flag in (0, 2, 255):
            self.assertIs(SyncOrigin.from_flag(flag), SyncOrigin.REALTIME)


class ReadingQualityTest(unittest.TestCase):
    def test_borde_de_saturacion(self):
        sat = 790.0
        self.assertIs(ReadingQuality.classify(MagneticFluxDensity(789.9), sat), ReadingQuality.VALID)
        self.assertIs(ReadingQuality.classify(MagneticFluxDensity(790.0), sat), ReadingQuality.SATURATED)
        self.assertIs(ReadingQuality.classify(MagneticFluxDensity(1000.0), sat), ReadingQuality.SATURATED)


class ContractValuesTest(unittest.TestCase):
    """Los valores de los enums viajan tal cual en el contrato MQTT."""

    def test_valores_literales_del_contrato(self):
        self.assertEqual({e.value for e in SyncOrigin}, {"REALTIME", "DEFERRED"})
        self.assertEqual({e.value for e in ReadingQuality}, {"VALID", "SATURATED"})
        self.assertEqual({e.value for e in CapturedAtSource}, {"DEVICE_CLOCK", "EDGE_RECEIVED"})
        self.assertEqual({e.value for e in DeviceType}, {"PERSONAL_DOSIMETER", "FIXED_SENSOR"})


class ReadingIdentityTest(unittest.TestCase):
    def test_misma_terna_es_la_misma_identidad(self):
        a = ReadingIdentity("EMSafe-DOS-01", 10, 1700000000)
        b = ReadingIdentity("EMSafe-DOS-01", 10, 1700000000)
        self.assertEqual(a, b)
        self.assertEqual(hash(a), hash(b))

    def test_cambia_cualquier_componente_cambia_la_identidad(self):
        base = ReadingIdentity("EMSafe-DOS-01", 10, 1700000000)
        self.assertNotEqual(base, ReadingIdentity("EMSafe-DOS-02", 10, 1700000000))
        self.assertNotEqual(base, ReadingIdentity("EMSafe-DOS-01", 11, 1700000000))
        # seq se reinicia con el ESP32: mismo seq, otra hora, otra lectura.
        self.assertNotEqual(base, ReadingIdentity("EMSafe-DOS-01", 10, 1700000999))

    def test_ts_cero_es_valido_y_marca_que_no_hay_reloj(self):
        identity = ReadingIdentity("EMSafe-DOS-01", 1, 0)
        self.assertFalse(identity.has_device_clock)
        self.assertTrue(ReadingIdentity("EMSafe-DOS-01", 1, 5).has_device_clock)

    def test_ts_cero_con_mismo_seq_colisiona_limitacion_conocida(self):
        # Documentado en el contrato: dos arranques sin hora colisionan
        # hasta que el firmware exponga un bootId.
        self.assertEqual(
            ReadingIdentity("EMSafe-DOS-01", 1, 0), ReadingIdentity("EMSafe-DOS-01", 1, 0)
        )

    def test_rechaza_componentes_invalidos(self):
        with self.assertRaises(InvalidReading):
            ReadingIdentity("", 1, 0)
        with self.assertRaises(InvalidReading):
            ReadingIdentity("EMSafe-DOS-01", -1, 0)
        with self.assertRaises(InvalidReading):
            ReadingIdentity("EMSafe-DOS-01", 1, -5)
        with self.assertRaises(InvalidReading):
            ReadingIdentity("EMSafe-DOS-01", 1.5, 0)
        with self.assertRaises(InvalidReading):
            ReadingIdentity("EMSafe-DOS-01", True, 0)


if __name__ == "__main__":
    unittest.main()
