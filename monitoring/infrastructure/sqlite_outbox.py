"""Outbox de lecturas sobre SQLite (Peewee). Implementa `ReadingOutbox`."""
from __future__ import annotations

from datetime import datetime
from typing import Callable, Dict, List, Optional

from peewee import IntegrityError

from monitoring.domain.entities import OutboxMessage, Reading
from monitoring.domain.services import BatchPolicy
from monitoring.domain.value_objects import (
    AlertLevel,
    CapturedAtSource,
    MagneticFluxDensity,
    ReadingIdentity,
    ReadingQuality,
    SyncOrigin,
)
from monitoring.infrastructure.models import ReadingRow, ReadingStatus
from shared.database import db


def _to_entity(row: ReadingRow) -> Reading:
    return Reading(
        identity=ReadingIdentity(row.device_id, row.sequence, row.device_ts),
        flux=MagneticFluxDensity(row.flux_ut),
        captured_at=row.captured_at,
        captured_at_source=CapturedAtSource(row.captured_at_source),
        received_at=row.received_at,
        origin=SyncOrigin(row.origin),
        quality=ReadingQuality(row.quality),
        level=AlertLevel[row.level],
    )


class SqliteReadingOutbox:
    """Outbox persistente. La deduplicación la hace el índice único de la tabla."""

    def add(self, reading: Reading) -> bool:
        try:
            with db.atomic():
                ReadingRow.create(
                    device_id=reading.device_id,
                    sequence=reading.sequence,
                    device_ts=reading.identity.device_ts,
                    flux_ut=reading.flux.micro_tesla,
                    captured_at=reading.captured_at,
                    captured_at_source=reading.captured_at_source.value,
                    received_at=reading.received_at,
                    origin=reading.origin.value,
                    quality=reading.quality.value,
                    level=reading.level.wire_value,
                )
        except IntegrityError:
            # Reenvío del sensor: la fila original conserva su capturedAt.
            return False
        return True

    def find(self, identity: ReadingIdentity) -> Optional[Reading]:
        row = ReadingRow.get_or_none(
            (ReadingRow.device_id == identity.device_id)
            & (ReadingRow.sequence == identity.sequence)
            & (ReadingRow.device_ts == identity.device_ts)
        )
        return _to_entity(row) if row else None

    def latest(self, device_id: Optional[str] = None, limit: int = 50) -> List[Reading]:
        query = ReadingRow.select().order_by(ReadingRow.id.desc())
        if device_id:
            query = query.where(ReadingRow.device_id == device_id)
        return [_to_entity(row) for row in query.limit(limit)]

    def pending_count(self) -> int:
        return (
            ReadingRow.select()
            .where(ReadingRow.status != ReadingStatus.PUBLISHED)
            .count()
        )

    def batch_pending(self, policy: BatchPolicy, new_message_id: Callable[[], str]) -> int:
        rows = list(
            ReadingRow.select()
            .where(ReadingRow.status == ReadingStatus.PENDING)
            .order_by(ReadingRow.id)
        )
        if not rows:
            return 0
        ids_by_identity = {}
        readings = []
        for row in rows:
            reading = _to_entity(row)
            ids_by_identity[reading.identity] = row.id
            readings.append(reading)
        groups = policy.group(readings)
        # Todo o nada: un lote a medio guardar podría publicarse incompleto.
        with db.atomic():
            for group in groups:
                ids = [ids_by_identity[r.identity] for r in group]
                (
                    ReadingRow.update(status=ReadingStatus.BATCHED, message_id=new_message_id())
                    .where(ReadingRow.id.in_(ids))
                    .execute()
                )
        return len(groups)

    def open_messages(self, limit: Optional[int] = None) -> List[OutboxMessage]:
        rows = (
            ReadingRow.select()
            .where(ReadingRow.status == ReadingStatus.BATCHED)
            .order_by(ReadingRow.id)
        )
        grouped: Dict[str, List[Reading]] = {}
        for row in rows:
            if row.message_id not in grouped and limit is not None and len(grouped) >= limit:
                continue
            grouped.setdefault(row.message_id, []).append(_to_entity(row))
        return [OutboxMessage(mid, tuple(readings)) for mid, readings in grouped.items()]

    def mark_published(self, message_id: str, published_at: datetime) -> int:
        return (
            ReadingRow.update(status=ReadingStatus.PUBLISHED, published_at=published_at)
            .where(
                (ReadingRow.message_id == message_id)
                & (ReadingRow.status == ReadingStatus.BATCHED)
            )
            .execute()
        )

    def purge_published(self, older_than: datetime) -> int:
        # Exige PUBLISHED y hora de PUBACK: lo pendiente nunca se borra.
        return (
            ReadingRow.delete()
            .where(
                (ReadingRow.status == ReadingStatus.PUBLISHED)
                & ReadingRow.published_at.is_null(False)
                & (ReadingRow.published_at < older_than)
            )
            .execute()
        )
