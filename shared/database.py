"""Base SQLite local del outbox.

`db` es un proxy: la base real se elige al arrancar con `init_db(path)`.
No hay migraciones; si el esquema cambia se borra el archivo, y solo se
pierde lo que estaba pendiente de publicar.
"""
from __future__ import annotations

from peewee import DatabaseProxy, SqliteDatabase

db = DatabaseProxy()


def init_db(path: str) -> SqliteDatabase:
    """Abre la base y crea las tablas que falten."""
    database = SqliteDatabase(
        path,
        pragmas={
            # WAL: el publicador lee mientras la ingesta escribe.
            "journal_mode": "wal",
            # Durable ante cortes de energía sin un fsync completo por lectura.
            "synchronous": "normal",
            "foreign_keys": 1,
        },
    )
    db.initialize(database)
    from monitoring.infrastructure.models import ALL_MODELS

    database.connect(reuse_if_open=True)
    database.create_tables(ALL_MODELS, safe=True)
    return database
