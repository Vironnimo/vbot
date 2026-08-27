"""Tests for strict semantic Tool-argument helpers."""

from __future__ import annotations

import pytest

from core.tools.arguments import (
    ToolArgumentError,
    line_number_gutter_candidates,
    logical_line_count,
    looks_like_line_numbered_content,
    optional_bool,
    optional_int,
    optional_number,
    optional_string,
    required_int,
    required_string,
    strip_line_number_gutters,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", 0),
        ("one", 1),
        ("one\n", 1),
        ("one\ntwo", 2),
        ("one\r\ntwo\r\n", 2),
        ("one\rtwo", 2),
        ("\n\n", 2),
    ],
)
def test_logical_line_count_handles_text_file_endings(text: str, expected: int) -> None:
    assert logical_line_count(text) == expected


class TestOptionalString:
    def test_absent_is_none(self) -> None:
        assert optional_string(None, field_name="x") is None

    @pytest.mark.parametrize(("value", "expected"), [("", ""), ("   ", ""), ("\t\n", "")])
    def test_blank_is_preserved_as_present(self, value: str, expected: str) -> None:
        assert optional_string(value, field_name="x") == expected

    def test_value_is_trimmed(self) -> None:
        assert optional_string("  abc  ", field_name="x") == "abc"

    @pytest.mark.parametrize("value", [123, 1.5, True, ["a"], {"a": 1}])
    def test_non_string_is_rejected(self, value: object) -> None:
        with pytest.raises(ToolArgumentError):
            optional_string(value, field_name="x")


class TestRequiredString:
    @pytest.mark.parametrize("value", [None, "", "   ", 5])
    def test_blank_or_missing_is_rejected(self, value: object) -> None:
        with pytest.raises(ToolArgumentError):
            required_string(value, field_name="x")

    def test_trims_by_default(self) -> None:
        assert required_string("  abc ", field_name="x") == "abc"

    def test_can_preserve_whitespace(self) -> None:
        assert required_string("  abc ", field_name="x", strip=False) == "  abc "


class TestOptionalInt:
    def test_absent_yields_default(self) -> None:
        assert optional_int(None, field_name="n", default=7) == 7

    def test_accepts_integer(self) -> None:
        assert optional_int(5, field_name="n") == 5

    @pytest.mark.parametrize("value", [True, False, 1.5, 5.0, "5", "", "abc", [], {}])
    def test_rejects_non_integers(self, value: object) -> None:
        with pytest.raises(ToolArgumentError):
            optional_int(value, field_name="n")

    def test_enforces_bounds(self) -> None:
        assert optional_int(3, field_name="n", minimum=1, maximum=5) == 3
        with pytest.raises(ToolArgumentError):
            optional_int(9, field_name="n", minimum=1, maximum=5)

    def test_minimum_only_message(self) -> None:
        with pytest.raises(ToolArgumentError):
            optional_int(0, field_name="n", minimum=1)


class TestRequiredInt:
    @pytest.mark.parametrize("value", [None, "", "   ", 12.0])
    def test_absent_or_wrong_type_is_rejected(self, value: object) -> None:
        with pytest.raises(ToolArgumentError):
            required_int(value, field_name="n")

    def test_accepts_integer(self) -> None:
        assert required_int(12, field_name="n") == 12

    def test_rejects_bool(self) -> None:
        with pytest.raises(ToolArgumentError):
            required_int(True, field_name="n")


class TestOptionalNumber:
    def test_absent_yields_default(self) -> None:
        assert optional_number(None, field_name="t", default=30.0) == 30.0

    @pytest.mark.parametrize(("value", "expected"), [(5, 5.0), (1.5, 1.5)])
    def test_accepts_numbers(self, value: object, expected: float) -> None:
        assert optional_number(value, field_name="t") == expected

    @pytest.mark.parametrize("value", [True, "1.5", "", "abc", [], {}])
    def test_rejects_non_numbers(self, value: object) -> None:
        with pytest.raises(ToolArgumentError):
            optional_number(value, field_name="t")

    @pytest.mark.parametrize(
        "value",
        [float("nan"), float("inf"), float("-inf")],
    )
    def test_rejects_non_finite_numbers(self, value: object) -> None:
        with pytest.raises(ToolArgumentError):
            optional_number(value, field_name="t")

    def test_inclusive_minimum(self) -> None:
        assert optional_number(0, field_name="t", minimum=0) == 0.0
        with pytest.raises(ToolArgumentError):
            optional_number(-1, field_name="t", minimum=0)

    def test_exclusive_minimum(self) -> None:
        assert optional_number(0.1, field_name="t", minimum=0, minimum_exclusive=True) == 0.1
        with pytest.raises(ToolArgumentError):
            optional_number(0, field_name="t", minimum=0, minimum_exclusive=True)


