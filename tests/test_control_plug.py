"""Enchufe: órdenes del usuario por MQTT, estado publicado y corte automático opcional."""
import json
import unittest

from monitoring.application.control_plug import CommandResult, ControlPlugUseCase
from monitoring.domain.services import ReadingFactory
from monitoring.domain.value_objects import AlertThresholds, PlugState, PlugStateSource
from monitoring.infrastructure.mqtt_payloads import iso_utc, parse_plug_command
from tests.fakes import T0, FakeClock

DEVICE = "EMSafe-DOS-01"
PLUG = "EMSafe-PLUG-01"
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


class FakeStatePublisher:
    def __init__(self, connected=True):
        self.connected = connected
        self.states = []

    def publish_plug_state(self, plug_id, state, command_id, source, changed_at):
        if not self.connected:
            return False
        self.states.append((plug_id, state.value, command_id, source.value, changed_at))
        return True


class Sensor:
    """Genera lecturas como las del firmware."""

    def __init__(self, thresholds=AlertThresholds(100, 200)):
        self.factory = ReadingFactory(790.0)
        self.thresholds = thresholds
        self.seq = 0

    def __call__(self, value, deferred=0):
        self.seq += 1
        return self.factory.from_raw(DEVICE, self.seq, 0, value, deferred, T0, self.thresholds)


def command(action="OFF", command_id="cmd-1", requested_at=T0, **overrides):
    data = {
        "schemaVersion": "1.0",
        "commandId": command_id,
        "plugId": PLUG,
        "action": action,
        "requestedBy": "USER",
        "requestedAt": iso_utc(requested_at),
    }
    data.update(overrides)
    return json.dumps(data).encode("utf-8")


def build(actuator=None, thresholds=AlertThresholds(100, 200), auto=True, **kwargs):
    actuator = FakeActuator() if actuator is None else actuator
    clock, publisher = FakeClock(), FakeStatePublisher()
    use_case = ControlPlugUseCase(
        actuator, publisher, clock, lambda _d: thresholds, PLUG, parse_plug_command,
        auto_cutoff_enabled=auto, **kwargs
    )
    return use_case, actuator, publisher, clock, Sensor(thresholds)


class UserCommandTest(unittest.TestCase):
    def test_orden_acciona_y_publica_el_estado_con_su_command_id(self):
        use_case, actuator, publisher, clock, _ = build(auto=False)
        clock.advance(2)

        self.assertIs(use_case.handle_command(command("OFF", requested_at=T0)), CommandResult.APPLIED)

        self.assertEqual(actuator.calls, ["off"])
        self.assertEqual(publisher.states, [(PLUG, "OFF", "cmd-1", "USER", clock.now())])
        self.assertEqual(use_case.status()["state"], "OFF")

    def test_es_idempotente_por_command_id(self):
        use_case, actuator, publisher, _, _ = build()
        use_case.handle_command(command("OFF"))

        with self.assertLogs(LOGGER, level="INFO"):
            self.assertIs(use_case.handle_command(command("ON")), CommandResult.DUPLICATE)
        self.assertEqual(actuator.calls, ["off"])
        self.assertEqual(len(publisher.states), 1)

    def test_otra_orden_con_la_misma_accion_vuelve_a_accionar(self):
        use_case, actuator, publisher, _, _ = build()
        use_case.handle_command(command("OFF", "a"))
        use_case.handle_command(command("OFF", "b"))
        self.assertEqual(actuator.calls, ["off", "off"])
        self.assertEqual([s[2] for s in publisher.states], ["a", "b"])

    def test_ignora_ordenes_de_hace_mas_de_60_s(self):
        use_case, actuator, publisher, clock, _ = build()
        clock.advance(61)

        with self.assertLogs(LOGGER, level="WARNING"):
            self.assertIs(use_case.handle_command(command("OFF")), CommandResult.STALE)
        self.assertEqual((actuator.calls, publisher.states), ([], []))

    def test_60_s_justos_todavia_vale(self):
        use_case, _, _, clock, _ = build()
        clock.advance(60)
        self.assertIs(use_case.handle_command(command("OFF")), CommandResult.APPLIED)

    def test_ignora_ordenes_con_hora_muy_adelantada(self):
        use_case, actuator, _, clock, _ = build()
        clock.advance(-120)
        with self.assertLogs(LOGGER, level="WARNING"):
            self.assertIs(use_case.handle_command(command("OFF")), CommandResult.STALE)
        self.assertEqual(actuator.calls, [])

    def test_orden_vieja_repetida_sigue_siendo_duplicada(self):
        use_case, _, _, clock, _ = build()
        clock.advance(61)
        with self.assertLogs(LOGGER, level="WARNING"):
            use_case.handle_command(command("OFF"))
        with self.assertLogs(LOGGER, level="INFO"):
            self.assertIs(use_case.handle_command(command("OFF")), CommandResult.DUPLICATE)

    def test_ordenes_invalidas_se_rechazan_con_log(self):
        use_case, actuator, publisher, _, _ = build()
        for raw in (b"no json", command(action="TOGGLE"), command(plugId="OTRO"),
                    command(requestedBy="AUTO"), command(commandId=""),
                    command(schemaVersion="2.0"), command(requestedAt="ayer")):
            with self.subTest(raw=raw), self.assertLogs(LOGGER, level="WARNING"):
                self.assertIs(use_case.handle_command(raw), CommandResult.REJECTED)
        self.assertEqual((actuator.calls, publisher.states), ([], []))

    def test_actuador_caido_publica_el_estado_sin_cambios(self):
        actuator = FakeActuator(ok=False)
        use_case, _, publisher, clock, _ = build(actuator=actuator)

        with self.assertLogs(LOGGER, level="WARNING") as logs:
            self.assertIs(use_case.handle_command(command("OFF")), CommandResult.FAILED)

        self.assertIn("no respondió", logs.output[-1])
        self.assertEqual(publisher.states, [(PLUG, "UNKNOWN", "cmd-1", "USER", clock.now())])
        self.assertIsNotNone(use_case.status()["lastError"])

    def test_sin_actuador_configurado_tambien_publica_sin_cambios(self):
        clock, publisher = FakeClock(), FakeStatePublisher()
        use_case = ControlPlugUseCase(
            None, publisher, clock, lambda _d: AlertThresholds(100, 200), PLUG, parse_plug_command
        )
        with self.assertLogs(LOGGER, level="WARNING"):
            self.assertIs(use_case.handle_command(command("ON")), CommandResult.FAILED)
        self.assertEqual(publisher.states[0][1], "UNKNOWN")
        self.assertFalse(use_case.status()["enabled"])


