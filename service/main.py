"""The one local process: serves the task pane over HTTPS and drives the pipeline.

Static files and the WebSocket share a single origin (https://localhost:3000) so the
add-in and its API are same-origin and ``wss://`` needs no mixed-content exceptions.

Run with ``scripts/run.ps1`` or ``python -m service.main``.
"""
from __future__ import annotations

import contextlib
import logging
import pathlib
import sys
from collections.abc import AsyncIterator

from typing import Any

from fastapi import FastAPI, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from service import config as config_module
from service.console import enable_utf8
from service.bridge import WordBridge
from service.pipeline.orchestrator import Orchestrator

log = logging.getLogger("autowriter")


class NoCacheStatic(StaticFiles):
    """Serve the task pane with caching disabled.

    Both WebView2 (which hosts the pane inside Word) and the browser cache ES modules
    aggressively, and a stale module is indistinguishable from a bug: you edit a file,
    reload the add-in, and watch the old code run. Since everything is served from
    localhost, there is nothing to gain from caching it.
    """

    def file_response(self, *args: Any, **kwargs: Any) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-store, max-age=0"
        return response


def create_app(cfg: config_module.Config | None = None) -> FastAPI:
    cfg = cfg or config_module.load()

    bridge = WordBridge()
    orchestrator = Orchestrator(cfg, bridge)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await orchestrator.start()
        try:
            yield
        finally:
            await orchestrator.aclose()

    app = FastAPI(title="AI Autowriter", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.config = cfg
    app.state.bridge = bridge
    app.state.orchestrator = orchestrator

    @app.get("/health")
    async def health() -> JSONResponse:
        return JSONResponse(
            {
                "ok": True,
                "paneConnected": bridge.connected,
                **orchestrator.state(),
            }
        )

    @app.get("/")
    async def index() -> RedirectResponse:
        return RedirectResponse("/taskpane.html")

    @app.websocket("/ws")
    async def websocket_endpoint(socket: WebSocket) -> None:
        await socket.accept()
        await bridge.attach(socket)
        await orchestrator.on_pane_attached()
        try:
            while True:
                message = await socket.receive_json()
                if not isinstance(message, dict):
                    continue
                try:
                    await bridge.handle(message)
                except Exception:
                    log.exception("error handling pane message %s", message.get("type"))
        except WebSocketDisconnect:
            pass
        except Exception:
            log.exception("websocket error")
        finally:
            await bridge.detach(socket)

    taskpane_dir = cfg.root / "taskpane"
    app.mount("/", NoCacheStatic(directory=str(taskpane_dir), html=True), name="taskpane")
    return app


def _resolve_certs(cfg: config_module.Config) -> tuple[str, str]:
    crt, key = cfg.server.ssl_certfile, cfg.server.ssl_keyfile
    if crt and key and pathlib.Path(crt).exists() and pathlib.Path(key).exists():
        return crt, key
    raise SystemExit(
        "No HTTPS certificate found.\n"
        "Word will only load an add-in served over trusted HTTPS. Run:\n"
        "    npx --yes office-addin-dev-certs install\n"
        "then start the service again (or set ssl_certfile/ssl_keyfile in config.toml)."
    )


def main() -> None:
    import uvicorn

    enable_utf8()
    cfg = config_module.load()
    logging.basicConfig(
        level=getattr(logging, cfg.debug.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    # These libraries log every HTTP request at INFO, which buries our own output
    # behind hundreds of lines while a model downloads.
    for noisy in ("httpx", "httpcore", "huggingface_hub", "filelock", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    crt, key = _resolve_certs(cfg)

    log.info("Task pane:  https://localhost:%d/taskpane.html", cfg.server.port)
    uvicorn.run(
        create_app(cfg),
        host=cfg.server.host,
        port=cfg.server.port,
        ssl_certfile=crt,
        ssl_keyfile=key,
        log_level=cfg.debug.log_level.lower(),
        access_log=False,
    )


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        main()
