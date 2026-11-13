"""Orden del usuario para el enchufe, consultada al backend por HTTP.

Endpoint público `GET /api/v1/devices/{id}/plug`, que responde
`{"data": {"desiredPlug": "ON" | "OFF" | null}}`.
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

import requests

log = logging.getLogger(__name__)


class BackendDesiredPlugSource:
    """Implementa `DesiredPlugSource`. Ante cualquier fallo devuelve None (sin orden)."""

    def __init__(self, base_url: str, timeout_s: float = 3.0, get: Callable = requests.get) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._get = get

    def fetch_desired_plug(self, device_id: str) -> Optional[str]:
        url = f"{self._base_url}/api/v1/devices/{device_id}/plug"
        try:
            resp = self._get(url, timeout=self._timeout_s)
            if resp.status_code != 200:
                return None
            value = ((resp.json() or {}).get("data") or {}).get("desiredPlug")
        except Exception as exc:
            log.warning("No se pudo consultar la orden del usuario: %s", exc)
            return None
        if isinstance(value, str) and value.upper() in ("ON", "OFF"):
            return value.upper()
        return None
