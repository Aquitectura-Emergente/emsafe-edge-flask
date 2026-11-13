"""Servicios de dominio."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence

from monitoring.domain.entities import Reading
from monitoring.domain.value_objects import (
    AlertLevel,
    AlertThresholds,
    CapturedAtSource,
    MagneticFluxDensity,
    ReadingIdentity,
    ReadingQuality,
    SyncOrigin,
)


class ReadingFactory:
    """Convierte el dato crudo del sensor en una `Reading` validada y clasificada.

    Con `ts = 0` el sensor aún no tenía hora y `capturedAt` es la hora de
    recepción (EDGE_RECEIVED). Se calcula una sola vez: los reenvíos se
    descartan por identidad antes de llegar aquí.
    """

    def __init__(self, saturation_ut: float, thresholds: Optional[AlertThresholds] = None) -> None:
        if not saturation_ut > 0:
            raise ValueError("saturation_ut debe ser mayor que 0")
        self._saturation_ut = float(saturation_ut)
        # Umbrales usados cuando la lectura no trae los vigentes.
        self._thresholds = thresholds or AlertThresholds(100.0, 200.0)

    @property
    def saturation_ut(self) -> float:
        return self._saturation_ut

    def from_raw(
        self,
        device_id: str,
        sequence: int,
        ts: int,
        value,
        deferred: int,
        received_at: datetime,
        thresholds: Optional[AlertThresholds] = None,
    ) -> Reading:
        identity = ReadingIdentity(device_id, sequence, ts)
        flux = MagneticFluxDensity.from_raw(value)
        if identity.has_device_clock:
            captured_at = datetime.fromtimestamp(ts, tz=timezone.utc)
            source = CapturedAtSource.DEVICE_CLOCK
        else:
            captured_at = received_at
            source = CapturedAtSource.EDGE_RECEIVED
        quality = ReadingQuality.classify(flux, self._saturation_ut)
        return Reading(
            identity=identity,
            flux=flux,
            captured_at=captured_at,
            captured_at_source=source,
            received_at=received_at,
            origin=SyncOrigin.from_flag(deferred),
            quality=quality,
            level=AlertLevel.classify(flux, quality, thresholds or self._thresholds),
        )


class BatchPolicy:
    """Agrupa las lecturas pendientes en mensajes de telemetría.

    Un mensaje lleva un solo dispositivo y un solo origen. Las de tiempo real
    van de a una; las diferidas, hasta `max_deferred` por mensaje, porque el
    buffer del sensor puede traer 1800 de golpe. Solo se agrupan lecturas
    consecutivas, para respetar el orden de llegada de cada dispositivo.
    """

    def __init__(self, max_deferred: int = 50) -> None:
        if max_deferred < 1:
            raise ValueError("max_deferred debe ser al menos 1")
        self.max_deferred = max_deferred

    def group(self, readings: Sequence[Reading]) -> List[List[Reading]]:
        groups: List[List[Reading]] = []
        open_group: Dict[str, List[Reading]] = {}  # último grupo de cada dispositivo
        for reading in readings:
            current = open_group.get(reading.device_id)
            if (
                current is not None
                and reading.is_deferred
                and current[0].is_deferred
                and len(current) < self.max_deferred
            ):
                current.append(reading)
                continue
            new_group = [reading]
            groups.append(new_group)
            open_group[reading.device_id] = new_group
        return groups
