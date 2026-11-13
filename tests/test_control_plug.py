"""Reglas del enchufe: corte en DANGER, histéresis, US05, diferidas y orden del usuario."""
import unittest

from monitoring.application.control_plug import ControlPlugUseCase
from monitoring.domain.services import ReadingFactory
from monitoring.domain.value_objects import AlertThresholds
from tests.fakes import T0, FakeClock

DEVICE = "EMSafe-DOS-01"
LOGGER = "monitoring.application.control_plug"


class FakeActuator:
    def __init__(self, ok=True):
        self.ok = ok
        self.calls = []

    def turn_off(self):
        self.calls.append("off")
        return self.ok

    def turn_on(self):
        self.calls.append("on")
        return self.ok


class FakeDesired:
    def __init__(self, value=None):
        self.value = value
        self.calls = 0

    def fetch_desired_plug(self, _device_id):
        self.calls += 1
        return self.value


class Sensor:
    """Genera lecturas como las del firmware."""

    def __init__(self, thresholds=AlertThresholds(100, 200)):
        self.factory = ReadingFactory(790.0)
        self.thresholds = thresholds
        self.seq = 0

    def __call__(self, value, deferred=0):
        self.seq += 1
        return self.factory.from_raw(DEVICE, self.seq, 0, value, deferred, T0, self.thresholds)


def build(actuator=None, desired=None, thresholds=AlertThresholds(100, 200), **kwargs):
    actuator = actuator if actuator is not None else FakeActuator()
    clock = FakeClock()
    use_case = ControlPlugUseCase(
        actuator, desired, clock, lambda _d: thresholds, **kwargs
    )
    return use_case, actuator, clock, Sensor(thresholds)


class AutoCutoffTest(unittest.TestCase):
    def test_danger_en_tiempo_real_corta_una_sola_vez(self):
        use_case, actuator, _, r = build()

        self.assertEqual(use_case.evaluate(r(250)), "OFF")
        self.assertIsNone(use_case.evaluate(r(260)))
        self.assertEqual(actuator.calls, ["off"])

    def test_saturada_corta(self):
        use_case, actuator, _, r = build()
        use_case.evaluate(r(800))
        self.assertEqual(actuator.calls, ["off"])

    def test_safe_o_caution_sin_corte_previo_no_hacen_nada(self):
        use_case, actuator, _, r = build()
        for value in (10, 150, 199):
            self.assertIsNone(use_case.evaluate(r(value)))
        self.assertEqual(actuator.calls, [])

    def test_histeresis_restablece_bajo_el_80_por_ciento(self):
        use_case, actuator, _, r = build()
        use_case.evaluate(r(250))

        self.assertIsNone(use_case.evaluate(r(180)))  # CAUTION, sobre 160
        self.assertIsNone(use_case.evaluate(r(160)))  # en el borde todavía no
        self.assertEqual(use_case.evaluate(r(159.9)), "ON")
        self.assertEqual(actuator.calls, ["off", "on"])

    def test_umbral_de_restablecimiento_configurado(self):
        use_case, actuator, _, r = build(restore_ut=50)
        use_case.evaluate(r(250))
        self.assertIsNone(use_case.evaluate(r(100)))
        self.assertEqual(use_case.evaluate(r(49)), "ON")
        self.assertEqual(use_case.restore_threshold(DEVICE), 50)

    def test_restablecimiento_sigue_al_danger_vigente(self):
        use_case, _, _, _ = build(thresholds=AlertThresholds(100, 300))
        self.assertAlmostEqual(use_case.restore_threshold(DEVICE), 240.0)
        # Un EMSAFE_RESTORE_UT que ya no queda bajo DANGER se reemplaza por el 80 %.
        use_case, _, _, _ = build(thresholds=AlertThresholds(50, 100), restore_ut=150)
        self.assertAlmostEqual(use_case.restore_threshold(DEVICE), 80.0)

    def test_diferidas_nunca_accionan(self):
        use_case, actuator, _, r = build()
        self.assertIsNone(use_case.evaluate(r(500, deferred=1)))
        use_case.evaluate(r(250))
        self.assertIsNone(use_case.evaluate(r(5, deferred=1)))
        self.assertEqual(actuator.calls, ["off"])

    def test_us05_desactivado_no_corta(self):
        use_case, actuator, _, r = build(auto_cutoff_enabled=False)
        self.assertIsNone(use_case.evaluate(r(500)))
        self.assertEqual(actuator.calls, [])
        self.assertFalse(use_case.status()["autoCutoffEnabled"])

    def test_sin_actuador_no_hace_nada(self):
        use_case = ControlPlugUseCase(None, None, FakeClock(), lambda _d: AlertThresholds(100, 200))
        self.assertIsNone(use_case.evaluate(Sensor()(500)))
        self.assertFalse(use_case.status()["enabled"])


