"""Configuración del sensor sobre SQLite (Peewee). Implementa `ConfigStore`."""
from __future__ import annotations

from typing import Optional

from monitoring.domain.entities import DeviceConfig
from monitoring.infrastructure.models import DeviceConfigRow


class SqliteConfigStore:
    def get(self, device_id: str) -> Optional[DeviceConfig]:
        row = DeviceConfigRow.get_or_none(DeviceConfigRow.device_id == device_id)
        if row is None:
            return None
        # Ya se validó al recibirla.
        return DeviceConfig(
            device_id=row.device_id,
            config_version=row.config_version,
            alert_threshold_ut=row.alert_threshold_ut,
            defined_by=row.defined_by,
            updated_at=row.updated_at,
        )

    def save(self, config: DeviceConfig) -> None:
        (
            DeviceConfigRow.insert(
                device_id=config.device_id,
                config_version=config.config_version,
                alert_threshold_ut=config.alert_threshold_ut,
                defined_by=config.defined_by,
                updated_at=config.updated_at,
            )
            .on_conflict_replace()
            .execute()
        )
