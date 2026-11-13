"""Reloj del Edge, siempre en UTC. Es inyectable para que los tests fijen el instante."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """Instante actual, con tzinfo UTC."""
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)
