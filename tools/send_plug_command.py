"""Simula la orden del usuario: publica ON u OFF en iot/{plugId}/command.

Uso (desde la raíz del repo):
    .\\.venv\\Scripts\\python.exe tools\\send_plug_command.py OFF
    .\\.venv\\Scripts\\python.exe tools\\send_plug_command.py ON --host localhost --plug-id EMSafe-PLUG-01

Genera un commandId nuevo y requestedAt con la hora UTC actual. El broker y el
enchufe se toman de EMSAFE_MQTT_HOST, EMSAFE_MQTT_PORT, EMSAFE_MQTT_TLS,
EMSAFE_MQTT_USERNAME, EMSAFE_MQTT_PASSWORD y EMSAFE_PLUG_ID, igual que el Edge.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import uuid
from datetime import datetime, timezone
from typing import Optional


def build_command(action: str, plug_id: str, now: Optional[datetime] = None, command_id: Optional[str] = None) -> dict:
    action = action.upper()
    if action not in ("ON", "OFF"):
        raise ValueError(f"la acción debe ser ON u OFF, no {action!r}")
    now = now or datetime.now(timezone.utc)
    return {
        "schemaVersion": "1.0",
        "commandId": command_id or str(uuid.uuid4()),
        "plugId": plug_id,
        "action": action,
        "requestedBy": "USER",
        "requestedAt": now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _env_bool(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "si", "sí", "on")


def _parse_args(argv):
    parser = argparse.ArgumentParser(description="Publica una orden ON/OFF para el enchufe del Edge hogar.")
    parser.add_argument("action", type=str.upper, choices=["ON", "OFF"])
    parser.add_argument("--host", default=os.environ.get("EMSAFE_MQTT_HOST") or "localhost")
    parser.add_argument("--port", type=int, default=int(os.environ.get("EMSAFE_MQTT_PORT") or 1883))
    parser.add_argument("--plug-id", default=os.environ.get("EMSAFE_PLUG_ID") or "EMSafe-PLUG-01")
    parser.add_argument("--timeout", type=float, default=10.0, help="segundos de espera (conexión y PUBACK)")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    import paho.mqtt.client as mqtt

    args = _parse_args(argv)
    command = build_command(args.action, args.plug_id)
    topic = f"iot/{args.plug_id}/command"

    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"emsafe-cmd-{uuid.uuid4().hex[:8]}",
        protocol=mqtt.MQTTv5,
    )
    if os.environ.get("EMSAFE_MQTT_USERNAME"):
        client.username_pw_set(os.environ["EMSAFE_MQTT_USERNAME"], os.environ.get("EMSAFE_MQTT_PASSWORD"))
    if _env_bool("EMSAFE_MQTT_TLS"):
        client.tls_set()

    connected = threading.Event()
    result = {}

    def on_connect(_client, _userdata, _flags, reason_code, _properties=None):
        result["reason"] = reason_code
        connected.set()

    client.on_connect = on_connect
    try:
        client.connect(args.host, args.port, keepalive=30)
    except OSError as exc:
        print(f"No se pudo conectar a {args.host}:{args.port}: {exc}", file=sys.stderr)
        return 1
    client.loop_start()
    try:
        if not connected.wait(args.timeout):
            print("El broker no respondió a la conexión", file=sys.stderr)
            return 1
        if result["reason"].is_failure:
            print(f"El broker rechazó la conexión: {result['reason']}", file=sys.stderr)
            return 1

        info = client.publish(topic, json.dumps(command), qos=1, retain=False)
        info.wait_for_publish(timeout=args.timeout)
        if not info.is_published():
            print("La orden no se confirmó (sin PUBACK)", file=sys.stderr)
            return 1
        print(f"{topic} <- {json.dumps(command)}")
        return 0
    finally:
        client.disconnect()
        client.loop_stop()


if __name__ == "__main__":
    sys.exit(main())
