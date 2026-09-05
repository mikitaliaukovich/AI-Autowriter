"""Turning validated ops into the exact payload Word receives.

Three things happen here, and all three must happen in exactly one place so that the CLI
tools, the benchmark and the live pipeline produce identical output:

* **Typography.** Applied per op, with the right notion of "start of paragraph": text
  appended mid-paragraph must not acquire a dialogue dash.
* **Concurrency hashes.** Each op carries the hash of the paragraph as it was when the
  context window was read, so the task pane can reject a batch computed against a
  document the user has since changed.
* **Resolving ``replace_in_paragraph`` here rather than in Word.** Because the hash
  guarantees the paragraph is byte-for-byte what the model was shown, the substring
  replacement can be computed in Python and sent as a plain ``replace_paragraph``.

That last point fixes a real failure. Word's ``Range.search()`` is picky — it caps
patterns at 255 characters, treats ``^`` as an escape introducer, and matches literally
— so a fragment the model copied with a straight quote instead of a guillemet simply was
not found. The whole batch then aborted with "фрагмент не найден" and the user's
dictated sentence was lost. Resolving it here means the mismatch is detected before the
document is touched, can be repaired, and when it genuinely cannot be resolved the
caller is told so it can ask the model again instead of silently dropping speech.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from service.config import TypographyConfig
from service.llm.schema import OpsBatch
from service.pipeline.typography import join_spacing, normalize
from service.protocol import DocumentContext, text_hash

# Characters the model routinely substitutes for one another when quoting the document
# back at us. Each group is matched interchangeably when locating a fragment.
_DASHES = "-‐‑‒–—―"
_QUOTES = "\"'«»„“”‟‘’"
_SPACES = " \t   "


@dataclass
class Finalized:
    """Ops ready for the wire, plus anything that could not be resolved.

    ``problems`` is not cosmetic: the orchestrator uses it to re-ask the model rather
    than let a dictated sentence vanish.
    """

    ops: list[dict[str, Any]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def _flexible_pattern(fragment: str) -> re.Pattern[str]:
    """A pattern matching ``fragment`` up to dash, quote and whitespace substitutions."""
    parts: list[str] = []
    previous_space = False
    for char in fragment:
        if char in _SPACES or char.isspace():
            if not previous_space:
                parts.append(f"[{re.escape(_SPACES)}\\s]+")
            previous_space = True
            continue
        previous_space = False
        if char in _DASHES:
            parts.append(f"[{re.escape(_DASHES)}]")
        elif char in _QUOTES:
            parts.append(f"[{re.escape(_QUOTES)}]")
        elif char in "еёЕЁ":
            parts.append("[еёЕЁ]" if char.islower() else "[ЕЁеё]")
        else:
            parts.append(re.escape(char))
    return re.compile("".join(parts), re.IGNORECASE)


def locate(text: str, fragment: str) -> tuple[int, int] | None:
    """Find ``fragment`` in ``text``, tolerating the substitutions models make.

    Returns the span in ``text``, or None. Exact matches win; only then do we relax,
    and never so far that a different sentence could match.
    """
    if not fragment:
        return None
    index = text.find(fragment)
    if index >= 0:
        return index, index + len(fragment)

    match = _flexible_pattern(fragment).search(text)
    if match:
        return match.start(), match.end()

    # Last resort: the model often trims or adds trailing punctuation.
    trimmed = fragment.strip().strip(".,;:!?…" + _DASHES + _QUOTES)
    if trimmed and trimmed != fragment:
        match = _flexible_pattern(trimmed).search(text)
        if match:
            return match.start(), match.end()
    return None


def _resolve_replacements(
    wire: list[dict[str, Any]], context: DocumentContext, problems: list[str]
) -> list[dict[str, Any]]:
    """Rewrite every ``replace_in_paragraph`` into a ``replace_paragraph``."""
    resolved: list[dict[str, Any]] = []
    # Paragraphs edited earlier in the batch must be matched against their new text.
    working: dict[str, str] = {}

    for op in wire:
        if op["op"] != "replace_in_paragraph":
            if op.get("op") == "replace_paragraph" and op.get("id"):
                working[op["id"]] = op["text"]
            resolved.append(op)
            continue

        pid = op["id"]
        paragraph = context.get(pid)
        if paragraph is None:
            problems.append(f"абзац {pid} вне окна контекста")
            continue

        current = working.get(pid, paragraph.text)
        span = locate(current, op["find"])
        if span is None:
            problems.append(
                f"фрагмент «{op['find'][:60]}» не найден в абзаце {pid}"
            )
            continue

        start, end = span
        new_text = current[:start] + op["replace"] + current[end:]
        working[pid] = new_text
        replacement: dict[str, Any] = {"op": "replace_paragraph", "id": pid, "text": new_text}
        if "expect" in op:
            replacement["expect"] = op["expect"]
        resolved.append(replacement)

    return resolved


def _fill_empty_anchor(wire: list[dict[str, Any]], context: DocumentContext) -> list[dict[str, Any]]:
    """Write the first paragraph *into* an empty anchor rather than after it.

    When the caret sits on a blank paragraph — the normal state at the end of a
    manuscript — the model may pick either ``replace_paragraph`` or
    ``insert_paragraphs_after``. Both are reasonable, but the second leaves the blank
    paragraph stranded above the new text. Rather than lean on the prompt to be
    consistent about it, the tidy-up is guaranteed here.
    """
    anchor = context.get("P0")
    if anchor is None or anchor.text:
        return wire

    for index, op in enumerate(wire):
        if op["op"] != "insert_paragraphs_after" or op["id"] != "P0":
            continue
        first, *rest = op["paragraphs"]
        replacement: list[dict[str, Any]] = [
            {"op": "replace_paragraph", "id": "P0", "text": first["text"],
             "style": first["style"], **({"expect": op["expect"]} if "expect" in op else {})}
        ]
        if rest:
            replacement.append({**op, "paragraphs": rest})
        return wire[:index] + replacement + wire[index + 1:]
    return wire


def finalize_ops(batch: OpsBatch, context: DocumentContext, typo: TypographyConfig) -> Finalized:
    """Apply typography, resolve fragment edits, and attach `expect` hashes."""
    wire: list[dict[str, Any]] = []
    problems: list[str] = []

    for op in batch.ops:
        data = op.model_dump(exclude_none=True)
        name = data["op"]

        if name == "noop":
            continue

        if name == "append_to_paragraph":
            text = normalize(data["text"], typo, paragraph_start=False)
            paragraph = context.get(data["id"])
            existing = paragraph.text if paragraph else ""
            data["text"] = join_spacing(existing, text) + text

        elif name == "insert_paragraphs_after":
            data["paragraphs"] = [
                {"text": normalize(p["text"], typo), "style": p.get("style", "normal")}
                for p in data["paragraphs"]
            ]

        elif name == "replace_paragraph":
            data["text"] = normalize(data["text"], typo)

        elif name == "replace_in_paragraph":
            data["replace"] = normalize(data["replace"], typo, paragraph_start=False)

        pid = data.get("id")
        if pid and (paragraph := context.get(pid)):
            data["expect"] = paragraph.hash or text_hash(paragraph.text)

        wire.append(data)

    wire = _resolve_replacements(wire, context, problems)
    return Finalized(ops=_fill_empty_anchor(wire, context), problems=problems)
