"""Pasos de orden_enchufe.feature sobre el Edge hogar en marcha (ver environment.py)."""
import logging
import time
import uuid
from datetime import datetime, timezone

from behave import given, then, when

from monitoring.infrastructure.ble_protocol import RawReading
from monitoring.infrastructure.ble_sensor_client import ReadingReceived
from monitoring.infrastructure.models import ReadingRow


@given("el actuador del enchufe disponible")
def step_actuador_disponible(context):
    context.http.responds = True


@given("el actuador del enchufe sin respuesta")
def step_actuador_sin_respuesta(context):
    context.http.responds = False


@when('llega la orden "{accion}" por el tópico "{topico}"')
def step_llega_la_orden(context, accion, topico):
    assert topico in context.broker.subscribed, f"el Edge no está suscrito a {topico}"
    context.command_id = str(uuid.uuid4())
    context.broker.deliver(topico, {
        "schemaVersion": "1.0",
        "commandId": context.command_id,
        "plugId": context.settings.plug_id,
        "action": accion,
        "requestedBy": "USER",
        "requestedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })
    # La orden termina cuando el Edge publica el estado del enchufe con su commandId.
    state_topic = f"iot/{context.settings.plug_id}/state"
    assert context.run_until(lambda: any(
        p.get("commandId") == context.command_id for p in context.broker.on_topic(state_topic)
    )), "el Edge no procesó la orden"


@then("el Edge solicita el corte al actuador")
def step_solicita_corte(context):
    assert context.http.calls == [f"{context.actuator_url}/off"], context.http.calls


@then('publica el estado "{estado}" con el mismo commandId en "{topico}"')
def step_publica_estado(context, estado, topico):
    estados = [p for p in context.broker.on_topic(topico) if p.get("commandId") == context.command_id]
    assert len(estados) == 1, estados
    assert estados[0]["state"] == estado, estados[0]
    assert estados[0]["source"] == "USER", estados[0]
    assert estados[0]["plugId"] == context.settings.plug_id


@then("el Edge registra el error")
def step_registra_error(context):
    assert context.http.calls == [f"{context.actuator_url}/off"], context.http.calls
    avisos = [r for r in context.logs.records if r.levelno >= logging.WARNING]
    assert any(r.name.endswith("http_actuator") and "sin respuesta" in r.getMessage() for r in avisos), \
        [r.getMessage() for r in avisos]
    assert any(context.command_id in r.getMessage() for r in avisos), [r.getMessage() for r in avisos]
    estado = [p for p in context.broker.on_topic(f"iot/{context.settings.plug_id}/state")
              if p.get("commandId") == context.command_id]
    assert estado and estado[-1]["state"] != "OFF", estado


@then("sigue recibiendo y publicando las lecturas del sensor")
def step_sigue_con_las_lecturas(context):
    ts = int(time.time())
    secuencias = [1, 2, 3]
    for seq in secuencias:
        context.queue.put_nowait(ReadingReceived(context.device_id, RawReading(seq, ts + seq, 40.0 + seq, 0)))
    telemetry_topic = f"iot/{context.device_id}/telemetry"

    def publicadas():
        return [r["sequence"] for m in context.broker.on_topic(telemetry_topic) for r in m["readings"]]

    assert context.run_until(lambda: publicadas() == secuencias), publicadas()
    assert context.run_until(lambda: context.outbox.pending_count() == 0)
    assert ReadingRow.select().count() == len(secuencias)
