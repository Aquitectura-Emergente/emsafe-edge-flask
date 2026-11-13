"""Tópicos y payloads del contrato MQTT, esquema 1.0.

`tests/test_contract_payload.py` compara la salida con los ejemplos del README.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict

from monitoring.domain.entities import OutboxMessage, Reading
from monitoring.domain.value_objects import DeviceType

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
