"""Narrowing model output into validated operations.

`parse_ops` is the boundary between "what the model said" and "what gets written into a
manuscript". Its rule is that anything which cannot be understood is dropped rather than
guessed at, because a wrong operation is far worse than a missing one.
"""
from __future__ import annotations

import pytest

from service.llm.schema import (
    LLM_RESPONSE_SCHEMA,
    MAX_SEARCH_LEN,
    OP_NAMES,
    STYLES,
    normalize_id,
    parse_ops,
)

IDS = {"P-2", "P-1", "P0", "P+1"}


def ops_of(payload: dict) -> list[str]:
    return [op.op for op in parse_ops(payload, IDS).ops]


class TestNormalizeId:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("P0", "P0"), ("p0", "P0"), ("P", "P0"), ("P+0", "P0"), ("P-0", "P0"),
            ("P-1", "P-1"), ("p -1", "P-1"), (" P+2 ", "P+2"),
            ("Q0", None), ("", None), (None, None), (0, None), ("P0x", None),
        ],
    )
    def test_accepts_the_spellings_models_produce(self, raw: object, expected: str | None) -> None:
        assert normalize_id(raw) == expected


class TestValidOps:
    def test_all_op_names_round_trip(self) -> None:
        payload = {
            "mode": "dictation",
            "ops": [
                {"op": "append_to_paragraph", "id": "P0", "text": "текст"},
                {"op": "insert_paragraphs_after", "id": "P0", "paragraphs": [{"text": "а"}]},
                {"op": "replace_paragraph", "id": "P0", "text": "б"},
                {"op": "replace_in_paragraph", "id": "P0", "find": "а", "replace": "б"},
                {"op": "delete_paragraph", "id": "P-1"},
                {"op": "set_style", "id": "P0", "style": "heading1"},
                {"op": "revert", "count": 2},
                {"op": "noop", "reason": "нечего делать"},
            ],
        }
        assert ops_of(payload) == list(OP_NAMES)

    def test_mode_is_preserved(self) -> None:
        assert parse_ops({"mode": "command", "ops": []}, IDS).mode == "command"

    def test_unknown_mode_falls_back_to_dictation(self) -> None:
        assert parse_ops({"mode": "нечто", "ops": []}, IDS).mode == "dictation"

    def test_revert_count_is_clamped(self) -> None:
        batch = parse_ops({"ops": [{"op": "revert", "count": 999}]}, IDS)
        assert batch.ops[0].count == 20
        batch = parse_ops({"ops": [{"op": "revert", "count": "nonsense"}]}, IDS)
        assert batch.ops[0].count == 1

    def test_paragraph_style_defaults_when_unknown(self) -> None:
        batch = parse_ops(
            {"ops": [{"op": "insert_paragraphs_after", "id": "P0",
                      "paragraphs": [{"text": "а", "style": "выдумка"}]}]},
            IDS,
        )
        assert batch.ops[0].paragraphs[0].style == "normal"


class TestRejection:
    def test_unknown_op_is_dropped(self) -> None:
        assert ops_of({"ops": [{"op": "drop_database", "id": "P0"}]}) == []

    def test_id_outside_the_window_is_dropped(self) -> None:
        assert ops_of({"ops": [{"op": "delete_paragraph", "id": "P-9"}]}) == []

    def test_missing_id_is_dropped(self) -> None:
        assert ops_of({"ops": [{"op": "append_to_paragraph", "text": "текст"}]}) == []

    def test_empty_text_is_dropped(self) -> None:
        assert ops_of({"ops": [{"op": "append_to_paragraph", "id": "P0", "text": "   "}]}) == []

    def test_insert_with_no_usable_paragraphs_is_dropped(self) -> None:
        assert ops_of({"ops": [{"op": "insert_paragraphs_after", "id": "P0",
                                "paragraphs": [{"text": ""}, {"nope": 1}]}]}) == []

    def test_unknown_style_drops_set_style(self) -> None:
        assert ops_of({"ops": [{"op": "set_style", "id": "P0", "style": "выдумка"}]}) == []

    def test_non_object_entries_are_ignored(self) -> None:
        assert ops_of({"ops": ["nonsense", 42, None]}) == []

    def test_ops_not_a_list_is_survivable(self) -> None:
        assert parse_ops({"mode": "dictation", "ops": "nope"}, IDS).ops == []

    def test_empty_payload_is_survivable(self) -> None:
        batch = parse_ops({}, IDS)
        assert batch.ops == [] and batch.mode == "dictation"


class TestSearchLimit:
    def test_long_fragments_survive_parsing(self) -> None:
        # Word's 255-character search limit no longer applies: finalisation resolves the
        # fragment in Python against the hash-verified paragraph text. Dropping it here
        # would throw away a dictated sentence for no reason.
        payload = {"ops": [{"op": "replace_in_paragraph", "id": "P0",
                            "find": "я" * (MAX_SEARCH_LEN + 50), "replace": "б"}]}
        assert ops_of(payload) == ["replace_in_paragraph"]

    def test_blank_find_is_dropped(self) -> None:
        assert ops_of({"ops": [{"op": "replace_in_paragraph", "id": "P0",
                                "find": "  ", "replace": "б"}]}) == []


class TestSchemaShape:
    """The schema is handed to Ollama as a decoding grammar, so it must stay well formed."""

    def test_declares_the_op_names_and_styles_we_accept(self) -> None:
        item = LLM_RESPONSE_SCHEMA["properties"]["ops"]["items"]
        assert item["properties"]["op"]["enum"] == list(OP_NAMES)
        assert item["properties"]["style"]["enum"] == list(STYLES)
        assert item["required"] == ["op"]

    def test_only_op_is_required_so_the_grammar_stays_flat(self) -> None:
        # A discriminated union converts poorly to a GBNF grammar; every op-specific
        # field must therefore be optional.
        item = LLM_RESPONSE_SCHEMA["properties"]["ops"]["items"]
        assert set(item["required"]) == {"op"}

    def test_top_level_requires_mode_and_ops(self) -> None:
        assert LLM_RESPONSE_SCHEMA["required"] == ["mode", "ops"]
