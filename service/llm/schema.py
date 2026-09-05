"""Edit-operation schema.

The model never returns prose to paste blindly. It returns *operations* addressed to
paragraph ids from the context window it was shown, which is what makes "rewrite the
last sentence" an exact edit rather than a fuzzy search across the manuscript.

Two representations live here:

* ``LLM_RESPONSE_SCHEMA`` -- a deliberately **flat** JSON schema handed to Ollama for
  constrained decoding. Flat (all op fields optional on one object) rather than a
  discriminated union, because llama.cpp's JSON-schema-to-grammar conversion handles
  flat objects far more reliably than ``anyOf``.
* The pydantic models -- the strict internal form. :func:`parse_ops` narrows the loose
  model output into these, dropping anything malformed instead of trusting the model.
"""
from __future__ import annotations

import re
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field, ValidationError

# Paragraph styles we are willing to set. Mapped to Word.Style constants in the task
# pane. Keys are language-independent on purpose: `paragraph.style` takes *localized*
# names and this add-in targets a Russian Word UI, so the task pane uses `styleBuiltIn`.
STYLES = ("normal", "heading1", "heading2", "heading3", "quote", "intenseQuote", "listParagraph")

OP_NAMES = (
    "append_to_paragraph",
    "insert_paragraphs_after",
    "replace_paragraph",
    "replace_in_paragraph",
    "delete_paragraph",
    "set_style",
    "revert",
    "noop",
)

MODES = ("dictation", "command", "mixed", "ignore")

# Word's Range.search() rejects patterns longer than this.
MAX_SEARCH_LEN = 255


class NewParagraph(BaseModel):
    text: str = ""
    style: str = "normal"


class _Base(BaseModel):
    """Common fields.

    ``expect`` is the paragraph-text hash captured when the context window was read;
    the task pane rejects the batch if that paragraph has changed since.
    """

    expect: str | None = None


class AppendToParagraph(_Base):
    op: Literal["append_to_paragraph"]
    id: str
    text: str


class InsertParagraphsAfter(_Base):
    op: Literal["insert_paragraphs_after"]
    id: str
    paragraphs: list[NewParagraph]


class ReplaceParagraph(_Base):
    op: Literal["replace_paragraph"]
    id: str
    text: str
    style: str | None = None


class ReplaceInParagraph(_Base):
    op: Literal["replace_in_paragraph"]
    id: str
    find: str
    replace: str


class DeleteParagraph(_Base):
    op: Literal["delete_paragraph"]
    id: str


class SetStyle(_Base):
    op: Literal["set_style"]
    id: str
    style: str


class Revert(_Base):
    op: Literal["revert"]
    count: int = 1


class Noop(_Base):
    op: Literal["noop"]
    reason: str = ""


Op = Annotated[
    Union[
        AppendToParagraph,
        InsertParagraphsAfter,
        ReplaceParagraph,
        ReplaceInParagraph,
        DeleteParagraph,
        SetStyle,
        Revert,
        Noop,
    ],
    Field(discriminator="op"),
]


class OpsBatch(BaseModel):
    mode: str = "dictation"
    ops: list[Op] = Field(default_factory=list)
    note: str = ""


# The loose schema shown to the model. Every op-specific field is optional; `op` alone
# is required. Descriptions double as inline documentation for the model.
LLM_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "mode": {
            "type": "string",
            "enum": list(MODES),
            "description": (
                "dictation - авторский текст; command - указание ассистенту; "
                "mixed - и то и другое; ignore - ничего не делать."
            ),
        },
        "ops": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "op": {"type": "string", "enum": list(OP_NAMES)},
                    "id": {
                        "type": "string",
                        "description": "Идентификатор абзаца из окна контекста, например P0 или P-1.",
                    },
                    "text": {"type": "string"},
                    "find": {
                        "type": "string",
                        "description": "Точная подстрока в абзаце, не длиннее 255 символов.",
                    },
                    "replace": {"type": "string"},
                    "style": {"type": "string", "enum": list(STYLES)},
                    "paragraphs": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "text": {"type": "string"},
                                "style": {"type": "string", "enum": list(STYLES)},
                            },
                            "required": ["text"],
                        },
                    },
                    "count": {"type": "integer"},
                    "reason": {"type": "string"},
                },
                "required": ["op"],
            },
        },
        "note": {
            "type": "string",
            "description": "Краткое пояснение для журнала (в документ не попадает).",
        },
    },
    "required": ["mode", "ops"],
}

