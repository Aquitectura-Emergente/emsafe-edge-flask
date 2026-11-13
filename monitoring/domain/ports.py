"""Puertos de la aplicación. Son `Protocol`: la infraestructura y los dobles los cumplen por firma."""
from __future__ import annotations

from datetime import datetime
from typing import Callable, List, Optional, Protocol

from monitoring.domain.entities import OutboxMessage, Reading
from monitoring.domain.services import BatchPolicy
from monitoring.domain.value_objects import ReadingIdentity
from shared.clock import Clock  # noqa: F401  (reexportado como puerto)


class ReadingOutbox(Protocol):
    """Outbox local: toda lectura válida se guarda aquí antes de salir por MQTT.

    PENDING → BATCHED (con `messageId`) → PUBLISHED (tras el PUBACK). Las
    publicadas se conservan un tiempo para descartar reenvíos del sensor.
    """

    def add(self, reading: Reading) -> bool:
        """False si la identidad ya existía; la lectura guardada no se modifica."""
        ...

    def find(self, identity: ReadingIdentity) -> Optional[Reading]:
        ...

    def pending_count(self) -> int:
        """Lecturas que todavía no tienen confirmación del broker."""
        ...

    def batch_pending(self, policy: BatchPolicy, new_message_id: Callable[[], str]) -> int:
        """Agrupa las PENDING en mensajes. Devuelve cuántos armó."""
        ...

    def open_messages(self, limit: Optional[int] = None) -> List[OutboxMessage]:
        """Mensajes armados que esperan PUBACK, en orden de llegada."""
        ...

    def mark_published(self, message_id: str, published_at: datetime) -> int:
        """Marca publicadas las lecturas del mensaje. Devuelve cuántas marcó."""
        ...

    def purge_published(self, older_than: datetime) -> int:
        """Borra las publicadas antes de `older_than`.

        Nunca borra pendientes ni sin confirmar, por viejas que sean: si la
        nube estuvo caída, siguen esperando.
        """
        ...


class ReadingHistory(Protocol):
    """Consulta de las últimas lecturas guardadas, para la API local."""

    def latest(self, device_id: Optional[str] = None, limit: int = 50) -> List[Reading]:
        """Más recientes primero."""
        ...


class TelemetryPublisher(Protocol):
    """Publica telemetría con QoS 1. El adaptador arma tópico y payload."""

    async def publish_telemetry(self, message: OutboxMessage, published_at: datetime) -> bool:
        """True solo si llegó el PUBACK."""
        ...


class StatusPublisher(Protocol):
    """Publica la conectividad del sensor (retained).

    No reintenta: sin broker devuelve False y el estado se republica al reconectar.
    """

    def publish_device_status(self, device_id: str, connectivity: str, changed_at: datetime) -> bool:
        ...


class ActuatorGateway(Protocol):
    """Enchufe inteligente. Sin respuesta devuelve False: nunca bloquea la ingesta."""

    def turn_off(self) -> bool:
        ...

    def turn_on(self) -> bool:
        ...


class DesiredPlugSource(Protocol):
    """Orden del usuario para el enchufe desde la app."""

    def fetch_desired_plug(self, device_id: str) -> Optional[str]:
        """"ON", "OFF" o None si no hay orden o no se pudo consultar."""
        ...
