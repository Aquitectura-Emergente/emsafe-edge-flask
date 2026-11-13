"""Caso de uso: publicar el outbox por MQTT.

Un mensaje se marca publicado solo tras el PUBACK: si el broker cae antes,
la lectura sigue en el outbox y no se pierde. El ciclo se detiene en el
primer fallo para no publicar un mensaje posterior antes que uno anterior,
y el siguiente reenvía el mismo mensaje con su `messageId`. QoS 1 puede
duplicar: la ingesta es idempotente por ReadingIdentity.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import Callable, Optional

from monitoring.domain.ports import Clock, ReadingOutbox, TelemetryPublisher
from monitoring.domain.services import BatchPolicy

log = logging.getLogger(__name__)


def _uuid4() -> str:
    return str(uuid.uuid4())


@dataclass(frozen=True)
class PublishReport:
    batched: int
    published: int
    failed: bool


class PublishOutboxUseCase:
    def __init__(
        self,
        outbox: ReadingOutbox,
        publisher: TelemetryPublisher,
        clock: Clock,
        policy: Optional[BatchPolicy] = None,
        new_message_id: Callable[[], str] = _uuid4,
        ack_timeout_s: float = 10.0,
        max_messages_per_run: Optional[int] = None,
    ) -> None:
        self._outbox = outbox
        self._publisher = publisher
        self._clock = clock
        self._policy = policy or BatchPolicy()
        self._new_message_id = new_message_id
        self._ack_timeout_s = ack_timeout_s
        self._max_messages = max_messages_per_run

    async def run_once(self) -> PublishReport:
        batched = self._outbox.batch_pending(self._policy, self._new_message_id)
        published = 0
        for message in self._outbox.open_messages(self._max_messages):
            if not await self._publish(message):
                return PublishReport(batched, published, failed=True)
            self._outbox.mark_published(message.message_id, self._clock.now())
            published += 1
        return PublishReport(batched, published, failed=False)

    async def _publish(self, message) -> bool:
        try:
            acked = await asyncio.wait_for(
                self._publisher.publish_telemetry(message, self._clock.now()),
                timeout=self._ack_timeout_s,
            )
        except asyncio.TimeoutError:
            log.warning(
                "Sin PUBACK para el mensaje %s de %s tras %.0f s: se reintentará",
                message.message_id, message.device_id, self._ack_timeout_s,
            )
            return False
        except Exception as exc:
            log.warning(
                "No se pudo publicar el mensaje %s de %s (%s): se reintentará",
                message.message_id, message.device_id, exc,
            )
            return False
        if not acked:
            log.warning(
                "El broker no confirmó el mensaje %s de %s: se reintentará",
                message.message_id, message.device_id,
            )
        return bool(acked)
