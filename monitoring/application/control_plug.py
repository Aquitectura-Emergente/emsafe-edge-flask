"""Caso de uso: controlar el enchufe inteligente y publicar su estado.

- La orden del usuario llega por MQTT (`iot/{plugId}/command`). Es idempotente
  por `commandId` y se ignora si `requestedAt` tiene más de 60 s.
- El corte automático es una opción de seguridad (US05): una lectura en tiempo
  real en DANGER apaga el enchufe y se restablece con histéresis, solo si lo
  cortó la regla automática. La orden del usuario tiene prioridad.
- Las diferidas describen el pasado y nunca accionan el relé.

El HTTP al actuador es bloqueante: el runner llama a estos métodos desde un
hilo, y un lock evita que una orden y una lectura se crucen.
"""
from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from datetime import datetime
from enum import Enum
from typing import Callable, Dict, Optional, Sequence

from monitoring.domain.entities import InvalidPlugCommand, PlugCommand, Reading
from monitoring.domain.ports import ActuatorGateway, Clock, PlugStatePublisher
from monitoring.domain.value_objects import (
    AlertThresholds,
    PlugAction,
    PlugState,
    PlugStateSource,
)

log = logging.getLogger(__name__)

RESTORE_RATIO = 0.8

CommandParser = Callable[[str, bytes], PlugCommand]


class CommandResult(Enum):
    APPLIED = "APPLIED"
    FAILED = "FAILED"          # el actuador no respondió; se publicó el estado sin cambios
    DUPLICATE = "DUPLICATE"    # commandId ya procesado
    STALE = "STALE"            # requestedAt fuera de la ventana permitida
    REJECTED = "REJECTED"      # no cumple el contrato


