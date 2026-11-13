"""Clasificación local SAFE / CAUTION / DANGER con umbrales configurables."""
import unittest

from monitoring.application.ingest_reading import IngestReadingUseCase, IngestResult
from monitoring.domain.services import ReadingFactory
from monitoring.domain.value_objects import (
    AlertLevel,
    AlertThresholds,
    MagneticFluxDensity,
    ReadingQuality,
)
from tests.fakes import T0, FakeClock, FakeOutbox

DEVICE = "EMSafe-DOS-01"


def level(value, thresholds=AlertThresholds(100, 200), quality=ReadingQuality.VALID):
    return AlertLevel.classify(MagneticFluxDensity(value), quality, thresholds)


class ClassifyTest(unittest.TestCase):
    def test_bordes_inclusivos_por_abajo(self):
        self.assertIs(level(0), AlertLevel.SAFE)
        self.assertIs(level(99.9), AlertLevel.SAFE)
        self.assertIs(level(100), AlertLevel.CAUTION)
        self.assertIs(level(199.9), AlertLevel.CAUTION)
        self.assertIs(level(200), AlertLevel.DANGER)

    def test_usa_los_umbrales_configurados(self):
        t = AlertThresholds(50, 80)
        self.assertIs(level(49, t), AlertLevel.SAFE)
        self.assertIs(level(50, t), AlertLevel.CAUTION)
        self.assertIs(level(80, t), AlertLevel.DANGER)

    def test_saturada_es_danger_aunque_el_umbral_sea_mayor(self):
        self.assertIs(level(10, quality=ReadingQuality.SATURATED), AlertLevel.DANGER)

    def test_danger_recomienda_cortar(self):
        self.assertTrue(AlertLevel.DANGER.requires_cut_off)
        self.assertFalse(AlertLevel.CAUTION.requires_cut_off)
        self.assertEqual(AlertLevel.CAUTION.wire_value, "CAUTION")
        self.assertTrue(AlertLevel.SAFE.message)


class ThresholdsTest(unittest.TestCase):
    def test_validacion(self):
        for caution, danger in ((0, 200), (-1, 200), (300, 200), (100, float("nan")), ("a", 200)):
            with self.assertRaises(ValueError, msg=(caution, danger)):
                AlertThresholds(caution, danger)

    def test_caution_igual_a_danger_se_permite(self):
        self.assertIs(level(150, AlertThresholds(150, 150)), AlertLevel.DANGER)

    def test_nuevo_danger_desde_la_nube(self):
        self.assertEqual(AlertThresholds(100, 200).with_danger(300), AlertThresholds(100, 300))
        # Un DANGER por debajo de CAUTION arrastra a CAUTION.
        self.assertEqual(AlertThresholds(100, 200).with_danger(60), AlertThresholds(60, 60))


class FactoryAndIngestTest(unittest.TestCase):
    def test_la_fabrica_clasifica_con_los_umbrales_dados(self):
        factory = ReadingFactory(790.0)
        r = factory.from_raw(DEVICE, 1, 0, 150.0, 0, T0, AlertThresholds(100, 140))
        self.assertIs(r.level, AlertLevel.DANGER)
        self.assertIs(factory.from_raw(DEVICE, 2, 0, 150.0, 0, T0).level, AlertLevel.CAUTION)

    def test_la_ingesta_usa_los_umbrales_vigentes_y_avisa_lo_guardado(self):
        outbox, stored = FakeOutbox(), []
        current = {"t": AlertThresholds(100, 200)}
        use_case = IngestReadingUseCase(
            outbox, ReadingFactory(790.0), FakeClock(),
            thresholds_for=lambda _d: current["t"], on_stored=stored.append,
        )

        use_case.ingest(DEVICE, 1, 0, 150.0, 0)
        current["t"] = AlertThresholds(100, 120)
        use_case.ingest(DEVICE, 2, 0, 150.0, 0)
        self.assertIs(use_case.ingest(DEVICE, 2, 0, 150.0, 0), IngestResult.DUPLICATE)
        use_case.ingest(DEVICE, 3, 0, -1.0, 0)

        self.assertEqual([r.level for r in stored], [AlertLevel.CAUTION, AlertLevel.DANGER])
        self.assertEqual([e.reading.level for e in outbox.entries], [AlertLevel.CAUTION, AlertLevel.DANGER])


if __name__ == "__main__":
    unittest.main()
