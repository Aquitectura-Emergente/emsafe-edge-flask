"""Entidades: lectura, mensaje del outbox y orden del enchufe."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Tuple

from monitoring.domain.value_objects import (
    AlertLevel,
    CapturedAtSource,
    MagneticFluxDensity,
    PlugAction,
    ReadingIdentity,
    ReadingQuality,
    SyncOrigin,
)


def _require_utc(name: str, value: datetime) -> None:
    # Una hora sin zona es ambigua: se rechaza en vez de suponer la zona local.
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} debe ser un datetime con zona horaria")
    if value.utcoffset().total_seconds() != 0:
        raise ValueError(f"{name} debe estar en UTC")


@dataclass(frozen=True)
class Reading:
    """Lectura validada del sensor. Se construye con `ReadingFactory`.

    `level` se calcula con los umbrales vigentes al recibirla y se guarda con
    ella; es local y no viaja en el contrato MQTT.
    """

    identity: ReadingIdentity
    flux: MagneticFluxDensity
    captured_at: datetime
    captured_at_source: CapturedAtSource
    received_at: datetime
    origin: SyncOrigin
    quality: ReadingQuality
    level: AlertLevel = AlertLevel.SAFE

    def __post_init__(self) -> None:
        _require_utc("captured_at", self.captured_at)
        _require_utc("received_at", self.received_at)

    @property
    def device_id(self) -> str:
        return self.identity.device_id

    @property
    def sequence(self) -> int:
        return self.identity.sequence

    @property
    def device_ts(self) -> int:
        return self.identity.device_ts

    @property
    def is_deferred(self) -> bool:
        return self.origin is SyncOrigin.DEFERRED


@dataclass(frozen=True)
class OutboxMessage:
    """Lote de lecturas de un dispositivo y un origen, con su `messageId`.

    Se arma una sola vez y se guarda, así un reintento reenvía el mismo
    mensaje. `publishedAt` no forma parte de él: es la hora de cada intento.
    """

    message_id: str
    readings: Tuple[Reading, ...]

    def __post_init__(self) -> None:
        if not self.message_id:
            raise ValueError("messageId vacío")
        if not self.readings:
            raise ValueError("un mensaje de telemetría necesita al menos una lectura")
        first = self.readings[0]
        for r in self.readings[1:]:
            if r.device_id != first.device_id:
                raise ValueError("un mensaje no puede mezclar dispositivos")
            if r.origin is not first.origin:
                raise ValueError("un mensaje no puede mezclar REALTIME y DEFERRED")

    @property
    def device_id(self) -> str:
        return self.readings[0].device_id

    @property
    def origin(self) -> SyncOrigin:
        return self.readings[0].origin


class InvalidPlugCommand(ValueError):
    """Orden del enchufe que no cumple el contrato."""


@dataclass(frozen=True)
class PlugCommand:
    """Orden del usuario recibida por `iot/{plugId}/command`."""

    command_id: str
    plug_id: str
    action: PlugAction
    requested_at: datetime
    requested_by: str = "USER"

    def __post_init__(self) -> None:
        if not isinstance(self.command_id, str) or not self.command_id.strip():
            raise InvalidPlugCommand("commandId vacío")
        if not isinstance(self.plug_id, str) or not self.plug_id.strip():
            raise InvalidPlugCommand("plugId vacío")
        try:
            _require_utc("requestedAt", self.requested_at)
        except ValueError as exc:
            raise InvalidPlugCommand(str(exc))
