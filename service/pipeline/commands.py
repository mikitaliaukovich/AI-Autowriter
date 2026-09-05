"""Tier-1 command parsing: deterministic, instant, and impossible to misfire.

The hybrid model the project uses is:

* An explicit wake prefix ("ассистент, ...") **always** means a command. No model call
  is needed to decide that.
* A small set of control phrases is recognised **only when it is the entire utterance**.
  This is what keeps the word "стоп" inside dictated prose from halting the session.
* Everything else falls through to the LLM, which classifies it from context.

Control phrases are split into two tiers, because a novel is exactly the kind of text
where a bare command word is also plausible dialogue. "— Стоп!", "— Назад!" and
"— Не надо." are all real lines someone would dictate, so those spellings are
**wake-only**; only unambiguous multi-word phrases ("новый абзац", "стоп запись") fire
without the wake word.

Matching runs on a normalised form because Whisper renders these phrases
inconsistently — "отмени", "Отмени.", "атмени" — and a control word that works four
times out of five is worse than none at all.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

Control = Literal["stop", "undo", "new_paragraph", "new_chapter"]
Kind = Literal["control", "command", "dictation"]

WAKE_WORDS = ("ассистент", "помощник", "секретарь")

# Unambiguous as a standalone utterance — no wake word needed.
CONTROL_BARE: dict[Control, tuple[str, ...]] = {
    "stop": ("стоп запись", "останови запись", "конец записи", "закончить запись"),
    "undo": ("отмени последнее", "убери последнее", "отменить последнее", "отмени правку"),
    "new_paragraph": ("новый абзац", "с новой строки", "новая строка"),
    "new_chapter": ("новая глава", "новый раздел"),
}

# Plausible as dictated dialogue, so these require the wake word.
CONTROL_WAKE_ONLY: dict[Control, tuple[str, ...]] = {
    "stop": ("стоп", "пауза", "хватит", "останови", "остановись"),
    "undo": ("отмена", "отмени", "отменить", "назад", "убери это", "не надо"),
    "new_paragraph": ("абзац",),
    "new_chapter": ("глава",),
}

# Whole-utterance phrases that are certainly instructions but need the model to act on
# them — "rewrite it" says nothing about what to rewrite it *to*. Routed to the LLM as
# a command rather than handled here.
FORCE_COMMAND: tuple[str, ...] = (
    "перепиши последнее предложение",
    "перепиши последний абзац",
    "перепиши последнее",
    "переделай последнее",
    "мне не нравится",
    "это плохо",
)

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Lowercase, drop punctuation, fold ё to е, collapse whitespace."""
    text = unicodedata.normalize("NFKC", text).lower().replace("ё", "е")
    text = _PUNCT_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()


def within_one_edit(a: str, b: str) -> bool:
    """True when ``a`` and ``b`` differ by at most one insertion, deletion or substitution.

    A bounded check rather than a full Levenshtein matrix: we only ever ask about
    distance <= 1, and this answers that in O(n) with no allocation.
    """
    if a == b:
        return True
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la == lb:
        diffs = 0
        for ca, cb in zip(a, b):
            if ca != cb:
                diffs += 1
                if diffs > 1:
                    return False
        return True
    # Lengths differ by exactly one: the longer must contain the shorter as a
    # subsequence with a single skip.
    short, long = (a, b) if la < lb else (b, a)
    i = j = 0
    skipped = False
    while i < len(short) and j < len(long):
        if short[i] == long[j]:
            i += 1
            j += 1
        elif skipped:
            return False
        else:
            skipped = True
            j += 1
    return True


@dataclass(frozen=True)
class Parsed:
    """Result of tier-1 parsing.

    ``kind == "control"``   -> act immediately, no model call.
    ``kind == "command"``   -> definitely an instruction; send ``text`` to the LLM.
    ``kind == "dictation"`` -> send ``text`` to the LLM, which decides what it is.
    """

    kind: Kind
    text: str
    control: Control | None = None
    wake: bool = False


def _strip_wake(norm: str, raw: str) -> tuple[bool, str, str]:
    """Detect and remove a leading wake word.

    Returns ``(found, normalised_remainder, raw_remainder)``. The raw remainder keeps
    its original casing and punctuation, which the model needs.
    """
    first, _, rest_norm = norm.partition(" ")
    if not any(within_one_edit(first, w) for w in WAKE_WORDS):
        return False, norm, raw

    raw_rest = raw.strip()
    match = re.match(r"^\s*\S+[\s,.:—–-]*", raw_rest)
    if match:
        raw_rest = raw_rest[match.end():]
    return True, rest_norm.strip(), raw_rest.strip()


def _match(norm: str, tables: tuple[dict[Control, tuple[str, ...]], ...]) -> Control | None:
    if not norm:
        return None
    for table in tables:
        for control, phrases in table.items():
            for phrase in phrases:
                if within_one_edit(norm, phrase):
                    return control
    return None


def parse(raw: str) -> Parsed:
    """Classify one transcribed utterance."""
    raw = (raw or "").strip()
    norm = normalize(raw)
    if not norm:
        return Parsed(kind="dictation", text="")

    wake, norm_rest, raw_rest = _strip_wake(norm, raw)
    subject = norm_rest if wake else norm

    tables = (CONTROL_BARE, CONTROL_WAKE_ONLY) if wake else (CONTROL_BARE,)
    control = _match(subject, tables)
    if control is not None:
        return Parsed(kind="control", text="", control=control, wake=wake)

    if any(within_one_edit(subject, phrase) for phrase in FORCE_COMMAND):
        return Parsed(kind="command", text=raw_rest if wake else raw, wake=wake)

    if wake:
        # "ассистент" alone, with nothing after it, is not actionable.
        if not norm_rest:
            return Parsed(kind="control", text="", control=None, wake=True)
        return Parsed(kind="command", text=raw_rest, wake=True)

    return Parsed(kind="dictation", text=raw, wake=False)
