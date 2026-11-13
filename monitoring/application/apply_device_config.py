"""Caso de uso: aplicar la configuración que llega de la nube.

El umbral de la nube reemplaza al de DANGER del espacio: se valida, se guarda
y se escribe en el sensor al conectar (vía `threshold_for`) y en caliente
cuando cambia, así el LED y el buzzer coinciden con el corte del enchufe.
"""
from __future__ import annotations

import logging
from enum import Enum
from typing import Callable, Set

from monitoring.domain.entities import DeviceConfig, InvalidDeviceConfig
from monitoring.domain.ports import ConfigStore, SensorLink

log = logging.getLogger(__name__)

ConfigParser = Callable[[str, bytes], DeviceConfig]


class ConfigResult(Enum):
    APPLIED = "APPLIED"
    IGNORED = "IGNORED"      # versión menor o igual a la vigente
    REJECTED = "REJECTED"
    CLEARED = "CLEARED"      # retained borrado: se conserva la vigente


class ApplyDeviceConfigUseCase:
    def __init__(
        self,
        store: ConfigStore,
        link: SensorLink,
        parser: ConfigParser,
        default_threshold_ut: float,
    ) -> None:
        self._store = store
        self._link = link
        self._parser = parser
        self._default_threshold_ut = default_threshold_ut
        self._warned_default: Set[str] = set()

    async def on_config_message(self, device_id: str, raw: bytes) -> ConfigResult:
        if not raw:
            # Un retained vacío solo borra el mensaje del broker; no es una
            # orden de volver al umbral por defecto.
            log.info("Config retained de %s borrada en el broker: se mantiene la vigente", device_id)
            return ConfigResult.CLEARED

        try:
            config = self._parser(device_id, raw)
        except InvalidDeviceConfig as exc:
            log.warning("Config de %s rechazada: %s", device_id, exc)
            return ConfigResult.REJECTED

        current = self._store.get(device_id)
        if not config.supersedes(current):
            log.info(
                "Config de %s ignorada: versión %s y la vigente es %s",
                device_id, config.config_version, current.config_version,
            )
            return ConfigResult.IGNORED

        self._store.save(config)
        log.info(
            "Config de %s aplicada: versión %s, umbral %.1f µT (definido por %s)",
            device_id, config.config_version, config.alert_threshold_ut, config.defined_by,
        )
        if self._link.is_connected(device_id):
            await self._write(device_id, config.alert_threshold_ut)
        return ConfigResult.APPLIED

    def threshold_for(self, device_id: str) -> float:
        """Umbral de DANGER vigente; sin config de la nube, EMSAFE_DANGER_UT."""
        config = self._store.get(device_id)
        if config is not None:
            return config.alert_threshold_ut
        if device_id not in self._warned_default:
            log.info(
                "Sin configuración de la nube para %s: se usa el umbral de DANGER %.1f µT "
                "(EMSAFE_DANGER_UT)",
                device_id, self._default_threshold_ut,
            )
            self._warned_default.add(device_id)
        return self._default_threshold_ut

    async def _write(self, device_id: str, threshold_ut: float) -> None:
        try:
            await self._link.write_threshold(device_id, threshold_ut)
        except Exception as exc:
            log.warning(
                "No se pudo escribir el umbral en %s (%s): se escribirá al reconectar",
                device_id, exc,
            )
