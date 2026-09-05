"""Finalisation: typography, concurrency hashes, and the empty-anchor tidy-up.

This is the last stage before ops reach the document, so it is where the guarantees the
task pane depends on are actually established.
"""
from __future__ import annotations

import pytest

from service.config import TypographyConfig
from service.llm.schema import parse_ops
from service.pipeline.finalize import finalize_ops
from service.protocol import text_hash
from service.textspec import build_context

TYPO = TypographyConfig()


def run(context_spec: str, ops: list[dict]) -> list[dict]:
    context = build_context(context_spec)
    batch = parse_ops({"mode": "dictation", "ops": ops}, context.ids)
    return finalize_ops(batch, context, TYPO).ops


class TestTypography:
    def test_new_paragraphs_get_dialogue_dashes(self) -> None:
        result = run("Мэри молчала.|", [
            {"op": "insert_paragraphs_after", "id": "P0",
             "paragraphs": [{"text": "- Почему? - спросила она."}]},
        ])
        assert result[0]["paragraphs"][0]["text"] == "— Почему? — спросила она."

    def test_appended_text_is_not_treated_as_a_paragraph_start(self) -> None:
        result = run("Он вышел на улицу и|", [
            {"op": "append_to_paragraph", "id": "P0", "text": "замер"},
        ])
        # A leading space is added, and no dialogue dash is introduced.
        assert result[0]["text"] == " замер"

    def test_join_adds_no_space_before_punctuation(self) -> None:
        result = run("Он вышел|", [
            {"op": "append_to_paragraph", "id": "P0", "text": ", наконец"},
        ])
        assert result[0]["text"] == ", наконец"

    def test_replacement_text_is_normalised(self) -> None:
        result = run("Старый текст.|", [
            {"op": "replace_in_paragraph", "id": "P0", "find": "Старый", "replace": '"новый"'},
        ])
        assert result[0]["text"] == "«новый» текст."


class TestConcurrencyHashes:
    def test_expect_hash_matches_the_context_paragraph(self) -> None:
        result = run("Первый. / Второй.|", [
            {"op": "replace_paragraph", "id": "P-1", "text": "Заменён."},
        ])
        assert result[0]["expect"] == text_hash("Первый.")

    def test_every_addressed_op_carries_a_hash(self) -> None:
        result = run("А. / Б.|", [
            {"op": "append_to_paragraph", "id": "P0", "text": " ещё"},
            {"op": "delete_paragraph", "id": "P-1"},
        ])
        assert all(op["expect"] for op in result)

    def test_revert_needs_no_hash(self) -> None:
        result = run("текст|", [{"op": "revert", "count": 1}])
        assert result == [{"op": "revert", "count": 1}]


class TestEmptyAnchor:
    def test_first_paragraph_fills_the_blank_anchor(self) -> None:
        result = run("Мэри молчала. / |", [
            {"op": "insert_paragraphs_after", "id": "P0",
             "paragraphs": [{"text": "— Первая."}, {"text": "— Вторая."}]},
        ])
        assert [op["op"] for op in result] == ["replace_paragraph", "insert_paragraphs_after"]
        assert result[0]["text"] == "— Первая."
        assert [p["text"] for p in result[1]["paragraphs"]] == ["— Вторая."]

    def test_single_paragraph_leaves_no_insert_op(self) -> None:
        result = run("|", [
            {"op": "insert_paragraphs_after", "id": "P0", "paragraphs": [{"text": "Одна строка."}]},
        ])
        assert [op["op"] for op in result] == ["replace_paragraph"]
        assert result[0]["text"] == "Одна строка."

    def test_style_is_carried_over(self) -> None:
        result = run("|", [
            {"op": "insert_paragraphs_after", "id": "P0",
             "paragraphs": [{"text": "Глава третья", "style": "heading1"}]},
        ])
        assert result[0]["style"] == "heading1"

    def test_non_empty_anchor_is_left_alone(self) -> None:
        result = run("Уже есть текст.|", [
            {"op": "insert_paragraphs_after", "id": "P0", "paragraphs": [{"text": "Новая строка."}]},
        ])
        assert [op["op"] for op in result] == ["insert_paragraphs_after"]

    def test_hash_survives_the_rewrite(self) -> None:
        result = run("|", [
            {"op": "insert_paragraphs_after", "id": "P0", "paragraphs": [{"text": "Текст."}]},
        ])
        assert result[0]["expect"] == text_hash("")


class TestNoops:
    def test_noops_are_dropped(self) -> None:
        assert run("текст|", [{"op": "noop", "reason": "ничего"}]) == []


