"""Tier-1 parsing must never steal a line of dialogue and never miss a real command."""
from __future__ import annotations

import pytest

from service.pipeline.commands import normalize, parse, within_one_edit


class TestWithinOneEdit:
    @pytest.mark.parametrize(
        ("a", "b", "expected"),
        [
            ("отмени", "отмени", True),
            ("атмени", "отмени", True),      # substitution
            ("отмен", "отмени", True),       # deletion
            ("оттмени", "отмени", True),     # insertion
            ("отменить", "отмени", False),   # two edits
            ("", "стоп", False),
            ("ассистент", "ассистент", True),
        ],
    )
    def test_bounded_distance(self, a: str, b: str, expected: bool) -> None:
        assert within_one_edit(a, b) is expected

    def test_is_symmetric(self) -> None:
        assert within_one_edit("отмен", "отмени") == within_one_edit("отмени", "отмен")


class TestNormalize:
    def test_folds_case_punctuation_and_yo(self) -> None:
        assert normalize("  Отмени, пожалуйста!  ") == "отмени пожалуйста"
        assert normalize("Ёлка") == "елка"


class TestBareControls:
    @pytest.mark.parametrize(
        ("utterance", "control"),
        [
            ("Новый абзац", "new_paragraph"),
            ("новый абзац.", "new_paragraph"),
            ("С новой строки", "new_paragraph"),
            ("Новая глава", "new_chapter"),
            ("Отмени последнее", "undo"),
            ("Стоп запись", "stop"),
        ],
    )
    def test_unambiguous_phrases_fire_without_wake_word(self, utterance: str, control: str) -> None:
        result = parse(utterance)
        assert result.kind == "control"
        assert result.control == control

    def test_phrase_embedded_in_prose_is_dictation(self) -> None:
        result = parse("Он начал новый абзац в своём письме.")
        assert result.kind == "dictation"
        assert result.control is None


class TestDialogueSafety:
    """The words a novelist would actually dictate as dialogue must not be commands."""

    @pytest.mark.parametrize("line", ["Стоп!", "Назад.", "Не надо.", "Хватит!", "Остановись!", "Глава", "Абзац"])
    def test_ambiguous_words_stay_dictation_without_wake_word(self, line: str) -> None:
        assert parse(line).kind == "dictation"

    @pytest.mark.parametrize(
        ("line", "control"),
        [("Ассистент, стоп", "stop"), ("Ассистент, отмени", "undo"), ("Помощник, абзац", "new_paragraph")],
    )
    def test_same_words_do_fire_after_the_wake_word(self, line: str, control: str) -> None:
        result = parse(line)
        assert result.kind == "control"
        assert result.control == control
        assert result.wake is True


class TestWakeWord:
    def test_command_body_keeps_original_casing(self) -> None:
        result = parse("Ассистент, перепиши последнее предложение на «Он молчал».")
        assert result.kind == "command"
        assert result.wake is True
        assert result.text == "перепиши последнее предложение на «Он молчал»."

    def test_misheard_wake_word_still_matches(self) -> None:
        assert parse("Асистент, удали этот абзац").kind == "command"

    def test_wake_word_alone_is_not_actionable(self) -> None:
        result = parse("Ассистент")
        assert result.kind == "control"
        assert result.control is None

    def test_wake_word_mid_sentence_is_dictation(self) -> None:
        # A character can be called "ассистент" without it being an instruction.
        assert parse("Он позвал ассистента в кабинет.").kind == "dictation"


class TestForceCommand:
    @pytest.mark.parametrize("line", ["Перепиши последнее предложение", "Мне не нравится", "Переделай последнее"])
    def test_routed_to_llm_as_command(self, line: str) -> None:
        result = parse(line)
        assert result.kind == "command"
        assert result.text == line

    def test_longer_variant_falls_through_to_the_model(self) -> None:
        line = "Перепиши последнее предложение так, чтобы оно звучало мягче"
        assert parse(line).kind == "dictation"


class TestDictation:
    def test_plain_prose(self) -> None:
        line = "Диалог. Почему ты это сделал, гневно сказала Мэри."
        result = parse(line)
        assert result.kind == "dictation"
        assert result.text == line

    def test_empty_input(self) -> None:
        assert parse("").kind == "dictation"
        assert parse("   ").text == ""
