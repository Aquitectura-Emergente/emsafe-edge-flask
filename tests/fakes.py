"""Dobles compartidos por los tests: sin SQLite, BLE ni broker reales."""
from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional

from monitoring.domain.entities import OutboxMessage, Reading
from monitoring.domain.services import BatchPolicy
from monitoring.domain.value_objects import ReadingIdentity

T0 = datetime(2026, 11, 12, 15, 4, 4, tzinfo=timezone.utc)

PENDING, BATCHED, PUBLISHED = "PENDING", "BATCHED", "PUBLISHED"


class FakeClock:
    """Reloj fijo que solo avanza cuando el test lo pide."""

    def __init__(self, start: datetime = T0) -> None:
        self.current = start

    def now(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


class _Entry:
    def __init__(self, reading: Reading) -> None:
        self.reading = reading
        self.status = PENDING
        self.message_id: Optional[str] = None
        self.published_at: Optional[datetime] = None


class FakeOutbox:
    """Outbox en memoria con las mismas reglas que el de SQLite."""

    def __init__(self) -> None:
        self.entries: List[_Entry] = []
        self._by_identity: Dict[ReadingIdentity, _Entry] = {}

    # --- ReadingOutbox ---
    def add(self, reading: Reading) -> bool:
        if reading.identity in self._by_identity:
            return False
        entry = _Entry(reading)
        self.entries.append(entry)
        self._by_identity[reading.identity] = entry
        return True

    def find(self, identity: ReadingIdentity) -> Optional[Reading]:
        entry = self._by_identity.get(identity)
        return entry.reading if entry else None

    def pending_count(self) -> int:
        return sum(1 for e in self.entries if e.status != PUBLISHED)

    def batch_pending(self, policy: BatchPolicy, new_message_id: Callable[[], str]) -> int:
        pending = [e.reading for e in self.entries if e.status == PENDING]
        groups = policy.group(pending)
        for group in groups:
            mid = new_message_id()
            for reading in group:
                entry = self._by_identity[reading.identity]
                entry.status, entry.message_id = BATCHED, mid
        return len(groups)

    def open_messages(self, limit: Optional[int] = None) -> List[OutboxMessage]:
        grouped: Dict[str, List[Reading]] = {}
        for e in self.entries:
            if e.status != BATCHED:
                continue
            if e.message_id not in grouped and limit is not None and len(grouped) >= limit:
                continue
            grouped.setdefault(e.message_id, []).append(e.reading)
        return [OutboxMessage(mid, tuple(rs)) for mid, rs in grouped.items()]

    def mark_published(self, message_id: str, published_at: datetime) -> int:
        marked = 0
        for e in self.entries:
            if e.message_id == message_id and e.status == BATCHED:
                e.status, e.published_at = PUBLISHED, published_at
                marked += 1
        return marked

    def purge_published(self, older_than: datetime) -> int:
        keep = [
            e for e in self.entries
            if not (e.status == PUBLISHED and e.published_at is not None and e.published_at < older_than)
        ]
        purged = len(self.entries) - len(keep)
        self.entries = keep
        self._by_identity = {e.reading.identity: e for e in keep}
        return purged

    # --- ayudas para los tests ---
    def stored(self) -> List[Reading]:
        return [e.reading for e in self.entries]

    def statuses(self) -> List[str]:
        return [e.status for e in self.entries]


class FakePublisher:
    """Broker simulado. `responses` decide cada intento: True (PUBACK), False,
    una excepción o "hang" (nunca confirma). Sin respuestas, confirma todo."""

    def __init__(self, responses=None) -> None:
        self.responses = list(responses or [])
        self.sent: List[tuple] = []  # (message_id, device_id, n_lecturas, published_at)

    async def publish_telemetry(self, message: OutboxMessage, published_at: datetime) -> bool:
        self.sent.append((message.message_id, message.device_id, len(message.readings), published_at))
        response = self.responses.pop(0) if self.responses else True
        if isinstance(response, BaseException):
            raise response
        if response == "hang":
            import asyncio
            await asyncio.Event().wait()
        return response


def sequential_ids(prefix: str = "msg") -> Callable[[], str]:
    counter = itertools.count(1)
    return lambda: f"{prefix}-{next(counter)}"


class FakeStatusPublisher:
    """Broker para los estados retained: registra lo publicado; puede estar caído."""

    def __init__(self, connected: bool = True) -> None:
        self.connected = connected
        self.published: List[tuple] = []  # (device_id, connectivity, changed_at)

    def publish_device_status(self, device_id: str, connectivity: str, changed_at: datetime) -> bool:
        if not self.connected:
            return False
        self.published.append((device_id, connectivity, changed_at))
        return True