class TestOptionalBool:
    def test_absent_yields_default(self) -> None:
        assert optional_bool(None, field_name="b", default=True) is True

    @pytest.mark.parametrize(("value", "expected"), [(True, True), (False, False)])
    def test_accepts_booleans(self, value: object, expected: bool) -> None:
        assert optional_bool(value, field_name="b", default=False) is expected

    @pytest.mark.parametrize("value", [0, 1, 2, -1, "", "true", "maybe", 1.0, [], {}])
    def test_rejects_other_values(self, value: object) -> None:
        with pytest.raises(ToolArgumentError):
            optional_bool(value, field_name="b", default=False)


class TestLooksLikeLineNumberedContent:
    @pytest.mark.parametrize(
        "text",
        [
            "1| import os\n2| import sys\n3| \n",  # current read output, including blank
            "50:50001| continuing line\n51| next line",  # continuation then next line
            "1|import os\n2|import sys\n3|\n",  # compact pasted gutters remain detectable
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


class TestLineNumberGutterCandidates:
    def test_strips_current_gutter_and_preserves_source_indentation(self) -> None:
        assert line_number_gutter_candidates("10| def f():\r\n11|     return 1\r\n") == (
            "def f():\r\n    return 1\r\n",
            " def f():\r\n     return 1\r\n",
        )

    def test_strips_compact_and_continuation_gutters(self) -> None:
        assert line_number_gutter_candidates("50:50001|fragment\n51|next") == ("fragment\nnext",)

    def test_can_reject_continuation_gutters(self) -> None:
        assert (
            line_number_gutter_candidates("50:50001|fragment\n51|next", allow_continuations=False)
            == ()
        )

    def test_can_recover_a_complete_nonconsecutive_shape_for_diagnostics(self) -> None:
        assert line_number_gutter_candidates("10| alpha\n12| gamma", require_consecutive=False) == (
            "alpha\ngamma",
            " alpha\n gamma",
        )

    @pytest.mark.parametrize(
        "text",
        [
            "1|only one line",
            "1|first\n3|third",
            "1|first\nordinary second line",
            "| name | id |\n| --- | --- |",
        ],
    )
    def test_rejects_incomplete_or_ordinary_blocks(self, text: str) -> None:
        assert line_number_gutter_candidates(text) == ()


class TestStripLineNumberGutters:
    def test_strips_single_line_gutter(self) -> None:
        assert strip_line_number_gutters("42|     return 1") == "    return 1"

    def test_strips_single_line_compact_gutter(self) -> None:
        assert strip_line_number_gutters("42|return 1") == "return 1"

    def test_strips_continuation_gutter(self) -> None:
        assert strip_line_number_gutters("50:50001| fragment") == "fragment"

    def test_strips_partial_gutter_leaving_plain_lines_unchanged(self) -> None:
        assert strip_line_number_gutters("1| def foo():\n    return 1\n3| return 2") == (
            "def foo():\n    return 1\nreturn 2"
        )

    def test_strips_non_consecutive_gutter_numbers(self) -> None:
        assert strip_line_number_gutters("1| alpha\n3| gamma") == "alpha\ngamma"

    def test_preserves_line_endings(self) -> None:
        assert strip_line_number_gutters("10| def f():\r\n11|     return 1\r\n") == (
            "def f():\r\n    return 1\r\n"
        )

    def test_strips_indented_gutter_block(self) -> None:
        # Leading whitespace before the gutter (model indentation when pasting)
        # is removed along with the gutter — it is not file content.
        assert strip_line_number_gutters("  10| def f():\n  11|     return 1") == (
            "def f():\n    return 1"
        )

    def test_strips_blank_line_gutter(self) -> None:
        assert strip_line_number_gutters("1| alpha\n2| \n3| gamma") == "alpha\n\ngamma"

    def test_returns_none_when_no_line_has_gutter(self) -> None:
        assert strip_line_number_gutters("def foo():\n    return 1") is None

    def test_returns_none_for_pipe_without_digit_prefix(self) -> None:
        assert strip_line_number_gutters("echo hi | grep foo") is None

    def test_returns_none_for_empty_string(self) -> None:
        assert strip_line_number_gutters("") is None

    def test_non_string_returns_none(self) -> None:
        assert strip_line_number_gutters(None) is None  # type: ignore[arg-type]

    def test_does_not_strip_pipe_in_middle_of_line(self) -> None:
        # A pipe preceded by non-digit text is not a gutter.
        assert strip_line_number_gutters("x = a|b\nc = d|e") is None