_ID_RE = re.compile(r"^P[+-]?\d+$")


def normalize_id(raw: Any) -> str | None:
    """Accept the id spellings models actually produce: P0, P-1, P+1, 'P 1', 'p0'."""
    if not isinstance(raw, str):
        return None
    s = raw.strip().replace(" ", "").upper()
    if s in ("P0", "P"):
        return "P0"
    if _ID_RE.match(s):
        return "P0" if s in ("P+0", "P-0") else s
    return None


def parse_ops(payload: dict[str, Any], known_ids: set[str]) -> OpsBatch:
    """Narrow a loose model response into validated ops.

    Anything that cannot be made sense of is dropped rather than guessed at: a wrong op
    is far worse than a missing one, because it lands in the user's manuscript.
    """
    mode = payload.get("mode")
    mode = mode if mode in MODES else "dictation"
    note = str(payload.get("note") or "")[:400]

    raw_ops = payload.get("ops")
    if not isinstance(raw_ops, list):
        raw_ops = []

    cleaned: list[dict[str, Any]] = []
    dropped_oversized = False

    for item in raw_ops:
        if not isinstance(item, dict):
            continue
        name = item.get("op")
        if name not in OP_NAMES:
            continue

        op: dict[str, Any] = {"op": name}
        expect = item.get("expect")
        if expect:
            op["expect"] = str(expect)

        if name in ("revert", "noop"):
            if name == "revert":
                try:
                    op["count"] = max(1, min(20, int(item.get("count", 1))))
                except (TypeError, ValueError):
                    op["count"] = 1
            else:
                op["reason"] = str(item.get("reason") or "")[:200]
            cleaned.append(op)
            continue

        pid = normalize_id(item.get("id"))
        if pid is None or (known_ids and pid not in known_ids):
            continue
        op["id"] = pid

        if name == "append_to_paragraph":
            text = item.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            op["text"] = text

        elif name == "insert_paragraphs_after":
            paras = item.get("paragraphs")
            if not isinstance(paras, list):
                continue
            built = [
                {
                    "text": p["text"],
                    "style": p.get("style") if p.get("style") in STYLES else "normal",
                }
                for p in paras
                if isinstance(p, dict) and isinstance(p.get("text"), str) and p["text"].strip()
            ]
            if not built:
                continue
            op["paragraphs"] = built

        elif name == "replace_paragraph":
            text = item.get("text")
            if not isinstance(text, str):
                continue
            op["text"] = text
            if item.get("style") in STYLES:
                op["style"] = item["style"]

        elif name == "replace_in_paragraph":
            find, repl = item.get("find"), item.get("replace")
            if not isinstance(find, str) or not find.strip() or not isinstance(repl, str):
                continue
            if len(find) > MAX_SEARCH_LEN:
                # Word's search() would reject this. Dropping is safer than truncating,
                # which would silently replace the wrong span.
                dropped_oversized = True
                continue
            op["find"], op["replace"] = find, repl

        elif name == "set_style":
            if item.get("style") not in STYLES:
                continue
            op["style"] = item["style"]

        cleaned.append(op)

    try:
        batch = OpsBatch(mode=mode, ops=cleaned, note=note)
    except ValidationError:
        batch = OpsBatch(mode=mode, ops=[], note=note)
    if dropped_oversized:
        batch.note = (batch.note + " [dropped oversized find/replace]").strip()
    return batch
