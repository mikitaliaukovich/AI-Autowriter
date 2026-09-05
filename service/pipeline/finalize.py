"""Turning validated ops into the exact payload Word receives.

Two things happen here, and both must happen in exactly one place so that the CLI
tools, the benchmark and the live pipeline all produce identical output:

* **Typography.** Applied per op, with the right notion of "start of paragraph":
  text appended mid-paragraph must not acquire a dialogue dash.
* **Concurrency hashes.** Each op carries the hash of the paragraph as it was when the
  context window was read, so the task pane can reject a batch computed against a
  document the user has since changed.
"""
from __future__ import annotations

from typing import Any

from service.config import TypographyConfig
from service.llm.schema import OpsBatch
from service.pipeline.typography import join_spacing, normalize
from service.protocol import DocumentContext, text_hash


def _fill_empty_anchor(wire: list[dict[str, Any]], context: DocumentContext) -> list[dict[str, Any]]:
    """Write the first paragraph *into* an empty anchor rather than after it.

    When the caret sits on a blank paragraph — which is the normal state at the end of a
    manuscript — the model may pick either ``replace_paragraph`` or
    ``insert_paragraphs_after``. Both are reasonable, but the second leaves the blank
    paragraph stranded above the new text. Rather than lean on the prompt to be
    consistent about it, the tidy-up is done here where it can be guaranteed.
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


def finalize_ops(batch: OpsBatch, context: DocumentContext, typo: TypographyConfig) -> list[dict[str, Any]]:
    """Apply typography and attach `expect` hashes. Drops no-ops."""
    wire: list[dict[str, Any]] = []

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

    return _fill_empty_anchor(wire, context)