class StateTest(unittest.TestCase):
    def test_al_arrancar_el_estado_es_unknown_por_startup(self):
        use_case, _, publisher, _, _ = build()
        self.assertTrue(use_case.publish_state())
        self.assertEqual(publisher.states, [(PLUG, "UNKNOWN", None, "STARTUP", T0)])

    def test_republica_el_ultimo_estado_sin_cambiar_su_hora(self):
        use_case, _, publisher, clock, _ = build()
        clock.advance(5)
        use_case.handle_command(command("OFF", requested_at=clock.now()))
        changed = clock.now()
        clock.advance(100)
        use_case.publish_state()
        self.assertEqual(publisher.states[-1], (PLUG, "OFF", "cmd-1", "USER", changed))

    def test_sin_broker_devuelve_false(self):
        use_case, _, publisher, _, _ = build()
        publisher.connected = False
        self.assertFalse(use_case.publish_state())


class AutoCutoffTest(unittest.TestCase):
    def test_desactivado_por_defecto(self):
        clock, publisher, actuator = FakeClock(), FakeStatePublisher(), FakeActuator()
        use_case = ControlPlugUseCase(
            actuator, publisher, clock, lambda _d: AlertThresholds(100, 200), PLUG, parse_plug_command
        )
        self.assertIsNone(use_case.evaluate(Sensor()(500)))
        self.assertEqual(actuator.calls, [])
        self.assertFalse(use_case.status()["autoCutoffEnabled"])

    def test_danger_en_tiempo_real_corta_una_vez_y_publica_auto(self):
        use_case, actuator, publisher, clock, r = build()

        self.assertIs(use_case.evaluate(r(250)), PlugState.OFF)
        self.assertIsNone(use_case.evaluate(r(260)))
        self.assertEqual(actuator.calls, ["off"])
        self.assertEqual(publisher.states, [(PLUG, "OFF", None, "AUTO", clock.now())])

    def test_saturada_corta(self):
        use_case, actuator, _, _, r = build()
        use_case.evaluate(r(800))
        self.assertEqual(actuator.calls, ["off"])

    def test_safe_o_caution_sin_corte_previo_no_hacen_nada(self):
        use_case, actuator, _, _, r = build()
        for value in (10, 150, 199):
            self.assertIsNone(use_case.evaluate(r(value)))
        self.assertEqual(actuator.calls, [])

    def test_histeresis_restablece_bajo_el_80_por_ciento(self):
        use_case, actuator, publisher, _, r = build()
        use_case.evaluate(r(250))

        self.assertIsNone(use_case.evaluate(r(180)))
        self.assertIsNone(use_case.evaluate(r(160)))
        self.assertIs(use_case.evaluate(r(159.9)), PlugState.ON)
        self.assertEqual(actuator.calls, ["off", "on"])
        self.assertEqual([s[1:4] for s in publisher.states], [("OFF", None, "AUTO"), ("ON", None, "AUTO")])

    def test_umbral_de_restablecimiento_configurado(self):
        use_case, _, _, _, r = build(restore_ut=50)
        use_case.evaluate(r(250))
        self.assertIsNone(use_case.evaluate(r(100)))
        self.assertIs(use_case.evaluate(r(49)), PlugState.ON)
        self.assertEqual(use_case.restore_threshold(DEVICE), 50)

    def test_restablecimiento_por_defecto_es_el_80_por_ciento_de_danger(self):
        use_case, _, _, _, _ = build(thresholds=AlertThresholds(100, 300))
        self.assertAlmostEqual(use_case.restore_threshold(DEVICE), 240.0)
        # Un umbral de restablecimiento que no queda bajo DANGER se reemplaza por el 80 %.
        use_case, _, _, _, _ = build(thresholds=AlertThresholds(50, 100), restore_ut=150)
        self.assertAlmostEqual(use_case.restore_threshold(DEVICE), 80.0)

    def test_diferidas_nunca_accionan(self):
        use_case, actuator, _, _, r = build()
        self.assertIsNone(use_case.evaluate(r(500, deferred=1)))
        use_case.evaluate(r(250))
        self.assertIsNone(use_case.evaluate(r(5, deferred=1)))
        self.assertEqual(actuator.calls, ["off"])

    def test_fallo_del_actuador_no_publica_y_se_reintenta(self):
        actuator = FakeActuator(ok=False)
        use_case, _, publisher, _, r = build(actuator=actuator)

        with self.assertLogs(LOGGER, level="WARNING"):
            self.assertIsNone(use_case.evaluate(r(250)))
        self.assertEqual(publisher.states, [])

        actuator.ok = True
        self.assertIs(use_case.evaluate(r(250)), PlugState.OFF)
        self.assertIsNone(use_case.status()["lastError"])


