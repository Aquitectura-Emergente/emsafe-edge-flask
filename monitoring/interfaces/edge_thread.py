"""Corre el loop asyncio del Edge (BLE + MQTT) en un hilo propio, junto a Flask."""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Callable, Dict, Optional

log = logging.getLogger(__name__)


class EdgeThread:
    def __init__(self, settings, build: Optional[Callable] = None) -> None:
        if build is None:
            from monitoring.interfaces.runner import build_runner as build
        self._settings = settings
        self._build = build
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ready = threading.Event()
        self._error: Optional[BaseException] = None
        self.runner = None

    def start(self, timeout_s: float = 15.0) -> None:
        """Arranca el hilo y espera a que el Edge esté armado. Relanza un error del armado."""
        self._thread = threading.Thread(target=self._run, name="edge-asyncio", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout_s):
            raise RuntimeError("el Edge no terminó de arrancar a tiempo")
        if self._error is not None:
            raise self._error

    def stop(self, timeout_s: float = 10.0) -> None:
        """Apagado ordenado: BLE, tareas y MQTT con su OFFLINE."""
        loop, runner = self._loop, self.runner
        if loop is not None and runner is not None and loop.is_running():
            loop.call_soon_threadsafe(runner.stop)
        if self._thread is not None:
            self._thread.join(timeout_s)
            if self._thread.is_alive():
                log.warning("El Edge no se detuvo en %.0f s", timeout_s)

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def health(self) -> Dict[str, Any]:
        if self.runner is None or not self.alive:
            return {"edge": "DOWN"}
        return {"edge": "UP", **self.runner.health()}

    def _run(self) -> None:
        try:
            asyncio.run(self._main())
        except BaseException as exc:
            if not self._ready.is_set():
                self._error = exc
                self._ready.set()
            else:
                log.exception("El loop del Edge terminó con error")

    async def _main(self) -> None:
        self._loop = asyncio.get_running_loop()
        self.runner = self._build(self._settings)
        self._ready.set()
        await self.runner.run()
