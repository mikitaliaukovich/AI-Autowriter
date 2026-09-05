"""Suppression of Whisper hallucinations.

Whisper was trained on a large amount of subtitled video, and on near-silent or noisy
audio it falls back on what it saw most often there. In Russian this is severe and very
recognisable: fragments of subtitle credits, channel sign-offs and "продолжение
следует" appear out of nowhere. In a dictation tool these land straight in the
manuscript, so they are filtered before anything else sees them.

Three independent signals are used, because none alone is sufficient:

* a phrase list of the known subtitle boilerplate (exact, cheap, catches the worst);
* Whisper's own ``no_speech_prob`` / ``avg_logprob`` confidence;
* degenerate repetition, which is what a decoding loop looks like.
"""
from __future__ import annotations

import re
import unicodedata

# Boilerplate Whisper emits on silence. Matched against the normalised transcript, and
# only when it accounts for essentially the whole utterance.
HALLUCINATION_PHRASES: tuple[str, ...] = (
    "продолжение следует",
    "субтитры сделал dimatorzok",
    "субтитры создавал dimatorzok",
    "субтитры делал dimatorzok",
    "редактор субтитров а синецкая",
    "корректор а егорова",
    "субтитры и перевод",
    "спасибо за просмотр",
    "спасибо за внимание",
    "подписывайтесь на канал",
    "подпишись на канал",
    "ставьте лайки",
    "всем пока",
    "до новых встреч",
    "продолжение в следующей серии",
    "музыка",
    "аплодисменты",
    "смех",
    "субтитры",
    "перевод subs by",
    "thanks for watching",
    "subscribe",
    "you",
    "спасибо",
)

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")

# Confidence gates. Whisper reports these per segment; values outside them mean the
# decoder was guessing.
NO_SPEECH_MAX = 0.75
AVG_LOGPROB_MIN = -1.2


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower().replace("ё", "е")
    text = _PUNCT_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()


def is_boilerplate(text: str) -> bool:
    """True when the transcript is (almost) nothing but subtitle boilerplate."""
    norm = normalize(text)
    if not norm:
        return True
    for phrase in HALLUCINATION_PHRASES:
        if norm == phrase:
            return True
        # A phrase that accounts for most of a short utterance is still boilerplate:
        # "Спасибо за просмотр!" preceded by a stray word, for instance.
        if phrase in norm and len(phrase) / len(norm) > 0.7:
            return True
    return False


def is_degenerate_repetition(text: str, *, min_repeats: int = 4) -> bool:
    """Detect a decoding loop: the same word or short phrase repeated on and on."""
    words = normalize(text).split()
    if len(words) < min_repeats:
        return False

    # A single token dominating the utterance.
    unique = set(words)
    if len(unique) == 1:
        return True
    if len(words) >= 8 and len(unique) <= max(2, len(words) // 6):
        return True

    # A short phrase repeated back to back.
    for size in (2, 3, 4):
        if len(words) < size * min_repeats:
            continue
        chunk = words[:size]
        repeats = 1
        for start in range(size, len(words) - size + 1, size):
            if words[start:start + size] == chunk:
                repeats += 1
            else:
                break
        if repeats >= min_repeats:
            return True
    return False


def reason_to_drop(
    text: str,
    *,
    no_speech_prob: float = 0.0,
    avg_logprob: float = 0.0,
    duration_s: float = 0.0,
) -> str:
    """Return a short reason to suppress this transcript, or "" to keep it."""
    if not text.strip():
        return "empty"
    if is_boilerplate(text):
        return "hallucinated subtitle boilerplate"
    if is_degenerate_repetition(text):
        return "degenerate repetition"
    if no_speech_prob > NO_SPEECH_MAX:
        return f"no_speech_prob={no_speech_prob:.2f}"
    if avg_logprob and avg_logprob < AVG_LOGPROB_MIN:
        return f"low confidence (avg_logprob={avg_logprob:.2f})"
    # A long transcript from a very short clip is a fabrication.
    if duration_s and duration_s < 1.0 and len(text) > 80:
        return "implausible length for clip duration"
    return ""
