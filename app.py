"""Punto de entrada del EMSafe Edge Hogar.

Flask corre en el hilo principal y el Edge (BLE + MQTT + enchufe) en un hilo
con su propio loop asyncio. Ctrl+C detiene Flask y luego apaga el Edge en orden.

Uso:
    python app.py
"""
from __future__ import annotations

import logging
import os
import sys

from flask import Flask

from monitoring.interfaces.api import create_monitoring_api


def create_app(history=None, health=None) -> Flask:
    app = Flask(__name__)
    app.register_blueprint(create_monitoring_api(history, health))

    @app.teardown_request
    def _close_db(_exc):
        # Cada request corre en su hilo; la conexión SQLite es por hilo.
        from shared.database import db

        if db.obj is not None and not db.is_closed():
            db.close()

    return app


# Sin Edge: sirve para los tests y para `flask --app app routes`.
app = create_app()


def _debug_enabled() -> bool:
    """Debug de Flask solo por entorno: con host 0.0.0.0 la consola de Werkzeug
    permitiría ejecutar código en el equipo desde la red local."""
    return os.getenv("EMSAFE_EDGE_DEBUG", "").strip().lower() in ("1", "true", "yes")


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    # bleak es muy verboso en DEBUG y werkzeug registra cada request.
    logging.getLogger("bleak").setLevel(logging.WARNING)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)


def main() -> int:
    from monitoring.infrastructure.sqlite_outbox import SqliteReadingOutbox
    from monitoring.interfaces.edge_thread import EdgeThread
    from shared.config import EdgeSettings

    log = logging.getLogger("main")
    try:
        settings = EdgeSettings.from_env()
    except ValueError as exc:
        _configure_logging("INFO")
        log.error("Configuración inválida: %s", exc)
        return 2
    _configure_logging(settings.log_level)
    log.info(
        "Nodo %s (organización %s), sensor %s, broker %s:%s, actuador %s, base %s",
        settings.edge_node_id, settings.organization_id, settings.device_name,
        settings.mqtt_host, settings.mqtt_port, settings.actuator_url or "desactivado",
        settings.db_path,
    )

    edge = EdgeThread(settings)
    edge.start()
    web = create_app(SqliteReadingOutbox(), edge.health)
    try:
        # Sin recargador: duplicaría el proceso y con él la conexión BLE.
        web.run(host="0.0.0.0", port=5000, debug=_debug_enabled(), use_reloader=False)
    except KeyboardInterrupt:
        pass
    finally:
        edge.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
