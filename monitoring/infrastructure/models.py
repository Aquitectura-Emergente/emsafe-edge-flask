"""Modelos ORM (Peewee) del outbox local."""
from __future__ import annotations

from datetime import datetime, timezone

from peewee import AutoField, CharField, FloatField, IntegerField, Model

from shared.database import db


class UtcDateTimeField(CharField):
    """Fecha UTC guardada como texto ISO-8601 con zona.

    El `DateTimeField` de Peewee descarta la zona. El texto con `+00:00`
    conserva el instante exacto y ordena bien al comparar.
    """

    def db_value(self, value):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("UtcDateTimeField no acepta horas sin zona")
        return value.astimezone(timezone.utc).isoformat(timespec="microseconds")

    def python_value(self, value):
        if value is None:
            return None
        return datetime.fromisoformat(value).astimezone(timezone.utc)


class ReadingStatus:
    PENDING = "PENDING"        # sin mensaje MQTT
    BATCHED = "BATCHED"        # en un mensaje que espera PUBACK
    PUBLISHED = "PUBLISHED"


class ReadingRow(Model):
    """Lectura del outbox.

    El índice único `(device_id, sequence, device_ts)` es la ReadingIdentity:
    la base rechaza los reenvíos y la fila original conserva el
    `captured_at` de la primera recepción.
    """

    id = AutoField()
    device_id = CharField()
    sequence = IntegerField()
    device_ts = IntegerField()
    flux_ut = FloatField()
    captured_at = UtcDateTimeField()
    captured_at_source = CharField()
    received_at = UtcDateTimeField()
    origin = CharField()
    quality = CharField()
    # Nivel local con los umbrales vigentes al recibirla. No se publica.
    level = CharField(default="SAFE")
    status = CharField(default=ReadingStatus.PENDING, index=True)
    message_id = CharField(null=True, index=True)
    # Hora del PUBACK. La limpieza solo mira esta columna: sin PUBACK no se borra.
    published_at = UtcDateTimeField(null=True, index=True)

    class Meta:
        database = db
        table_name = "outbox_readings"
        indexes = ((("device_id", "sequence", "device_ts"), True),)


class DeviceConfigRow(Model):
    """Última config válida del sensor, para escribir el umbral aunque no haya broker."""

    device_id = CharField(primary_key=True)
    config_version = IntegerField()
    alert_threshold_ut = FloatField()
    defined_by = CharField()
    updated_at = UtcDateTimeField(null=True)

    class Meta:
        database = db
        table_name = "device_configs"


ALL_MODELS = [ReadingRow, DeviceConfigRow]
