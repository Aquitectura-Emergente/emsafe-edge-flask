"""Configuración del Edge, leída de variables de entorno.

Las credenciales llegan solo por entorno. `from_env` recibe el entorno como
argumento para poder probarlo sin tocar `os.environ`.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Mapping, Optional, Tuple

_TRUE = {"1", "true", "yes", "si", "sí", "on"}
_FALSE = {"0", "false", "no", "off", ""}


def _text(env: Mapping[str, str], name: str, default: str) -> str:
    value = env.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{name} debe ser numérico (recibido: {raw!r})")
    if value != value or value in (float("inf"), float("-inf")):
        raise ValueError(f"{name} debe ser un número finito (recibido: {raw!r})")
    return value


def _int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{name} debe ser un entero (recibido: {raw!r})")


def _bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ValueError(f"{name} debe ser true/false (recibido: {raw!r})")


def _optional_float(env: Mapping[str, str], name: str) -> Optional[float]:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return None
    return _float(env, name, 0.0)


@dataclass(frozen=True)
class EdgeSettings:
    """Parámetros del nodo, leídos una vez al arrancar."""

    edge_node_id: str = "EDGE-HOGAR-01"
    organization_id: str = "org-demo"
    device_name: str = "EMSafe-DOS-01"
    caution_ut: float = 100.0
    danger_ut: float = 200.0
    # None: 80 % del umbral de DANGER vigente.
    restore_ut: Optional[float] = None
    saturation_ut: float = 790.0
    # Corte automático por DANGER, como opción de seguridad (US05).
    auto_cutoff_enabled: bool = False
    # Vacío: sin actuador.
    actuator_url: str = ""
    plug_id: str = "EMSafe-PLUG-01"
    # La API local no tiene autenticación: por defecto solo atiende al propio equipo.
    http_host: str = "127.0.0.1"
    mqtt_host: str = "localhost"
    mqtt_port: int = 1883
    mqtt_tls: bool = False
    # Fuera del repr para que no aparezcan en logs.
    mqtt_username: Optional[str] = field(default=None, repr=False)
    mqtt_password: Optional[str] = field(default=None, repr=False)
    db_path: str = "emsafe_edge.db"
    # Días que se conservan las lecturas YA publicadas (sirven para descartar
    # reenvíos del sensor). Las pendientes nunca se borran por antigüedad.
    outbox_retention_days: int = 7
    log_level: str = "INFO"

    def __post_init__(self) -> None:
        if self.outbox_retention_days < 1:
            raise ValueError("EMSAFE_OUTBOX_RETENTION_DAYS debe ser al menos 1")
        if not 0 < self.saturation_ut:
            raise ValueError("EMSAFE_SATURATION_UT debe ser mayor que 0")
        if not 0 < self.caution_ut <= self.danger_ut:
            raise ValueError("EMSAFE_CAUTION_UT debe ser mayor que 0 y no superar EMSAFE_DANGER_UT")
        # DANGER se escribe en el sensor: tiene que poder dispararse antes de saturar.
        if not self.danger_ut < self.saturation_ut:
            raise ValueError(
                f"EMSAFE_DANGER_UT debe ser menor que EMSAFE_SATURATION_UT ({self.saturation_ut})"
            )
        if self.restore_ut is not None and not 0 < self.restore_ut < self.danger_ut:
            raise ValueError("EMSAFE_RESTORE_UT debe ser mayor que 0 y menor que EMSAFE_DANGER_UT")
        if not 0 < self.mqtt_port < 65536:
            raise ValueError("EMSAFE_MQTT_PORT debe estar entre 1 y 65535")
        if self.log_level.upper() not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"EMSAFE_LOG_LEVEL no válido: {self.log_level!r}")

    @property
    def device_names(self) -> Tuple[str, ...]:
        return (self.device_name,)

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "EdgeSettings":
        env = os.environ if env is None else env
        d = cls()
        return cls(
            edge_node_id=_text(env, "EMSAFE_EDGE_NODE_ID", d.edge_node_id),
            organization_id=_text(env, "EMSAFE_ORGANIZATION_ID", d.organization_id),
            device_name=_text(env, "EMSAFE_DEVICE_NAME", d.device_name),
            caution_ut=_float(env, "EMSAFE_CAUTION_UT", d.caution_ut),
            danger_ut=_float(env, "EMSAFE_DANGER_UT", d.danger_ut),
            restore_ut=_optional_float(env, "EMSAFE_RESTORE_UT"),
            saturation_ut=_float(env, "EMSAFE_SATURATION_UT", d.saturation_ut),
            auto_cutoff_enabled=_bool(env, "EMSAFE_AUTO_CUTOFF_ENABLED", d.auto_cutoff_enabled),
            actuator_url=_text(env, "EMSAFE_ACTUATOR_URL", d.actuator_url).rstrip("/"),
            plug_id=_text(env, "EMSAFE_PLUG_ID", d.plug_id),
            http_host=_text(env, "EMSAFE_HTTP_HOST", d.http_host),
            mqtt_host=_text(env, "EMSAFE_MQTT_HOST", d.mqtt_host),
            mqtt_port=_int(env, "EMSAFE_MQTT_PORT", d.mqtt_port),
            mqtt_tls=_bool(env, "EMSAFE_MQTT_TLS", d.mqtt_tls),
            mqtt_username=env.get("EMSAFE_MQTT_USERNAME") or None,
            mqtt_password=env.get("EMSAFE_MQTT_PASSWORD") or None,
            db_path=_text(env, "EMSAFE_DB_PATH", d.db_path),
            outbox_retention_days=_int(
                env, "EMSAFE_OUTBOX_RETENTION_DAYS", d.outbox_retention_days
            ),
            log_level=_text(env, "EMSAFE_LOG_LEVEL", d.log_level).upper(),
        )