class ControlPlugUseCase:
    def __init__(
        self,
        actuator: Optional[ActuatorGateway],
        publisher: PlugStatePublisher,
        clock: Clock,
        thresholds_for: Callable[[str], AlertThresholds],
        plug_id: str,
        parser: CommandParser,
        auto_cutoff_enabled: bool = False,
        restore_ut: Optional[float] = None,
        max_command_age_s: float = 60.0,
        remembered_commands: int = 1000,
    ) -> None:
        self._actuator = actuator
        self._publisher = publisher
        self._clock = clock
        self._thresholds_for = thresholds_for
        self._plug_id = plug_id
        self._parser = parser
        self._auto_enabled = auto_cutoff_enabled
        self._restore_ut = restore_ut
        self._max_age_s = max_command_age_s
        self._remembered = remembered_commands
        self._seen: "OrderedDict[str, None]" = OrderedDict()
        self._lock = threading.Lock()
        self._state = PlugState.UNKNOWN
        self._source = PlugStateSource.STARTUP
        self._command_id: Optional[str] = None
        self._changed_at: datetime = clock.now()
        self._auto_cut = False       # el corte vigente lo hizo la regla automática
        self._user_allows = False    # el usuario encendió durante DANGER: no se vuelve a cortar
        self._last_error: Optional[str] = None

    @property
    def plug_id(self) -> str:
        return self._plug_id

    @property
    def enabled(self) -> bool:
        return self._actuator is not None

    def restore_threshold(self, device_id: str) -> float:
        danger = self._thresholds_for(device_id).danger_ut
        if self._restore_ut is not None and self._restore_ut < danger:
            return self._restore_ut
        return danger * RESTORE_RATIO

    # ---------- Orden del usuario ----------

    def handle_command(self, raw: bytes) -> CommandResult:
        try:
            command = self._parser(self._plug_id, raw)
        except InvalidPlugCommand as exc:
            log.warning("Orden del enchufe rechazada: %s", exc)
            return CommandResult.REJECTED

        with self._lock:
            if command.command_id in self._seen:
                log.info("Orden %s repetida: se ignora", command.command_id)
                return CommandResult.DUPLICATE
            self._remember(command.command_id)

            age = (self._clock.now() - command.requested_at).total_seconds()
            if abs(age) > self._max_age_s:
                log.warning(
                    "Orden %s ignorada: requestedAt fuera de la ventana de %.0f s (%.0f s)",
                    command.command_id, self._max_age_s, age,
                )
                return CommandResult.STALE

            target = PlugState(command.action.value)
            if self._switch(target):
                # El usuario manda: la regla automática no deshace su orden.
                self._auto_cut = False
                self._user_allows = command.action is PlugAction.ON
                self._set(target, PlugStateSource.USER, command.command_id)
                log.info("Enchufe %s: orden del usuario %s", target.value, command.command_id)
                return CommandResult.APPLIED

            log.warning(
                "Orden %s sin efecto: el actuador no respondió a %s; el enchufe sigue %s",
                command.command_id, target.value, self._state.value,
            )
            self._publish(self._state, PlugStateSource.USER, command.command_id, self._clock.now())
            return CommandResult.FAILED

    # ---------- Corte automático ----------

    def evaluate_latest(self, readings: Sequence[Reading]) -> Optional[PlugState]:
        """Decide con lecturas acumuladas: si alguna en tiempo real es DANGER, manda esa."""
        realtime = [r for r in readings if not r.is_deferred]
        if not realtime:
            return None
        dangerous = [r for r in realtime if r.level.requires_cut_off]
        return self.evaluate(dangerous[-1] if dangerous else realtime[-1])

    def evaluate(self, reading: Reading) -> Optional[PlugState]:
        """Aplica la regla automática. Devuelve el estado nuevo si accionó el relé."""
        if reading.is_deferred or not self._auto_enabled or self._actuator is None:
            return None
        with self._lock:
            value = reading.flux.micro_tesla
            if reading.level.requires_cut_off:
                if self._user_allows:
                    return None
                target, reason = PlugState.OFF, f"DANGER ({value:.1f} µT)"
                # Aunque ya estuviera apagado, desde ahora lo mantiene la regla automática.
                self._auto_cut = True
            elif value < self.restore_threshold(reading.device_id):
                # Fin del episodio: la próxima vez DANGER vuelve a cortar.
                self._user_allows = False
                if not self._auto_cut:
                    return None
                target, reason = PlugState.ON, f"campo bajo el umbral de restablecimiento ({value:.1f} µT)"
            else:
                return None

            if target is self._state:
                return None
            if not self._switch(target):
                log.warning("Enchufe sin cambio: el actuador no respondió a %s (%s)", target.value, reason)
                return None
            if target is PlugState.ON:
                self._auto_cut = False
            self._set(target, PlugStateSource.AUTO, None)
            log.info("Enchufe %s: %s", target.value, reason)
            return target

    # ---------- Estado ----------

    def publish_state(self) -> bool:
        """Republica el estado vigente, p. ej. al conectar con el broker."""
        with self._lock:
            return self._publish(self._state, self._source, self._command_id, self._changed_at)

    def status(self) -> Dict[str, object]:
        return {
            "enabled": self.enabled,
            "plugId": self._plug_id,
            "autoCutoffEnabled": self._auto_enabled,
            "state": self._state.value,
            "source": self._source.value,
            "commandId": self._command_id,
            "autoCut": self._auto_cut,
            "lastError": self._last_error,
        }

    # ---------- Internos ----------

    def _switch(self, target: PlugState) -> bool:
        if self._actuator is None:
            self._last_error = "sin actuador configurado (EMSAFE_ACTUATOR_URL)"
            return False
        ok = self._actuator.turn_off() if target is PlugState.OFF else self._actuator.turn_on()
        self._last_error = None if ok else f"el actuador no respondió a {target.value}"
        return ok

    def _set(self, state: PlugState, source: PlugStateSource, command_id: Optional[str]) -> None:
        self._state, self._source, self._command_id = state, source, command_id
        self._changed_at = self._clock.now()
        self._publish(state, source, command_id, self._changed_at)

    def _publish(self, state, source, command_id, changed_at) -> bool:
        ok = self._publisher.publish_plug_state(self._plug_id, state, command_id, source, changed_at)
        if not ok:
            log.info("Estado del enchufe sin publicar (broker no disponible): se publicará al reconectar")
        return ok

    def _remember(self, command_id: str) -> None:
        self._seen[command_id] = None
        while len(self._seen) > self._remembered:
            self._seen.popitem(last=False)
