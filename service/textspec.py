"""A compact textual notation for a context window, used by the CLI tools and tests.

Paragraphs are separated by ``/`` and the caret is marked with ``|``::

    "Мэри стояла у окна. / Он вышел на улицу и| / Дальше шёл дождь."
     ^ P-1                 ^ P0, caret at the end   ^ P+1

This exists so a case can be written on one line, which is what makes the fixture file
readable and the benchmark quick to extend.
"""
from __future__ import annotations

from service.protocol import ContextParagraph, DocumentContext, text_hash


def build_context(spec: str) -> DocumentContext:
    """Parse a window spec. The anchor is the paragraph holding ``|``, else the last."""
    chunks = [chunk.strip() for chunk in spec.split("/")] if spec else [""]
    anchor_index = next((i for i, chunk in enumerate(chunks) if "|" in chunk), len(chunks) - 1)

    paragraphs: list[ContextParagraph] = []
    for index, chunk in enumerate(chunks):
        offset = index - anchor_index
        pid = "P0" if offset == 0 else f"P{offset:+d}"
        caret = None
        if "|" in chunk:
            caret = chunk.index("|")
            chunk = chunk.replace("|", "")
        paragraphs.append(
            ContextParagraph(
                id=pid,
                text=chunk,
                hash=text_hash(chunk),
                empty=not chunk,
                **({"caret": caret} if caret is not None else {}),
            )
        )

    anchor = paragraphs[anchor_index]
    return DocumentContext(
        paragraphs=paragraphs,
        at_end_of_paragraph=anchor.caret is None or anchor.caret >= len(anchor.text),
    )
