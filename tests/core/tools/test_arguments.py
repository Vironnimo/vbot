"""Shared Tool-argument helpers: strict typed values and pasted read gutters."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from core.tools.arguments import (
    line_number_gutter_candidates,
    optional_bool,
    optional_int,
    optional_number,
    optional_string,
    required_int,
    required_string,
    strip_line_number_gutters,
)
from core.tools.contracts import ToolContractError


@pytest.mark.parametrize(
    ("read", "value", "options", "expected"),
    [
        (optional_string, None, {}, None),
        (optional_string, " \t\n", {}, ""),  # a present blank stays present
        (optional_string, "  abc  ", {}, "abc"),
        (required_string, "  abc ", {}, "abc"),
        (required_string, "  abc ", {"strip": False}, "  abc "),
        (optional_int, None, {"default": 7}, 7),
        (optional_int, 3, {"minimum": 1, "maximum": 5}, 3),
        (required_int, 12, {}, 12),
        (optional_number, None, {"default": 30.0}, 30.0),
        (optional_number, 5, {}, 5.0),
        (optional_number, 0, {"minimum": 0}, 0.0),
        (optional_number, 0.1, {"minimum": 0, "minimum_exclusive": True}, 0.1),
        (optional_bool, None, {"default": True}, True),
        (optional_bool, False, {"default": True}, False),
    ],
)
def test_valid_values_are_returned_in_their_strict_type(
    read: Callable[..., object], value: object, options: dict, expected: object
) -> None:
    result = read(value, field_name="x", **options)
    assert result == expected
    assert type(result) is type(expected)


@pytest.mark.parametrize(
    ("read", "value", "options", "message"),
    [
        (optional_string, 123, {}, "x must be a string"),
        (required_string, None, {}, "x must be a non-empty string"),
        (required_string, "   ", {}, "x must be a non-empty string"),
        (optional_int, True, {}, "x must be an integer"),
        (optional_int, 5.0, {}, "x must be an integer"),
        (optional_int, "5", {}, "x must be an integer"),
        (optional_int, 9, {"minimum": 1, "maximum": 5}, "x must be between 1 and 5"),
        (optional_int, 0, {"minimum": 1}, "x must be >= 1"),
        (optional_int, 6, {"maximum": 5}, "x must be <= 5"),
        (required_int, None, {}, "x must be an integer"),
        (required_int, True, {}, "x must be an integer"),
        (optional_number, True, {}, "x must be a number"),
        (optional_number, "1.5", {}, "x must be a number"),
        (optional_number, float("nan"), {}, "x must be a finite number"),
        (optional_number, float("-inf"), {}, "x must be a finite number"),
        (optional_number, -1, {"minimum": 0}, "x must be >= 0"),
        (optional_number, 0, {"minimum": 0, "minimum_exclusive": True}, "x must be > 0"),
        (optional_bool, 1, {"default": False}, "x must be a boolean"),
        (optional_bool, "true", {"default": False}, "x must be a boolean"),
    ],
)
def test_invalid_values_name_the_field_and_the_expected_value(
    read: Callable[..., object], value: object, options: dict, message: str
) -> None:
    with pytest.raises(ToolContractError) as error:
        read(value, field_name="x", **options)
    assert str(error.value) == message


@pytest.mark.parametrize(
    ("text", "options", "candidates"),
    [
        # The current gutter: the first candidate drops the separator space, the
        # second keeps it for compact gutters pasted without one.
        (
            "10| def f():\r\n11|     return 1\r\n",
            {},
            ("def f():\r\n    return 1\r\n", " def f():\r\n     return 1\r\n"),
        ),
        ("50:50001|fragment\n51|next", {}, ("fragment\nnext",)),
        ("50:50001|fragment\n51|next", {"allow_continuations": False}, ()),
        (
            "10| alpha\n12| gamma",
            {"require_consecutive": False},
            ("alpha\ngamma", " alpha\n gamma"),
        ),
        ("1|only one line", {}, ()),
        ("1|first\n3|third", {}, ()),
        ("1|first\nordinary second line", {}, ()),
        ("| name | id |\n| --- | --- |", {}, ()),
    ],
)
def test_gutter_candidates_need_a_complete_numbered_block(
    text: str, options: dict, candidates: tuple[str, ...]
) -> None:
    assert line_number_gutter_candidates(text, **options) == candidates


@pytest.mark.parametrize(
    ("text", "stripped"),
    [
        ("42|     return 1", "    return 1"),
        ("42|return 1", "return 1"),
        ("50:50001| fragment", "fragment"),
        ("1| def foo():\n    return 1\n3| return 2", "def foo():\n    return 1\nreturn 2"),
        ("10| def f():\r\n11|     return 1\r\n", "def f():\r\n    return 1\r\n"),
        # Indentation before the gutter is the model's, not file content.
        ("  10| def f():\n  11|     return 1", "def f():\n    return 1"),
        ("1| alpha\n2| \n3| gamma", "alpha\n\ngamma"),
        ("def foo():\n    return 1", None),
        ("echo hi | grep foo", None),
        ("x = a|b\nc = d|e", None),
        ("", None),
        (None, None),
    ],
)
def test_gutters_are_stripped_line_by_line(text: str, stripped: str | None) -> None:
    assert strip_line_number_gutters(text) == stripped