class ActuatorFailureTest(unittest.TestCase):
    def test_fallo_se_registra_y_se_reintenta_con_la_siguiente(self):
        actuator = FakeActuator(ok=False)
        use_case, _, _, r = build(actuator=actuator)

        with self.assertLogs(LOGGER, level="WARNING"):
            self.assertIsNone(use_case.evaluate(r(250)))
        self.assertIsNotNone(use_case.status()["lastError"])

        actuator.ok = True
        self.assertEqual(use_case.evaluate(r(250)), "OFF")
        self.assertIsNone(use_case.status()["lastError"])
        self.assertEqual(use_case.status()["plug"], "OFF")


class UserOrderTest(unittest.TestCase):
    def test_orden_on_gana_sobre_danger(self):
        use_case, actuator, _, r = build(desired=FakeDesired("ON"))
        self.assertEqual(use_case.evaluate(r(500)), "ON")
        self.assertIsNone(use_case.evaluate(r(600)))
        self.assertEqual(actuator.calls, ["on"])

    def test_orden_off_gana_aunque_el_campo_sea_seguro(self):
        use_case, actuator, _, r = build(desired=FakeDesired("off"))
        self.assertEqual(use_case.evaluate(r(5)), "OFF")
        self.assertEqual(actuator.calls, ["off"])

    def test_orden_off_vale_aunque_us05_este_desactivado(self):
        use_case, actuator, _, r = build(desired=FakeDesired("OFF"), auto_cutoff_enabled=False)
        self.assertEqual(use_case.evaluate(r(5)), "OFF")

    def test_corte_del_usuario_no_lo_restablece_la_logica_automatica(self):
        desired = FakeDesired("OFF")
        use_case, actuator, clock, r = build(desired=desired)
        use_case.evaluate(r(5))
        desired.value = None
        clock.advance(10)
        self.assertIsNone(use_case.evaluate(r(5)))
        self.assertEqual(actuator.calls, ["off"])

    def test_sin_orden_vuelve_la_logica_automatica(self):
        desired = FakeDesired("ON")
        use_case, actuator, clock, r = build(desired=desired)
        use_case.evaluate(r(500))
        desired.value = None
        clock.advance(10)
        self.assertEqual(use_case.evaluate(r(500)), "OFF")
        self.assertEqual(actuator.calls, ["on", "off"])

    def test_la_orden_se_consulta_cada_cierto_tiempo(self):
        desired = FakeDesired(None)
        use_case, _, clock, r = build(desired=desired, desired_poll_s=5)
        for _ in range(4):
            use_case.evaluate(r(10))
            clock.advance(1)
        self.assertEqual(desired.calls, 1)
        clock.advance(1)
        use_case.evaluate(r(10))
        self.assertEqual(desired.calls, 2)

    def test_las_diferidas_no_consultan_la_orden(self):
        desired = FakeDesired("OFF")
        use_case, actuator, _, r = build(desired=desired)
        self.assertIsNone(use_case.evaluate(r(5, deferred=1)))
        self.assertEqual((desired.calls, actuator.calls), (0, []))


class AccumulatedTest(unittest.TestCase):
    def test_entre_varias_acumuladas_manda_la_danger(self):
        use_case, actuator, _, r = build()
        self.assertEqual(use_case.evaluate_latest([r(10), r(250), r(20)]), "OFF")
        self.assertEqual(actuator.calls, ["off"])

    def test_sin_danger_decide_la_ultima(self):
        use_case, actuator, _, r = build()
        use_case.evaluate(r(250))
        self.assertEqual(use_case.evaluate_latest([r(190), r(10)]), "ON")

    def test_solo_diferidas_no_hacen_nada(self):
        use_case, actuator, _, r = build()
        self.assertIsNone(use_case.evaluate_latest([r(500, deferred=1)]))
        self.assertEqual(actuator.calls, [])


if __name__ == "__main__":
    unittest.main()
