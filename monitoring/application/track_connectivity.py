"""Caso de uso: publicar la conectividad BLE del sensor.

Guarda el último estado para republicarlo al reconectar el broker, por si
el cambio ocurrió sin nube.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Dict, Tuple

from monitoring.domain.ports import Clock, StatusPublisher

log = logging.getLogger(__name__)

ONLINE, OFFLINE = "ONLINE", "OFFLINE"


class TrackConnectivityUseCase:
    def __init__(self, publisher: StatusPublisher, clock: Clock) -> None:
        self._publisher = publisher
        self._clock = clock
        self._last: Dict[str, Tuple[str, datetime]] = {}

    def device_connected(self, device_id: str) -> None:
        self._change(device_id, ONLINE)

    def device_disconnected(self, device_id: str) -> None:
        self._change(device_id, OFFLINE)

    def republish_all(self) -> int:
        published = 0
        for device_id, (connectivity, changed_at) in self._last.items():
            if self._publisher.publish_device_status(device_id, connectivity, changed_at):
                published += 1
        return published

    def current(self, device_id: str):
        state = self._last.get(device_id)
        return state[0] if state else None

    def _change(self, device_id: str, connectivity: str) -> None:
        changed_at = self._clock.now()
        self._last[device_id] = (connectivity, changed_at)
        if not self._publisher.publish_device_status(device_id, connectivity, changed_at):
            log.info(
                "Estado %s de %s sin publicar (broker no disponible): se publicará al reconectar",
                connectivity, device_id,
            )
