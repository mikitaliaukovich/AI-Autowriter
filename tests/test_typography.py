"""Typography rules are the one part of the pipeline that must be exactly right every
time, so they get the densest tests. No models are loaded here."""
from __future__ import annotations

import pytest

from service.config import TypographyConfig
from service.pipeline.typography import NBSP, join_spacing, normalize

DEFAULT = TypographyConfig()


def n(text: str, cfg: TypographyConfig = DEFAULT, **kw: object) -> str:
    return normalize(text, cfg, **kw)  # type: ignore[arg-type]


class TestDialogueDash:
    def test_hyphen_opening_becomes_em_dash_and_nbsp(self) -> None:
        assert n("- Почему ты это сделал?") == f"—{NBSP}Почему ты это сделал?"

    @pytest.mark.parametrize("dash", ["-", "--", "–", "—", "―"])
    def test_any_dash_flavour_is_normalised(self, dash: str) -> None:
        assert n(f"{dash} Реплика.") == f"—{NBSP}Реплика."

    def test_author_attribution_dash_inside_line(self) -> None:
        assert n("- Почему? - гневно спросила Мэри.") == (
            f"—{NBSP}Почему? —{NBSP}гневно спросила Мэри."
        )

    def test_hyphenated_word_is_left_alone(self) -> None:
        assert n("Кто-то что-то сказал по-русски.") == "Кто-то что-то сказал по-русски."

    def test_appended_text_gets_no_opening_dash(self) -> None:
        # Mid-paragraph continuation: a leading dash is not a dialogue marker.
        assert n("- продолжение", paragraph_start=False).startswith("-")

    def test_numeric_range_uses_en_dash(self) -> None:
        assert n("В 1941-1945 годах.") == "В 1941–1945 годах."


class TestQuotes:
    def test_straight_quotes_become_guillemets(self) -> None:
        assert n('Он читал "Войну и мир" вчера.') == "Он читал «Войну и мир» вчера."

    def test_nested_quotes(self) -> None:
        assert n('Он сказал: "роман "Мы" хорош".') == "Он сказал: «роман „Мы“ хорош»."

    def test_english_curly_quotes_are_converted(self) -> None:
        assert n("Он читал “Войну и мир” вчера.") == "Он читал «Войну и мир» вчера."

    def test_existing_guillemets_survive(self) -> None:
        assert n("Он читал «Войну и мир».") == "Он читал «Войну и мир»."


class TestEllipsisAndSpacing:
    def test_three_dots_become_ellipsis(self) -> None:
        assert n("Я не знаю...") == "Я не знаю…"

    def test_many_dots_collapse(self) -> None:
        assert n("Что.....") == "Что…"

    def test_double_spaces_collapse(self) -> None:
        assert n("Слово   другое") == "Слово другое"

    def test_no_space_before_punctuation(self) -> None:
        assert n("Слово , другое .") == "Слово, другое."

    def test_space_inserted_after_punctuation(self) -> None:
        assert n("Слово,другое.Ещё") == "Слово, другое. Ещё"

    def test_decimal_numbers_are_not_split(self) -> None:
        assert n("Цена 3,50 или 10.25") == "Цена 3,50 или 10.25"

    def test_no_space_after_opening_quote(self) -> None:
        assert n("Он читал « Война » вчера.") == "Он читал «Война» вчера."


class TestShortWordBinding:
    def test_off_by_default(self) -> None:
        assert n("Он шёл в лес.") == "Он шёл в лес."

    def test_on_when_enabled(self) -> None:
        cfg = TypographyConfig(nbsp_short_words=True)
        assert n("Он шёл в лес.", cfg) == f"Он шёл в{NBSP}лес."

    def test_does_not_bind_word_ending_in_short_letter(self) -> None:
        cfg = TypographyConfig(nbsp_short_words=True)
        # The "а" of "она" must not be treated as a standalone preposition.
        assert n("Она шла.", cfg) == "Она шла."


class TestJoinSpacing:
    @pytest.mark.parametrize(
        ("left", "right", "expected"),
        [
            ("Он шёл", "домой", " "),
            ("Он шёл ", "домой", ""),
            ("Он шёл", " домой", ""),
            ("Он шёл", ", устало", ""),
            ("Он сказал: «", "Привет", ""),
            ("", "домой", ""),
            ("Он шёл", "", ""),
        ],
    )
    def test_separator(self, left: str, right: str, expected: str) -> None:
        assert join_spacing(left, right) == expected


class TestBriefExample:
    """The worked example from the project brief, in Russian."""

    def test_dialogue_pair(self) -> None:
        raw = [
            "- Почему ты это сделал? - гневно сказала Мэри.",
            "- Потому что я тот, кто разрушит этот мир.",
        ]
        assert [n(p) for p in raw] == [
            f"—{NBSP}Почему ты это сделал? —{NBSP}гневно сказала Мэри.",
            f"—{NBSP}Потому что я тот, кто разрушит этот мир.",
        ]