class UserPriorityTest(unittest.TestCase):
    def test_encendido_del_usuario_durante_danger_no_se_corta(self):
        use_case, actuator, _, _, r = build()
        use_case.evaluate(r(250))
        use_case.handle_command(command("ON"))

        self.assertIsNone(use_case.evaluate(r(300)))
        self.assertEqual(actuator.calls, ["off", "on"])

    def test_tras_bajar_del_restablecimiento_danger_vuelve_a_cortar(self):
        use_case, actuator, _, _, r = build()
        use_case.handle_command(command("ON"))
        use_case.evaluate(r(250))
        use_case.evaluate(r(50))  # fin del episodio
        self.assertIs(use_case.evaluate(r(250)), PlugState.OFF)
        self.assertEqual(actuator.calls, ["on", "off"])

    def test_apagado_del_usuario_no_lo_restablece_la_regla_automatica(self):
        use_case, actuator, _, _, r = build()
        use_case.evaluate(r(250))
        use_case.handle_command(command("OFF"))
        self.assertIsNone(use_case.evaluate(r(5)))
        self.assertEqual(actuator.calls, ["off", "off"])
        self.assertEqual(use_case.status()["source"], "USER")

    def test_la_orden_vale_aunque_el_corte_automatico_este_desactivado(self):
        use_case, actuator, _, _, _ = build(auto=False)
        self.assertIs(use_case.handle_command(command("OFF")), CommandResult.APPLIED)
        self.assertEqual(actuator.calls, ["off"])


class AccumulatedTest(unittest.TestCase):
    def test_entre_varias_acumuladas_manda_la_danger(self):
        use_case, actuator, _, _, r = build()
        self.assertIs(use_case.evaluate_latest([r(10), r(250), r(20)]), PlugState.OFF)
        self.assertEqual(actuator.calls, ["off"])

    def test_sin_danger_decide_la_ultima(self):
        use_case, _, _, _, r = build()
        use_case.evaluate(r(250))
        self.assertIs(use_case.evaluate_latest([r(190), r(10)]), PlugState.ON)

    def test_solo_diferidas_no_hacen_nada(self):
        use_case, actuator, _, _, r = build()
        self.assertIsNone(use_case.evaluate_latest([r(500, deferred=1)]))
        self.assertEqual(actuator.calls, [])


if __name__ == "__main__":
    unittest.main()
