"""Caso de uso: decidir el estado del enchufe inteligente a partir de las lecturas.

- La orden del usuario (ON/OFF) gana sobre la lógica automática.
- Corte automático: una lectura en tiempo real en DANGER apaga el enchufe.
  Se restablece con histéresis, cuando el campo baja del umbral de
  restablecimiento, y solo si lo cortó la lógica automática.
- Las diferidas solo se registran: describen el pasado y no accionan el relé.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable, Dict, Optional, Sequence

from monitoring.domain.entities import Reading
from monitoring.domain.ports import ActuatorGateway, Clock, DesiredPlugSource
from monitoring.domain.value_objects import AlertThresholds

log = logging.getLogger(__name__)

ON, OFF = "ON", "OFF"
RESTORE_RATIO = 0.8


class ControlPlugUseCase:
    def __init__(
        self,
        actuator: Optional[ActuatorGateway],
        desired_source: Optional[DesiredPlugSource],
        clock: Clock,
        thresholds_for: Callable[[str], AlertThresholds],
        auto_cutoff_enabled: bool = True,
        restore_ut: Optional[float] = None,
        desired_poll_s: float = 5.0,
    ) -> None:
        self._actuator = actuator
        self._desired_source = desired_source
        self._clock = clock
        self._thresholds_for = thresholds_for
        self._auto_enabled = auto_cutoff_enabled
        self._restore_ut = restore_ut
        self._desired_poll_s = desired_poll_s
        self._plug: Optional[str] = None  # None: estado desconocido
        self._auto_cut = False  # el corte vigente lo mantiene la lógica automática
        self._desired: Optional[str] = None
        self._desired_at: Optional[datetime] = None
        self._last_error: Optional[str] = None

    @property
    def enabled(self) -> bool:
        return self._actuator is not None

    def restore_threshold(self, device_id: str) -> float:
        danger = self._thresholds_for(device_id).danger_ut
        if self._restore_ut is not None and self._restore_ut < danger:
            return self._restore_ut
        return danger * RESTORE_RATIO

    def evaluate_latest(self, readings: Sequence[Reading]) -> Optional[str]:
        """Decide con lecturas acumuladas: si alguna en tiempo real es DANGER, manda esa."""
        realtime = [r for r in readings if not r.is_deferred]
        if not realtime:
            return None
        dangerous = [r for r in realtime if r.level.requires_cut_off]
        return self.evaluate(dangerous[-1] if dangerous else realtime[-1])

    def evaluate(self, reading: Reading) -> Optional[str]:
        """Aplica las reglas a una lectura. Devuelve la orden enviada con éxito, o None."""
        if reading.is_deferred or self._actuator is None:
            return None

        value = reading.flux.micro_tesla
        desired = self._desired_plug(reading.device_id)
        if desired in (ON, OFF):
            target, reason = desired, "orden del usuario"
            self._auto_cut = False
        elif not self._auto_enabled:
            return None
        elif reading.level.requires_cut_off:
            target, reason = OFF, f"DANGER ({value:.1f} µT)"
            # Aunque ya estuviera apagado, desde ahora lo mantiene la lógica automática.
            self._auto_cut = True
        elif self._auto_cut and value < self.restore_threshold(reading.device_id):
            target, reason = ON, f"campo bajo el umbral de restablecimiento ({value:.1f} µT)"
        else:
            return None

        if target == self._plug:
            return None
        ok = self._actuator.turn_off() if target == OFF else self._actuator.turn_on()
        if not ok:
            self._last_error = f"el actuador no respondió a {target}"
            log.warning("Enchufe sin cambio: el actuador no respondió a %s (%s)", target, reason)
            return None
        self._plug = target
        self._last_error = None
        if target == ON:
            self._auto_cut = False
        log.info("Enchufe %s: %s", target, reason)
        return target

    def status(self) -> Dict[str, object]:
        return {
            "enabled": self.enabled,
            "autoCutoffEnabled": self._auto_enabled,
            "plug": self._plug,
            "autoCut": self._auto_cut,
            "userOrder": self._desired,
            "lastError": self._last_error,
        }

    def _desired_plug(self, device_id: str) -> Optional[str]:
        if self._desired_source is None:
            return None
        now = self._clock.now()
        if self._desired_at is None or (now - self._desired_at).total_seconds() >= self._desired_poll_s:
            value = self._desired_source.fetch_desired_plug(device_id)
            self._desired = value.upper() if isinstance(value, str) else None
            self._desired_at = now
        return self._desired
