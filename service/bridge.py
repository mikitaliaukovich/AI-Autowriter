"""Async RPC bridge to the Word task pane.

The rest of the service treats Word as a small awaitable API — ``read_context()`` and
``apply(ops)`` — and this module hides the fact that both are round trips over a
WebSocket to a browser pane that may disconnect at any moment.

Only one pane is connected at a time; a second connection replaces the first, which is
what you want when Word reloads the add-in.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from typing import Any

from service import protocol
from service.protocol import ApplyResult, Capabilities, DocumentContext

log = logging.getLogger(__name__)

# A pane round trip is local and should take milliseconds. If Word is busy (a modal
# dialog, a huge repaginate) we would rather fail loudly than hang the pipeline.
RPC_TIMEOUT_S = 10.0


class NotConnected(RuntimeError):
    """Raised when an operation needs the task pane but none is attached."""


class WordBridge:
    def __init__(self) -> None:
        self._socket: Any | None = None
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._lock = asyncio.Lock()
        self.capabilities = Capabilities()
        self.doc_title: str = ""
        # Latest context pushed by the pane on caret movement. Used as a fast path so a
        # dictation does not have to wait for a round trip that just happened anyway.
        self.last_context: DocumentContext | None = None
        self.on_command: Any = None      # set by the app: async (name, payload) -> None
        self.on_connect: Any = None      # set by the app: async () -> None

    # --- connection lifecycle ---------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._socket is not None

    async def attach(self, socket: Any) -> None:
        old = self._socket
        self._socket = socket
        if old is not None:
            with contextlib.suppress(Exception):
                await old.close()
        self._fail_pending(NotConnected("task pane reconnected"))

    async def detach(self, socket: Any) -> None:
        if self._socket is socket:
            self._socket = None
            self.last_context = None
            self._fail_pending(NotConnected("task pane disconnected"))

    def _fail_pending(self, exc: BaseException) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()

    # --- outbound ---------------------------------------------------------------------

    async def send(self, message: dict[str, Any]) -> None:
        socket = self._socket
        if socket is None:
            raise NotConnected("no task pane attached")
        await socket.send_json(message)

    async def try_send(self, message: dict[str, Any]) -> bool:
        """Fire-and-forget for status updates, where a missing pane is not an error."""
        try:
            await self.send(message)
            return True
        except Exception:
            return False

    async def _rpc(self, build: Any) -> dict[str, Any]:
        req_id = uuid.uuid4().hex[:12]
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[req_id] = future
        try:
            await self.send(build(req_id))
            return await asyncio.wait_for(future, RPC_TIMEOUT_S)
        finally:
            self._pending.pop(req_id, None)

    async def read_context(self) -> DocumentContext:
        """Ask the pane for a fresh window around the caret."""
        payload = await self._rpc(protocol.msg_request_context)
        ctx = _context_from_payload(payload)
        self.last_context = ctx
        return ctx

    async def apply(self, ops: list[dict[str, Any]], meta: dict[str, Any] | None = None) -> ApplyResult:
        """Run an op batch against the document.

        Serialised with a lock: two batches interleaving would invalidate each other's
        paragraph ids, since ids are positions relative to the caret.
        """
        async with self._lock:
            payload = await self._rpc(lambda rid: protocol.msg_apply(rid, ops, meta))
        try:
            return ApplyResult.model_validate(payload.get("result") or {})
        except Exception as exc:  # a malformed reply must not kill the pipeline
            log.warning("malformed applyResult: %s", exc)
            return ApplyResult(ok=False, error=str(exc))

    # --- inbound ----------------------------------------------------------------------

    async def handle(self, message: dict[str, Any]) -> None:
        kind = message.get("type")

        req_id = message.get("reqId")
        if req_id and (future := self._pending.get(req_id)) and not future.done():
            future.set_result(message)
            return

        if kind == "hello":
            self.capabilities = Capabilities(
                word_api=message.get("wordApi") or {},
                platform=str(message.get("platform") or ""),
                version=str(message.get("version") or ""),
            )
            self.doc_title = str(message.get("docTitle") or "")
            log.info("task pane connected: %s %s", self.capabilities.platform, self.capabilities.version)
            if self.on_connect:
                await self.on_connect()

        elif kind == "context":
            # An unsolicited push (the user moved the caret).
            self.last_context = _context_from_payload(message)

        elif kind in ("command", "dictate"):
            if self.on_command:
                await self.on_command(kind, message)

        elif kind == "log":
            log.info("pane: %s", message.get("message"))

        else:
            log.debug("unhandled pane message: %s", kind)


def _context_from_payload(payload: dict[str, Any]) -> DocumentContext:
    return DocumentContext(
        paragraphs=payload.get("paragraphs") or [],
        at_end_of_paragraph=bool(payload.get("atEndOfParagraph", True)),
        selection_text=str(payload.get("selectionText") or ""),
        doc_title=str(payload.get("docTitle") or ""),
    )
