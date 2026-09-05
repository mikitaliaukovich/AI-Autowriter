"""WebSocket message shapes shared by the service and the task pane.

One socket carries everything. Messages are JSON objects with a ``type`` discriminator.

Task pane -> service
    hello         capabilities of this Word build, document title
    context       a context window, in reply to ``requestContext`` or pushed on caret move
    applyResult   outcome of an ``apply``, including per-op conflicts
    command       user pressed a button in the pane (start/stop/undo/reload)
    dictate       debug: text typed into the pane, injected as if it had been spoken

Service -> task pane
    state         listening / model readiness / device, for the status strip
    requestContext  please send a fresh window (carries reqId)
    apply         ops to run against the document (carries reqId)
    transcript    what was heard, partial or final
    timing        per-stage latency for the footer readout
    log           a line for the pane's activity log
"""
from __future__ import annotations

import hashlib
from typing import Any, Literal

from pydantic import BaseModel, Field

PROTOCOL_VERSION = 1


def text_hash(text: str) -> str:
    """Short digest used for optimistic concurrency on a paragraph.

    The task pane computes the same digest before applying an op; a mismatch means the
    paragraph changed after we read it (the user typed something), so the batch is
    rejected rather than applied to the wrong text.
    """
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


class ContextParagraph(BaseModel):
    id: str
    text: str = ""
    style: str = "normal"
    hash: str = ""
    caret: int | None = None       # offset of the caret within this paragraph, if it is P0
    empty: bool = False


class DocumentContext(BaseModel):
    """The window of the document the model is allowed to see and address."""

    paragraphs: list[ContextParagraph] = Field(default_factory=list)
    at_end_of_paragraph: bool = True
    selection_text: str = ""
    doc_title: str = ""

    @property
    def ids(self) -> set[str]:
        return {p.id for p in self.paragraphs}

    def get(self, pid: str) -> ContextParagraph | None:
        return next((p for p in self.paragraphs if p.id == pid), None)

    @property
    def anchor(self) -> ContextParagraph | None:
        return self.get("P0")

    def render(self, max_chars: int = 4000) -> str:
        """Format the window for the prompt.

        The caret is marked inline with ``⟦…⟧`` so the model can tell whether it is
        continuing a half-finished sentence or starting a new one — the single most
        important signal for deciding between ``append_to_paragraph`` and
        ``insert_paragraphs_after``.
        """
        lines: list[str] = []
        for p in self.paragraphs:
            body = p.text
            if p.caret is not None:
                idx = max(0, min(len(body), p.caret))
                body = f"{body[:idx]}⟦КУРСОР⟧{body[idx:]}"
            if not body:
                body = "(пусто)"
            style = "" if p.style == "normal" else f" [{p.style}]"
            lines.append(f"{p.id}{style}: {body}")

        text = "\n".join(lines)
        if len(text) > max_chars:
            # Trim from the top: the paragraphs nearest the caret matter most.
            text = "…\n" + text[-max_chars:]
        return text


class ApplyConflict(BaseModel):
    index: int
    id: str = ""
    reason: str = ""


class ApplyResult(BaseModel):
    ok: bool = False
    applied: int = 0
    conflicts: list[ApplyConflict] = Field(default_factory=list)
    error: str = ""


class Capabilities(BaseModel):
    """What this Word build can actually do, reported by the pane at connect time."""

    word_api: dict[str, bool] = Field(default_factory=dict)
    platform: str = ""
    version: str = ""

    def supports(self, requirement_set: str) -> bool:
        return bool(self.word_api.get(requirement_set))


# --- Service -> task pane constructors -------------------------------------------------

def msg_state(**fields: Any) -> dict[str, Any]:
    return {"type": "state", **fields}


def msg_request_context(req_id: str) -> dict[str, Any]:
    return {"type": "requestContext", "reqId": req_id}


def msg_apply(req_id: str, ops: list[dict[str, Any]], meta: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"type": "apply", "reqId": req_id, "ops": ops, "meta": meta or {}}


def msg_transcript(text: str, final: bool = True, kind: str = "dictation") -> dict[str, Any]:
    return {"type": "transcript", "text": text, "final": final, "kind": kind}


def msg_timing(**stages: float) -> dict[str, Any]:
    return {"type": "timing", **stages}


def msg_log(message: str, level: Literal["info", "warn", "error"] = "info") -> dict[str, Any]:
    return {"type": "log", "level": level, "message": message}
