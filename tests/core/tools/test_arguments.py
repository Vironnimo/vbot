"""Tests for the shared lenient tool-argument coercion helpers."""

from __future__ import annotations

import pytest

from core.tools.arguments import (
    ToolArgumentError,
    coerce_bool,
    looks_like_line_numbered_content,
    normalize_aliases,
    optional_int,
    optional_number,
    optional_string,
    required_int,
    required_string,
)


class TestOptionalString:
    def test_absent_is_none(self) -> None:
        assert optional_string(None, field_name="x") is None

    @pytest.mark.parametrize("value", ["", "   ", "\t\n"])
    def test_blank_is_treated_as_omitted(self, value: str) -> None:
        # The reported bug class: a blank optional field means "omitted".
        assert optional_string(value, field_name="x") is None

    def test_value_is_trimmed(self) -> None:
        assert optional_string("  abc  ", field_name="x") == "abc"

    @pytest.mark.parametrize("value", [123, 1.5, True, ["a"], {"a": 1}])
    def test_non_string_is_rejected(self, value: object) -> None:
        with pytest.raises(ToolArgumentError, match="x must be a string"):
            optional_string(value, field_name="x")


class TestRequiredString:
    @pytest.mark.parametrize("value", [None, "", "   ", 5])
    def test_blank_or_missing_is_rejected(self, value: object) -> None:
        with pytest.raises(ToolArgumentError, match="x must be a non-empty string"):
            required_string(value, field_name="x")

    def test_trims_by_default(self) -> None:
        assert required_string("  abc ", field_name="x") == "abc"

    def test_can_preserve_whitespace(self) -> None:
        assert required_string("  abc ", field_name="x", strip=False) == "  abc "


class TestOptionalInt:
    @pytest.mark.parametrize("value", [None, "", "   "])
    def test_absent_or_blank_yields_default(self, value: object) -> None:
        assert optional_int(value, field_name="n", default=7) == 7

    @pytest.mark.parametrize(("value", "expected"), [(5, 5), (5.0, 5), ("5", 5), ("  5 ", 5)])
    def test_accepts_int_wholefloat_and_string(self, value: object, expected: int) -> None:
        assert optional_int(value, field_name="n") == expected

    def test_accepts_whole_float_string(self) -> None:
        assert optional_int("5.0", field_name="n") == 5

    @pytest.mark.parametrize("value", [True, False, 1.5, "5.5", "abc", "5x", [], {}])
    def test_rejects_non_integers(self, value: object) -> None:
        with pytest.raises(ToolArgumentError, match="n must be an integer"):
            optional_int(value, field_name="n")

    def test_enforces_bounds(self) -> None:
        assert optional_int("3", field_name="n", minimum=1, maximum=5) == 3
        with pytest.raises(ToolArgumentError, match="between 1 and 5"):
            optional_int(9, field_name="n", minimum=1, maximum=5)

    def test_minimum_only_message(self) -> None:
        with pytest.raises(ToolArgumentError, match="n must be >= 1"):
            optional_int(0, field_name="n", minimum=1)


class TestRequiredInt:
    @pytest.mark.parametrize("value", [None, "", "   "])
    def test_absent_or_blank_is_rejected(self, value: object) -> None:
        with pytest.raises(ToolArgumentError, match="n must be an integer"):
            required_int(value, field_name="n")

    def test_accepts_string(self) -> None:
        assert required_int("12", field_name="n") == 12

    def test_rejects_bool(self) -> None:
        with pytest.raises(ToolArgumentError, match="n must be an integer"):
            required_int(True, field_name="n")


class TestOptionalNumber:
    @pytest.mark.parametrize("value", [None, "", "  "])
    def test_absent_or_blank_yields_default(self, value: object) -> None:
        assert optional_number(value, field_name="t", default=30.0) == 30.0

    @pytest.mark.parametrize(
        ("value", "expected"), [(5, 5.0), (1.5, 1.5), ("1.5", 1.5), ("2", 2.0)]
    )
    def test_accepts_numbers_and_strings(self, value: object, expected: float) -> None:
        assert optional_number(value, field_name="t") == expected

    @pytest.mark.parametrize("value", [True, "abc", [], {}])
    def test_rejects_non_numbers(self, value: object) -> None:
        with pytest.raises(ToolArgumentError, match="t must be a number"):
            optional_number(value, field_name="t")

    def test_inclusive_minimum(self) -> None:
        assert optional_number(0, field_name="t", minimum=0) == 0.0
        with pytest.raises(ToolArgumentError, match="t must be >= 0"):
            optional_number(-1, field_name="t", minimum=0)

    def test_exclusive_minimum(self) -> None:
        assert optional_number("0.1", field_name="t", minimum=0, minimum_exclusive=True) == 0.1
        with pytest.raises(ToolArgumentError, match="t must be > 0"):
            optional_number(0, field_name="t", minimum=0, minimum_exclusive=True)


class TestCoerceBool:
    def test_absent_yields_default(self) -> None:
        assert coerce_bool(None, field_name="b", default=True) is True
        assert coerce_bool("", field_name="b", default=False) is False

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (True, True),
            (False, False),
            ("true", True),
            ("False", False),
            ("YES", True),
            ("no", False),
            ("on", True),
            ("off", False),
            ("1", True),
            ("0", False),
            (1, True),
            (0, False),
        ],
    )
    def test_accepts_common_encodings(self, value: object, expected: bool) -> None:
        assert coerce_bool(value, field_name="b", default=None) is expected  # type: ignore[arg-type]

    @pytest.mark.parametrize("value", [2, -1, "maybe", 1.0, [], {}])
    def test_rejects_other_values(self, value: object) -> None:
        with pytest.raises(ToolArgumentError, match="b must be a boolean"):
            coerce_bool(value, field_name="b", default=False)


class TestNormalizeAliases:
    def test_renames_alias_to_canonical(self) -> None:
        result = normalize_aliases({"oldString": "x"}, {"oldString": "old_string"})
        assert result == {"old_string": "x"}

    def test_canonical_key_wins_when_both_present(self) -> None:
        result = normalize_aliases(
            {"oldString": "alias", "old_string": "canonical"},
            {"oldString": "old_string"},
        )
        assert result == {"old_string": "canonical"}

    def test_returns_same_object_when_no_alias_present(self) -> None:
        arguments = {"old_string": "x"}
        assert normalize_aliases(arguments, {"oldString": "old_string"}) is arguments


class TestLooksLikeLineNumberedContent:
    @pytest.mark.parametrize(
        "text",
        [
            "1|import os\n2|import sys\n3|\n",  # consecutive, blank gutter line included
            "  10|alpha\n  11|beta\n  12|gamma",  # indented gutter, multi-digit
            "5|a\n6|b",  # the minimum: two consecutive numbered lines
        ],
    )
    def test_detects_pasted_gutter(self, text: str) -> None:
        assert looks_like_line_numbered_content(text) is True

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "plain single line",
            "1|only one numbered line\nplain\nplain",  # not dominant (1 of 3)
            "1|alpha\n5|beta",  # numbered but not consecutive
            "| name | id |\n| ---- | -- |\n| a | 1 |",  # Markdown table, no digit prefix
            "x = a|b\ny = c|d",  # literal pipes, no digit prefix
        ],
    )
    def test_passes_ordinary_content(self, text: str) -> None:
        assert looks_like_line_numbered_content(text) is False

    def test_non_string_is_false(self) -> None:
        assert looks_like_line_numbered_content(None) is False  # type: ignore[arg-type]