class TestFragmentResolution:
    """`replace_in_paragraph` is resolved here, not by Word's search().

    The `expect` hash guarantees the paragraph is byte-for-byte what the model was
    shown, so the substring edit can be computed directly. That removes Word's search
    quirks — a 255-character cap, `^` as an escape character, literal matching — which
    used to abort the whole batch with "фрагмент не найден" and lose a dictated
    sentence.
    """

    def resolve(self, spec: str, ops: list[dict]):
        context = build_context(spec)
        batch = parse_ops({"mode": "dictation", "ops": ops}, context.ids)
        return finalize_ops(batch, context, TYPO)

    def test_exact_fragment_becomes_a_paragraph_replacement(self) -> None:
        result = self.resolve("Мэри была очень злая и кричала.|", [
            {"op": "replace_in_paragraph", "id": "P0", "find": "очень злая", "replace": "молчалива"},
        ])
        assert [op["op"] for op in result.ops] == ["replace_paragraph"]
        assert result.ops[0]["text"] == "Мэри была молчалива и кричала."
        assert not result.problems

    def test_word_search_no_longer_reaches_the_document(self) -> None:
        # Nothing downstream should ever see this op again.
        result = self.resolve("Абзац с текстом.|", [
            {"op": "replace_in_paragraph", "id": "P0", "find": "текстом", "replace": "содержанием"},
        ])
        assert all(op["op"] != "replace_in_paragraph" for op in result.ops)

    def test_a_fragment_longer_than_words_search_limit_works(self) -> None:
        body = "Слово " * 80                       # comfortably over 255 characters
        result = self.resolve(f"{body.strip()}|", [
            {"op": "replace_in_paragraph", "id": "P0", "find": body.strip(), "replace": "Коротко."},
        ])
        assert result.ops[0]["text"] == "Коротко."
        assert not result.problems

    @pytest.mark.parametrize(
        ("document", "find"),
        [
            ("— Почему ты это сделал?", "- Почему ты это сделал?"),      # hyphen for em dash
            ("Он читал «Войну и мир».", 'Он читал "Войну и мир".'),      # straight quotes
            ("Он ушёл в лес.", "Он ушел в лес."),                        # ё folded to е
            ("Слово   с   пробелами", "Слово с пробелами"),              # collapsed spaces
            ("Мэри молчала.", "мэри молчала."),                          # case
        ],
    )
    def test_typography_mismatches_are_repaired(self, document: str, find: str) -> None:
        # The model routinely quotes the document back with substituted characters.
        # Failing on that would throw away the author's words.
        result = self.resolve(f"{document}|", [
            {"op": "replace_in_paragraph", "id": "P0", "find": find, "replace": "ЗАМЕНА"},
        ])
        assert not result.problems, result.problems
        assert result.ops[0]["text"] == "ЗАМЕНА"

    def test_a_genuinely_absent_fragment_is_reported_not_guessed(self) -> None:
        result = self.resolve("Мэри молчала.|", [
            {"op": "replace_in_paragraph", "id": "P0", "find": "совершенно другое", "replace": "x"},
        ])
        assert result.ops == []
        assert result.problems and "не найден" in result.problems[0]

    def test_the_concurrency_hash_survives_resolution(self) -> None:
        result = self.resolve("Мэри молчала.|", [
            {"op": "replace_in_paragraph", "id": "P0", "find": "молчала", "replace": "ушла"},
        ])
        assert result.ops[0]["expect"] == text_hash("Мэри молчала.")

    def test_two_edits_to_one_paragraph_compose(self) -> None:
        result = self.resolve("Он шёл домой и молчал.|", [
            {"op": "replace_in_paragraph", "id": "P0", "find": "шёл", "replace": "брёл"},
            {"op": "replace_in_paragraph", "id": "P0", "find": "молчал", "replace": "пел"},
        ])
        assert not result.problems
        assert result.ops[-1]["text"] == "Он брёл домой и пел."

    def test_only_the_first_occurrence_is_replaced(self) -> None:
        result = self.resolve("Он шёл и шёл без остановки.|", [
            {"op": "replace_in_paragraph", "id": "P0", "find": "шёл", "replace": "брёл"},
        ])
        assert result.ops[0]["text"] == "Он брёл и шёл без остановки."

    def test_an_exact_match_wins_over_a_relaxed_one(self) -> None:
        # "Да" differs from "да" only in case, so the exact lowercase occurrence later in
        # the paragraph is the right target — relaxing to case-insensitive would grab the
        # wrong word.
        result = self.resolve("Да, да, конечно.|", [
            {"op": "replace_in_paragraph", "id": "P0", "find": "да", "replace": "нет"},
        ])
        assert result.ops[0]["text"] == "Да, нет, конечно."
