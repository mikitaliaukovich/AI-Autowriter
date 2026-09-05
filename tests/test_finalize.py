"""Finalisation: typography, concurrency hashes, and the empty-anchor tidy-up.

This is the last stage before ops reach the document, so it is where the guarantees the
task pane depends on are actually established.
"""
from __future__ import annotations

from service.config import TypographyConfig
from service.llm.schema import parse_ops
from service.pipeline.finalize import finalize_ops
from service.protocol import text_hash
from service.textspec import build_context

TYPO = TypographyConfig()


def run(context_spec: str, ops: list[dict]) -> list[dict]:
    context = build_context(context_spec)
    batch = parse_ops({"mode": "dictation", "ops": ops}, context.ids)
    return finalize_ops(batch, context, TYPO)


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
        assert result[0]["replace"] == "«новый»"


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
