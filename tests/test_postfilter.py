"""Hallucination suppression.

These are the exact strings Whisper produces on Russian silence. If any of them slips
through, it lands in the manuscript.
"""
from __future__ import annotations

import pytest

from service.asr.postfilter import (
    is_boilerplate,
    is_degenerate_repetition,
    reason_to_drop,
)


class TestBoilerplate:
    @pytest.mark.parametrize(
        "text",
        [
            "Продолжение следует...",
            "Субтитры сделал DimaTorzok",
            "Субтитры создавал DimaTorzok",
            "Спасибо за просмотр!",
            "Подписывайтесь на канал!",
            "Всем пока!",
            "продолжение следует",
            "[Музыка]",
            "(аплодисменты)",
            "",
            "   ",
        ],
    )
    def test_known_hallucinations_are_caught(self, text: str) -> None:
        assert is_boilerplate(text) is True

    @pytest.mark.parametrize(
        "text",
        [
            "Почему ты это сделал, гневно сказала Мэри.",
            "Он вышел на улицу и сразу почувствовал, что что-то не так.",
            "Спасибо, — сказал он и вышел, аккуратно прикрыв за собой дверь.",
            "Музыка играла всю ночь, и никто не спал в этом доме до рассвета.",
        ],
    )
    def test_real_prose_survives(self, text: str) -> None:
        assert is_boilerplate(text) is False


class TestDegenerateRepetition:
    @pytest.mark.parametrize(
        "text",
        [
            "да да да да да да",
            "и и и и и",
            "спасибо большое спасибо большое спасибо большое спасибо большое",
        ],
    )
    def test_loops_are_caught(self, text: str) -> None:
        assert is_degenerate_repetition(text) is True

    @pytest.mark.parametrize(
        "text",
        [
            "Она шла и шла, и не могла остановиться.",
            "Да, да, конечно, я всё понял и сделаю как надо.",
            "Он повторил: очень, очень странно.",
        ],
    )
    def test_natural_repetition_survives(self, text: str) -> None:
        assert is_degenerate_repetition(text) is False


class TestReasonToDrop:
    def test_good_transcript_is_kept(self) -> None:
        assert reason_to_drop(
            "Мэри стояла у окна и молчала.",
            no_speech_prob=0.1,
            avg_logprob=-0.3,
            duration_s=3.0,
        ) == ""

    def test_high_no_speech_probability_drops(self) -> None:
        assert reason_to_drop("Что-то было сказано.", no_speech_prob=0.9, duration_s=3.0)

    def test_low_confidence_drops(self) -> None:
        assert reason_to_drop("Что-то было сказано.", avg_logprob=-2.0, duration_s=3.0)

    def test_implausible_length_for_short_clip_drops(self) -> None:
        assert reason_to_drop("а" * 120, duration_s=0.4)

    def test_reason_is_human_readable(self) -> None:
        reason = reason_to_drop("Продолжение следует...", duration_s=2.0)
        assert "hallucinat" in reason
