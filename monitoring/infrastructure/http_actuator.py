"""Adaptador HTTP del enchufe inteligente: `GET <base>/off` y `GET <base>/on`."""
from __future__ import annotations

import logging
from typing import Callable

import requests

log = logging.getLogger(__name__)


class HttpActuatorGateway:
    """Implementa `ActuatorGateway`. Un fallo se registra y devuelve False."""

    def __init__(self, base_url: str, timeout_s: float = 3.0, get: Callable = requests.get) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._get = get
        self.last_ok = False
        self.last_error = ""

    def turn_off(self) -> bool:
        return self._call("off")

    def turn_on(self) -> bool:
        return self._call("on")

    def _call(self, action: str) -> bool:
        url = f"{self._base_url}/{action}"
        try:
            resp = self._get(url, timeout=self._timeout_s)
        except Exception as exc:  # sin red, IP equivocada, actuador apagado...
            return self._fail(f"{url}: {exc}")
        if not 200 <= resp.status_code < 300:
            return self._fail(f"{url}: HTTP {resp.status_code}")
        self.last_ok, self.last_error = True, ""
        return True

    def _fail(self, error: str) -> bool:
        self.last_ok, self.last_error = False, error
        log.warning("Actuador sin respuesta: %s", error)
        return False
