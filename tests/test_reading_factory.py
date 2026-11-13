"""ReadingFactory: del dato crudo del BLE a una lectura validada."""
import unittest
from datetime import datetime, timedelta, timezone

from monitoring.domain.entities import Reading
from monitoring.domain.services import ReadingFactory
from monitoring.domain.value_objects import (
    CapturedAtSource,
    InvalidReading,
    MagneticFluxDensity,
    ReadingIdentity,
    ReadingQuality,
    SyncOrigin,
)

DEVICE = "EMSafe-DOS-01"
RECEIVED = datetime(2026, 11, 12, 15, 4, 4, 250000, tzinfo=timezone.utc)
TS = int(datetime(2026, 11, 12, 15, 4, 3, tzinfo=timezone.utc).timestamp())


def build(**overrides):
    args = dict(device_id=DEVICE, sequence=1234, ts=TS, value=219.5, deferred=0, received_at=RECEIVED)
    args.update(overrides)
    return ReadingFactory(saturation_ut=790.0).from_raw(**args)


class CapturedAtTest(unittest.TestCase):
    def test_con_hora_del_dosimetro_usa_ts_en_utc(self):
        r = build()

        self.assertEqual(r.captured_at, datetime(2026, 11, 12, 15, 4, 3, tzinfo=timezone.utc))
        self.assertEqual(r.captured_at.utcoffset(), timedelta(0))
        self.assertIs(r.captured_at_source, CapturedAtSource.DEVICE_CLOCK)
        self.assertEqual(r.received_at, RECEIVED)

    def test_sin_hora_usa_la_recepcion_y_lo_marca(self):
        r = build(ts=0)

        self.assertEqual(r.captured_at, RECEIVED)
        self.assertIs(r.captured_at_source, CapturedAtSource.EDGE_RECEIVED)

    def test_la_identidad_conserva_el_ts_crudo_no_captured_at(self):
        # Con ts = 0 la identidad no depende de la recepción,
        # así un reenvío del buffer tiene la misma identidad.
        first = build(ts=0, received_at=RECEIVED)
        resent = build(ts=0, received_at=RECEIVED + timedelta(minutes=3))

        self.assertEqual(first.identity, ReadingIdentity(DEVICE, 1234, 0))
        self.assertEqual(first.identity, resent.identity)
        self.assertNotEqual(first.captured_at, resent.captured_at)


class OriginAndQualityTest(unittest.TestCase):
    def test_deferred_1_es_diferida(self):
        self.assertIs(build(deferred=1).origin, SyncOrigin.DEFERRED)
        self.assertTrue(build(deferred=1).is_deferred)

    def test_deferred_0_es_tiempo_real(self):
        self.assertIs(build(deferred=0).origin, SyncOrigin.REALTIME)

    def test_saturacion_en_el_borde(self):
        self.assertIs(build(value=789.9).quality, ReadingQuality.VALID)
        self.assertIs(build(value=790.0).quality, ReadingQuality.SATURATED)

    def test_saturacion_configurable(self):
        r = ReadingFactory(saturation_ut=500.0).from_raw(DEVICE, 1, TS, 500.0, 0, RECEIVED)
        self.assertIs(r.quality, ReadingQuality.SATURATED)

    def test_saturada_se_construye_igual_porque_si_se_publica(self):
        r = build(value=950.0)
        self.assertEqual(r.flux, MagneticFluxDensity(950.0))


class ValidationTest(unittest.TestCase):
    def test_valores_invalidos_no_producen_lectura(self):
        for value in (float("nan"), float("inf"), -1.0, "x", None):
            with self.assertRaises(InvalidReading, msg=repr(value)):
                build(value=value)

    def test_device_id_vacio(self):
        with self.assertRaises(InvalidReading):
            build(device_id="")

    def test_hora_de_recepcion_sin_zona_se_rechaza(self):
        with self.assertRaises(ValueError):
            build(received_at=datetime(2026, 11, 12, 15, 4, 4))

    def test_hora_de_recepcion_en_otra_zona_se_rechaza(self):
        lima = timezone(timedelta(hours=-5))
        with self.assertRaises(ValueError):
            build(received_at=datetime(2026, 11, 12, 10, 4, 4, tzinfo=lima))

    def test_saturacion_debe_ser_positiva(self):
        with self.assertRaises(ValueError):
            ReadingFactory(saturation_ut=0)


class ReadingEntityTest(unittest.TestCase):
    def test_atajos_de_identidad(self):
        r = build()
        self.assertIsInstance(r, Reading)
        self.assertEqual(r.device_id, DEVICE)
        self.assertEqual(r.sequence, 1234)

    def test_es_inmutable(self):
        r = build()
        with self.assertRaises(AttributeError):
            r.quality = ReadingQuality.SATURATED


if __name__ == "__main__":
    unittest.main()
