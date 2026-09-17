"""The one local process: serves the task pane over HTTPS and drives the pipeline.

Static files and the WebSocket share a single origin (https://localhost:3000) so the
add-in and its API are same-origin and ``wss://`` needs no mixed-content exceptions.

Run with ``scripts/run.ps1`` or ``python -m service.main``.
"""
from __future__ import annotations

import contextlib
import logging
import os
import pathlib
import signal
import socket
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

# How long uvicorn may wait for connections to close before cutting them off.
SHUTDOWN_GRACE_S = 3.0


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
            # Close the task pane sockets ourselves. A WebSocket has no natural end, so
            # left alone it keeps uvicorn waiting for the connection to finish and the
            # terminal sits on "Shutting down" forever.
            log.info("closing task pane connections")
            await bridge.close_all()
            log.info("stopping pipeline")
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
                    await bridge.handle(socket, message)
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


def _check_port_free(host: str, port: int) -> None:
    """Fail before loading anything if the port is taken.

    Binding happens only after startup, so without this check a second instance
    registers a global hotkey and loads a 1.6 GB model before discovering it cannot
    serve — which is how stale processes end up holding the port, the hotkey and the
    microphone all at once.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((host, port))
            return
        except OSError:
            pass
    raise SystemExit(
        # Deliberately ASCII: this is raised before the console is switched to UTF-8,
        # so anything else arrives as mojibake.
        f"Port {port} is already in use - the service is probably already running.\n"
        "Stop it and clean up anything it left behind:\n"
        "    powershell -ExecutionPolicy Bypass -File scripts\\stop.ps1"
    )


def _handle_break(server: object) -> None:
    """Shut down on Ctrl+Break as well as Ctrl+C.

    uvicorn only captures SIGINT and SIGTERM, so without this Ctrl+Break kills the
    process outright and orphans ffmpeg and the microphone.
    """
    sigbreak = getattr(signal, "SIGBREAK", None)
    if sigbreak is None:
        return

    def handler(signum: int, frame: object) -> None:
        if getattr(server, "should_exit", False):
            os._exit(130)
        print("\nShutting down... press Ctrl+Break again to force quit.", flush=True)
        server.should_exit = True  # type: ignore[attr-defined]

    signal.signal(sigbreak, handler)


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
    _check_port_free(cfg.server.host, cfg.server.port)

    log.info("Task pane:  https://localhost:%d/taskpane.html", cfg.server.port)
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(cfg),
            host=cfg.server.host,
            port=cfg.server.port,
            ssl_certfile=crt,
            ssl_keyfile=key,
            log_level=cfg.debug.log_level.lower(),
            access_log=False,
            # Without this uvicorn waits indefinitely for connections to close, and a
            # task pane WebSocket never closes on its own.
            timeout_graceful_shutdown=SHUTDOWN_GRACE_S,
        )
    )
    _handle_break(server)
    server.run()

    # Everything that matters is already released: the pane sockets are closed, ffmpeg
    # is dead and the hotkey is unregistered. What remains is the interpreter's own
    # teardown, which joins the thread pool — and a Whisper transcription running there
    # cannot be interrupted, so `asyncio.run` would sit on it for up to five minutes
    # (THREAD_JOIN_TIMEOUT). Ctrl+C during dictation is exactly when that happens, which
    # is why the terminal appeared to hang on "Shutting down". Nothing needs flushing
    # beyond the streams, so leave immediately instead.
    log.info("stopped")
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        main()
