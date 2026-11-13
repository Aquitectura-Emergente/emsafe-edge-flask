"""Value objects del monitoring.

Los valores de los enums son los que viajan en el contrato MQTT: cambiarlos
rompe la ingesta del backend. No hay niveles de alerta: el Edge no evalúa límites.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum


class InvalidReading(ValueError):
    """Lectura que no supera la validación y no se publica."""


@dataclass(frozen=True)
class MagneticFluxDensity:
    """Densidad de flujo magnético en µT: número finito y no negativo."""

    micro_tesla: float

    def __post_init__(self) -> None:
        value = self.micro_tesla
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise InvalidReading(f"densidad de flujo no numérica: {value!r}")
        if math.isnan(value) or math.isinf(value):
            raise InvalidReading(f"densidad de flujo no finita: {value!r}")
        if value < 0:
            raise InvalidReading(f"densidad de flujo negativa: {value!r}")
        object.__setattr__(self, "micro_tesla", float(value))

    @classmethod
    def from_raw(cls, raw) -> "MagneticFluxDensity":
        if isinstance(raw, bool):
            raise InvalidReading(f"densidad de flujo no numérica: {raw!r}")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise InvalidReading(f"densidad de flujo no numérica: {raw!r}")
        return cls(value)

    def __float__(self) -> float:
        return self.micro_tesla


class SyncOrigin(Enum):
    """REALTIME: enviada al medirla. DEFERRED: venía del buffer offline del sensor."""

    REALTIME = "REALTIME"
    DEFERRED = "DEFERRED"

    @classmethod
    def from_flag(cls, deferred: int) -> "SyncOrigin":
        return cls.DEFERRED if deferred == 1 else cls.REALTIME


class ReadingQuality(Enum):
    """Calidad según el rango del sensor.

    SATURATED indica que el campo real fue al menos el medido, posiblemente
    mayor. Es una heurística sobre la magnitud del vector, no sobre un eje.
    Las saturadas se publican igual.
    """

    VALID = "VALID"
    SATURATED = "SATURATED"

    @classmethod
    def classify(cls, flux: MagneticFluxDensity, saturation_ut: float) -> "ReadingQuality":
        return cls.SATURATED if flux.micro_tesla >= saturation_ut else cls.VALID


class CapturedAtSource(Enum):
    """DEVICE_CLOCK: el sensor tenía hora. EDGE_RECEIVED: no la tenía (ts = 0) y se usa la recepción."""

    DEVICE_CLOCK = "DEVICE_CLOCK"
    EDGE_RECEIVED = "EDGE_RECEIVED"


class DeviceType(Enum):
    """Tipo de dispositivo. Este Edge solo maneja el sensor fijo de la vivienda."""

    PERSONAL_DOSIMETER = "PERSONAL_DOSIMETER"
    FIXED_SENSOR = "FIXED_SENSOR"


def _check_uint(name: str, value) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise InvalidReading(f"{name} debe ser un entero >= 0: {value!r}")


@dataclass(frozen=True)
class ReadingIdentity:
    """Identidad de una lectura para deduplicar: `deviceId + sequence + ts crudo`.

    Se usa el `ts` tal como llega por BLE, incluido el 0, y no `capturedAt`:
    con ts = 0, `capturedAt` sale de la hora de recepción y cambiaría en cada
    reenvío del buffer, así que los duplicados pasarían como lecturas nuevas.
    Dos arranques del sensor sin hora pueden chocar en el mismo `sequence`.
    """

    device_id: str
    sequence: int
    device_ts: int

    def __post_init__(self) -> None:
        if not isinstance(self.device_id, str) or not self.device_id.strip():
            raise InvalidReading("deviceId vacío")
        _check_uint("sequence", self.sequence)
        _check_uint("ts", self.device_ts)

    @property
    def has_device_clock(self) -> bool:
        return self.device_ts > 0


@dataclass(frozen=True)
class AlertThresholds:
    """Umbrales del espacio en µT: CAUTION desde `caution_ut`, DANGER desde `danger_ut`."""

    caution_ut: float
    danger_ut: float

    def __post_init__(self) -> None:
        for name in ("caution_ut", "danger_ut"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} debe ser numérico: {value!r}")
            if math.isnan(value) or math.isinf(value) or value <= 0:
                raise ValueError(f"{name} debe ser finito y mayor que 0: {value!r}")
            object.__setattr__(self, name, float(value))
        if self.caution_ut > self.danger_ut:
            raise ValueError("el umbral de CAUTION no puede superar al de DANGER")


class AlertLevel(Enum):
    """Nivel de alerta local, con su mensaje."""

    SAFE = ("SAFE", "Campo dentro de rango seguro. Equipo operando con normalidad.")
    CAUTION = ("CAUTION", "Exposicion moderada. Se recomienda mantener distancia de la fuente.")
    DANGER = ("DANGER", "Nivel critico de EMF. Se recomienda cortar la energia del equipo.")

    def __init__(self, wire_value: str, message: str) -> None:
        self._wire_value = wire_value
        self._message = message

    @property
    def wire_value(self) -> str:
        return self._wire_value

    @property
    def message(self) -> str:
        return self._message

    @classmethod
    def classify(
        cls, flux: MagneticFluxDensity, quality: ReadingQuality, thresholds: AlertThresholds
    ) -> "AlertLevel":
        """Umbrales inclusivos por abajo. Una saturada es DANGER: el campo fue al menos ese valor."""
        if quality is ReadingQuality.SATURATED:
            return cls.DANGER
        value = flux.micro_tesla
        if value >= thresholds.danger_ut:
            return cls.DANGER
        if value >= thresholds.caution_ut:
            return cls.CAUTION
        return cls.SAFE

    @property
    def requires_cut_off(self) -> bool:
        return self is AlertLevel.DANGER
