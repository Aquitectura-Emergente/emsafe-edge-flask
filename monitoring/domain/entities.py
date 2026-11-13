"""Entidades: lectura, mensaje del outbox y configuración del sensor."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Tuple

from monitoring.domain.value_objects import (
    AlertLevel,
    CapturedAtSource,
    MagneticFluxDensity,
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


class InvalidDeviceConfig(ValueError):
    """La configuración recibida de la nube no es aplicable al sensor."""


@dataclass(frozen=True)
class DeviceConfig:
    """Configuración del sensor. Su umbral reemplaza al de DANGER del espacio.

    Se exige 0 < umbral < saturación: el firmware acepta cualquier valor
    positivo, pero un umbral desde la saturación en adelante nunca se dispararía.
    """

    device_id: str
    config_version: int
    alert_threshold_ut: float
    defined_by: str = "SSO_RESPONSIBLE"
    updated_at: Optional[datetime] = None

    @classmethod
    def create(
        cls,
        device_id: str,
        config_version,
        alert_threshold_ut,
        saturation_ut: float,
        defined_by: str = "SSO_RESPONSIBLE",
        updated_at: Optional[datetime] = None,
    ) -> "DeviceConfig":
        if not isinstance(device_id, str) or not device_id.strip():
            raise InvalidDeviceConfig("deviceId vacío")
        if (
            isinstance(config_version, bool)
            or not isinstance(config_version, int)
            or config_version < 0
        ):
            raise InvalidDeviceConfig(f"configVersion inválida: {config_version!r}")
        if isinstance(alert_threshold_ut, bool) or not isinstance(alert_threshold_ut, (int, float)):
            raise InvalidDeviceConfig(f"alertThresholdUT no numérico: {alert_threshold_ut!r}")
        threshold = float(alert_threshold_ut)
        if math.isnan(threshold) or math.isinf(threshold):
            raise InvalidDeviceConfig(f"alertThresholdUT no finito: {alert_threshold_ut!r}")
        if not 0 < threshold < saturation_ut:
            raise InvalidDeviceConfig(
                f"alertThresholdUT {threshold} fuera de rango: debe ser > 0 y < {saturation_ut} "
                "(saturación del sensor)"
            )
        if updated_at is not None:
            try:
                _require_utc("updatedAt", updated_at)
            except ValueError as exc:
                raise InvalidDeviceConfig(str(exc))
        return cls(device_id, config_version, threshold, defined_by, updated_at)

    def supersedes(self, current: Optional["DeviceConfig"]) -> bool:
        """True si reemplaza a la vigente (retained y QoS 1 pueden repetirse o desordenarse)."""
        if current is None:
            return True
        if current.device_id != self.device_id:
            raise ValueError("no se comparan configuraciones de dispositivos distintos")
        return self.config_version > current.config_version
