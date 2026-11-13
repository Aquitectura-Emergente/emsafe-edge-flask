"""Tópicos y payloads del contrato MQTT, esquema 1.0.

`tests/test_contract_payload.py` compara la salida con los ejemplos del README.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from monitoring.domain.entities import InvalidPlugCommand, OutboxMessage, PlugCommand, Reading
from monitoring.domain.value_objects import DeviceType, PlugAction, PlugState, PlugStateSource

SCHEMA_VERSION = "1.0"


# ---------- Tópicos ----------

def telemetry_topic(device_id: str) -> str:
    return f"iot/{device_id}/telemetry"


def device_status_topic(device_id: str) -> str:
    return f"iot/{device_id}/status"


def edge_status_topic(edge_node_id: str) -> str:
    return f"edge/{edge_node_id}/status"


# ---------- Formato ----------

def iso_utc(value: datetime) -> str:
    """ISO-8601 UTC con `Z`, truncado al segundo."""
    if value.tzinfo is None:
        raise ValueError("la hora debe tener zona horaria")
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_json(payload: Dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode(
        "utf-8"
    )


# ---------- Telemetría ----------

def _reading_payload(reading: Reading) -> Dict[str, Any]:
    return {
        "sequence": reading.sequence,
        "capturedAt": iso_utc(reading.captured_at),
        "capturedAtSource": reading.captured_at_source.value,
        "receivedAt": iso_utc(reading.received_at),
        "magneticFluxDensityUT": reading.flux.micro_tesla,
        "origin": reading.origin.value,
        "quality": reading.quality.value,
    }


def telemetry_payload(
    message: OutboxMessage,
    *,
    organization_id: str,
    edge_node_id: str,
    published_at: datetime,
    device_type: DeviceType = DeviceType.FIXED_SENSOR,
) -> Dict[str, Any]:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "messageId": message.message_id,
        "organizationId": organization_id,
        "edgeNodeId": edge_node_id,
        "deviceId": message.device_id,
        "deviceType": device_type.value,
        "publishedAt": iso_utc(published_at),
        "readings": [_reading_payload(r) for r in message.readings],
    }


# ---------- Estados ----------

def device_status_payload(
    device_id: str, edge_node_id: str, connectivity: str, changed_at: datetime
) -> Dict[str, Any]:
    if connectivity not in ("ONLINE", "OFFLINE"):
        raise ValueError(f"connectivity inválida: {connectivity!r}")
    return {
        "schemaVersion": SCHEMA_VERSION,
        "deviceId": device_id,
        "edgeNodeId": edge_node_id,
        "connectivity": connectivity,
        "changedAt": iso_utc(changed_at),
        # El firmware todavía no los expone por BLE.
        "firmwareVersion": None,
        "batteryLevel": None,
    }


def edge_online_payload(changed_at: datetime) -> Dict[str, Any]:
    return {"status": "ONLINE", "changedAt": iso_utc(changed_at)}


def edge_last_will_payload() -> Dict[str, Any]:
    """Last Will: sin `changedAt`, porque se registra antes de saber cuándo caerá el Edge."""
    return {"status": "OFFLINE"}


# ---------- Enchufe (extensión del hogar) ----------

def plug_command_topic(plug_id: str) -> str:
    return f"iot/{plug_id}/command"


def plug_state_topic(plug_id: str) -> str:
    return f"iot/{plug_id}/state"


def plug_state_payload(
    plug_id: str,
    state: PlugState,
    command_id: Optional[str],
    source: PlugStateSource,
    changed_at: datetime,
) -> Dict[str, Any]:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "plugId": plug_id,
        "state": state.value,
        "commandId": command_id,
        "source": source.value,
        "changedAt": iso_utc(changed_at),
    }


def _parse_iso_utc(value, name: str) -> datetime:
    if not isinstance(value, str):
        raise InvalidPlugCommand(f"{name} inválido: {value!r}")
    try:
        # Python 3.10 no acepta la "Z" final en fromisoformat.
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        raise InvalidPlugCommand(f"{name} no es ISO-8601: {value!r}")
    if parsed.tzinfo is None:
        raise InvalidPlugCommand(f"{name} sin zona horaria: {value!r}")
    return parsed.astimezone(timezone.utc)


def parse_plug_command(plug_id: str, raw) -> PlugCommand:
    """Valida un mensaje de `iot/{plugId}/command`. Los campos desconocidos se ignoran."""
    try:
        text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw
        data = json.loads(text)
    except (UnicodeDecodeError, ValueError, TypeError) as exc:
        raise InvalidPlugCommand(f"la orden no es JSON válido: {exc}")
    if not isinstance(data, dict):
        raise InvalidPlugCommand("la orden debe ser un objeto JSON")

    schema = data.get("schemaVersion")
    if not isinstance(schema, str) or schema.split(".")[0] != SCHEMA_VERSION.split(".")[0]:
        raise InvalidPlugCommand(f"schemaVersion no soportada: {schema!r}")
    if data.get("plugId") != plug_id:
        raise InvalidPlugCommand(
            f"plugId {data.get('plugId')!r} no coincide con el tópico ({plug_id!r})"
        )
    try:
        action = PlugAction(data.get("action"))
    except ValueError:
        raise InvalidPlugCommand(f"action inválida: {data.get('action')!r}")
    if data.get("requestedBy") != "USER":
        raise InvalidPlugCommand(f"requestedBy inválido: {data.get('requestedBy')!r}")
    return PlugCommand(
        command_id=data.get("commandId"),
        plug_id=plug_id,
        action=action,
        requested_at=_parse_iso_utc(data.get("requestedAt"), "requestedAt"),
    )
