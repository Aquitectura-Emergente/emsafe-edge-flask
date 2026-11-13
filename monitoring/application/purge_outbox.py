"""Caso de uso: borrar las lecturas publicadas hace más de `retention_days`.

Mientras tanto se conservan para descartar reenvíos del sensor. Las
pendientes o sin confirmar nunca se borran por antigüedad, aunque la nube
esté caída semanas: son las que hay que entregar.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from monitoring.domain.ports import Clock, ReadingOutbox

log = logging.getLogger(__name__)


class PurgeOutboxUseCase:
    def __init__(self, outbox: ReadingOutbox, clock: Clock, retention_days: int = 7) -> None:
        if retention_days < 1:
            raise ValueError("retention_days debe ser al menos 1")
        self._outbox = outbox
        self._clock = clock
        self._retention = timedelta(days=retention_days)

    def run_once(self) -> int:
        cutoff = self._clock.now() - self._retention
        purged = self._outbox.purge_published(cutoff)
        if purged:
            log.info("Outbox: %d lecturas publicadas antes de %s eliminadas", purged, cutoff.isoformat())
        return purged
