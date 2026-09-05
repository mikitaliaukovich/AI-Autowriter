"""Async RPC bridge to the Word task pane.

The rest of the service treats Word as a small awaitable API — ``read_context()`` and
``apply(ops)`` — and this module hides the fact that both are round trips over a
WebSocket to a browser pane that may disconnect at any moment.

**Several panes may be connected at once**, and only one of them can be the one we
write to. An earlier version simply closed the previous socket whenever a new one
arrived, which turned two live panes (real Word plus a dev preview) into an endless
reconnect war: each kicked the other out, both reconnected, and every in-flight request
died with ``NotConnected``. So connections are now kept, one is designated *active*, and
a pane running in real Word always outranks a preview.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from service import protocol
from service.protocol import ApplyResult, Capabilities, DocumentContext

log = logging.getLogger(__name__)

# A pane round trip is local and should take milliseconds. If Word is busy (a modal
# dialog, a huge repaginate) we would rather fail loudly than hang the pipeline.
RPC_TIMEOUT_S = 10.0


class NotConnected(RuntimeError):
    """Raised when an operation needs the task pane but none is attached."""


@dataclass
class Client:
    """One connected pane."""

    socket: Any
    kind: str = "word"          # "word" (the real add-in) or "preview" (a dev harness)
    capabilities: Capabilities = field(default_factory=Capabilities)
    doc_title: str = ""

    @property
    def rank(self) -> int:
        """Higher wins. A real Word pane always outranks a preview."""
        return 1 if self.kind == "word" else 0


class WordBridge:
    def __init__(self) -> None:
        self._clients: list[Client] = []
        self._active: Client | None = None
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._lock = asyncio.Lock()
        # Latest context pushed by the active pane on caret movement.
        self.last_context: DocumentContext | None = None
        self.on_command: Any = None      # set by the app: async (kind, message) -> None
        self.on_connect: Any = None      # set by the app: async () -> None

    # --- connection lifecycle ---------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._active is not None

    @property
    def capabilities(self) -> Capabilities:
        return self._active.capabilities if self._active else Capabilities()

    @property
    def doc_title(self) -> str:
        return self._active.doc_title if self._active else ""

    @property
    def client_count(self) -> int:
        return len(self._clients)

    def _find(self, socket: Any) -> Client | None:
        return next((c for c in self._clients if c.socket is socket), None)

    async def attach(self, socket: Any) -> None:
        """Register a new pane. Nothing else is disturbed."""
        if self._find(socket) is None:
            self._clients.append(Client(socket=socket))
        if self._active is None:
            self._promote(self._clients[-1])

    async def detach(self, socket: Any) -> None:
        client = self._find(socket)
        if client is None:
            return
        self._clients.remove(client)
        if self._active is client:
            self._active = None
            self.last_context = None
            self._fail_pending(NotConnected("task pane disconnected"))
            best = max(self._clients, key=lambda c: c.rank, default=None)
            if best is not None:
                self._promote(best)
                log.info("active pane fell back to %s", best.kind)

    def _promote(self, client: Client) -> None:
        if self._active is client:
            return
        previous = self._active
        self._active = client
        if previous is not None:
            # Requests aimed at the old pane can never be answered now.
            self.last_context = None
            self._fail_pending(NotConnected("active task pane changed"))

    def _fail_pending(self, exc: BaseException) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()

    async def close_all(self) -> None:
        for client in list(self._clients):
            with contextlib.suppress(Exception):
                await client.socket.close()
        self._clients.clear()
        self._active = None

    # --- outbound ---------------------------------------------------------------------

    async def send(self, message: dict[str, Any]) -> None:
        client = self._active
        if client is None:
            raise NotConnected("no task pane attached")
        await client.socket.send_json(message)

    async def try_send(self, message: dict[str, Any]) -> bool:
        """Fire-and-forget for status updates, where a missing pane is not an error."""
        try:
            await self.send(message)
            return True
        except Exception:
            return False

    async def broadcast(self, message: dict[str, Any]) -> None:
        """Status updates go to every pane, so a preview also shows the truth."""
        for client in list(self._clients):
            try:
                await client.socket.send_json(message)
            except Exception:
                pass

    async def _rpc(self, build: Any) -> dict[str, Any]:
        req_id = uuid.uuid4().hex[:12]
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[req_id] = future
        try:
            await self.send(build(req_id))
            return await asyncio.wait_for(future, RPC_TIMEOUT_S)
        except asyncio.TimeoutError as exc:
            raise NotConnected("the task pane did not answer in time") from exc
        finally:
            self._pending.pop(req_id, None)

    async def read_context(self) -> DocumentContext:
        """Ask the active pane for a fresh window around the caret."""
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

    async def handle(self, socket: Any, message: dict[str, Any]) -> None:
        client = self._find(socket)
        if client is None:
            return
        kind = message.get("type")

        req_id = message.get("reqId")
        if req_id and (future := self._pending.get(req_id)) and not future.done():
            future.set_result(message)
            return

        if kind == "hello":
            client.kind = "preview" if message.get("client") == "preview" else "word"
            client.capabilities = Capabilities(
                word_api=message.get("wordApi") or {},
                platform=str(message.get("platform") or ""),
                version=str(message.get("version") or ""),
            )
            client.doc_title = str(message.get("docTitle") or "")
            log.info(
                "pane connected: %s (%s %s); %d attached",
                client.kind, client.capabilities.platform, client.capabilities.version,
                len(self._clients),
            )
            # A real Word pane takes over from a preview; a newer Word pane takes over
            # from an older one (the add-in was reloaded).
            if self._active is None or client.rank > self._active.rank or (
                client.kind == "word" and self._active.kind == "word" and self._active is not client
            ):
                self._promote(client)
            if self.on_connect:
                await self.on_connect()

        elif kind == "context":
            # An unsolicited push (the user moved the caret). Only the pane we write to
            # is allowed to define what "the context" is.
            if client is self._active:
                self.last_context = _context_from_payload(message)

        elif kind in ("command", "dictate"):
            if client is not self._active:
                self._promote(client)      # the pane the user is actually driving
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
