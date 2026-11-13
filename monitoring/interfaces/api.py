"""API REST local del Monitoring (inspección y demo)."""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from flask import Blueprint, jsonify, request

from monitoring.domain.entities import Reading
from monitoring.domain.ports import ReadingHistory

MAX_LIMIT = 500


def reading_to_json(reading: Reading) -> Dict[str, Any]:
    return {
        "deviceId": reading.device_id,
        "sequence": reading.sequence,
        "deviceTs": reading.device_ts,
        "field_uT": reading.flux.micro_tesla,
        "level": reading.level.wire_value,
        "message": reading.level.message,
        "quality": reading.quality.value,
        "origin": reading.origin.value,
        "capturedAt": reading.captured_at.isoformat(),
        "capturedAtSource": reading.captured_at_source.value,
        "receivedAt": reading.received_at.isoformat(),
    }


def create_monitoring_api(
    history: Optional[ReadingHistory] = None,
    health: Optional[Callable[[], Dict[str, Any]]] = None,
) -> Blueprint:
    api = Blueprint("monitoring_api", __name__)

    @api.route("/api/v1/emf-monitoring/readings", methods=["GET"])
    def list_readings():
        try:
            limit = int(request.args.get("limit", 50))
        except ValueError:
            return jsonify({"error": "limit debe ser un entero"}), 400
        limit = max(1, min(limit, MAX_LIMIT))
        device_id = request.args.get("deviceId") or request.args.get("serialNumber")
        if history is None:
            return jsonify([])
        return jsonify([reading_to_json(r) for r in history.latest(device_id, limit)])

    @api.route("/health", methods=["GET"])
    def health_check():
        body: Dict[str, Any] = {"status": "UP", "service": "emsafe-edge"}
        if health is not None:
            body.update(health())
        return jsonify(body)

    return api
