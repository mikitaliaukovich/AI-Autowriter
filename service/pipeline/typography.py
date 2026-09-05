"""Russian typographic normalisation.

Deliberately deterministic and *not* delegated to the LLM. Models are unreliable at
this — they will produce an em dash in one paragraph and a hyphen in the next — but a
manuscript has to be consistent to the character. So the model is asked for content and
this module is the sole authority on dashes, quotes, ellipses and spacing.

Everything here is pure text-in / text-out, which is what makes it cheap to test.
"""
from __future__ import annotations

import re

from service.config import TypographyConfig

NBSP = " "
EM_DASH = "—"
EN_DASH = "–"
ELLIPSIS = "…"

LAQUO, RAQUO = "«", "»"      # « »  — outer quotes in Russian
BDQUO, LDQUO = "„", "“"      # „ “  — nested quotes in Russian

# Every character a model might plausibly emit where a dash belongs.
_DASHES = "-‐‑‒–—―"

# One-letter prepositions and conjunctions that must not be left at a line end.
_SHORT_WORDS = "аиовксуяё"

_OPENERS = "([{«„" + _DASHES + " \t\n"


def _paragraph_dash(text: str, dash: str) -> str:
    """Normalise the dash that opens a dialogue line, plus inline author dashes."""
    # Leading dash of any flavour, optionally followed by whitespace.
    text = re.sub(rf"^[{_DASHES}]+[ \t{NBSP}]*", dash + NBSP, text)
    # Inline " - " (author's attribution inside a dialogue line, or a parenthetical).
    text = re.sub(rf"(?<=[^\s])\s+[{_DASHES}]\s+", f" {dash}{NBSP}", text)
    return text


def _inline_dash(text: str, dash: str) -> str:
    """Inline dashes only — used for text appended mid-paragraph."""
    return re.sub(rf"(?<=[^\s])\s+[{_DASHES}]\s+", f" {dash}{NBSP}", text)


def _number_ranges(text: str) -> str:
    """5-10 becomes 5–10 (en dash), the Russian convention for numeric ranges."""
    return re.sub(r"(?<=\d)\s?-\s?(?=\d)", EN_DASH, text)


def _quotes(text: str) -> str:
    """Convert straight and English curly quotes to Russian « » with nested „ “.

    Nesting depth counts pre-existing « » in the text too, so a model that emits
    « ... "..." ... » ends up with correctly nested guillemets and low-9 quotes.
    """
    out: list[str] = []
    depth = 0
    for i, ch in enumerate(text):
        if ch == LAQUO:
            depth += 1
            out.append(ch)
            continue
        if ch == RAQUO:
            depth = max(0, depth - 1)
            out.append(ch)
            continue
        if ch not in '"“”„‟':
            out.append(ch)
            continue

        if ch == "”":
            opening = False
        elif ch in "“„‟":
            opening = True
        else:
            prev = text[i - 1] if i else " "
            opening = prev in _OPENERS

        if opening:
            out.append(LAQUO if depth == 0 else BDQUO)
            depth += 1
        else:
            depth = max(0, depth - 1)
            out.append(RAQUO if depth == 0 else LDQUO)
    return "".join(out)


def _ellipsis(text: str) -> str:
    text = re.sub(r"\.{3,}", ELLIPSIS, text)
    return re.sub(rf"{ELLIPSIS}\.+", ELLIPSIS, text)


def _spacing(text: str) -> str:
    # Collapse runs of plain spaces/tabs (NBSP is intentionally preserved).
    text = re.sub(r"[ \t]{2,}", " ", text)
    # No space before closing punctuation.
    text = re.sub(rf"[ \t]+([,.!?;:{RAQUO}{LDQUO})\]}}])", r"\1", text)
    # No space after an opening bracket or quote.
    text = re.sub(rf"([({{\[{LAQUO}{BDQUO}])[ \t]+", r"\1", text)
    # Exactly one space after sentence punctuation followed by a word.
    text = re.sub(r"([,;:])(?=[^\s\d])", r"\1 ", text)
    text = re.sub(rf"([.!?{ELLIPSIS}])(?=[^\s\d.!?{ELLIPSIS}{RAQUO}{LDQUO})\]}}])", r"\1 ", text)
    return text


def _nbsp_short_words(text: str) -> str:
    """Bind one-letter prepositions to the following word."""
    pattern = rf"(?<![^\s{NBSP}({LAQUO}{BDQUO}{EM_DASH}])([{_SHORT_WORDS}{_SHORT_WORDS.upper()}])[ \t]+"
    return re.sub(pattern, r"\1" + NBSP, text)


def normalize(text: str, cfg: TypographyConfig, *, paragraph_start: bool = True) -> str:
    """Apply the configured rules to one run of text.

    ``paragraph_start=False`` is used for text appended mid-paragraph, where a leading
    dash is an inline dash rather than the opening of a dialogue line.
    """
    if not text:
        return text

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("  ", NBSP)

    if cfg.ellipsis:
        text = _ellipsis(text)
    if cfg.quotes:
        text = _quotes(text)
    if cfg.dialogue_dash:
        text = _number_ranges(text)
        text = _paragraph_dash(text, EM_DASH) if paragraph_start else _inline_dash(text, EM_DASH)
    if cfg.spacing:
        text = _spacing(text)
    if cfg.nbsp_short_words:
        text = _nbsp_short_words(text)

    # Trailing whitespace never belongs in a paragraph; leading only at a paragraph start.
    text = text.rstrip(" \t")
    if paragraph_start:
        text = text.lstrip(" \t")
    return text


def join_spacing(existing: str, addition: str) -> str:
    """Return the separator needed between an existing paragraph and appended text.

    Appending is the hot path during dictation, and getting this wrong shows up as
    "wordsjammedtogether" or a space before a comma.
    """
    if not existing or not addition:
        return ""
    if existing[-1].isspace() or addition[0].isspace():
        return ""
    if addition[0] in f",.!?;:{RAQUO}{LDQUO})]}}{ELLIPSIS}":
        return ""
    if existing[-1] in f"({LAQUO}{BDQUO}[{{":
        return ""
    return " "
