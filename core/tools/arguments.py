"""Strict semantic helpers for already schema-validated Tool arguments."""

from __future__ import annotations

import math
from typing import overload

# The read tool prefixes every line with an unpadded ``N| `` reference gutter. This
# separator is the single source of truth shared by the read builder and the
# detector below, so the two never drift apart.
LINE_NUMBER_GUTTER_SEPARATOR = "|"
# A block is treated as the gutter (not ordinary content that merely contains a
# pipe) only when at least this share of its non-blank lines carry a consecutive
# ``N|`` prefix (with or without its separator space after model reproduction).
_LINE_NUMBER_GUTTER_DOMINANCE = 0.6
# Two consecutive numbered lines is the minimum signal: a lone ``1|value`` line
# or a sparse pipe table must still pass through.
_LINE_NUMBER_GUTTER_MIN_LINES = 2


class ToolArgumentError(ValueError):
    """An invalid tool argument supplied by the model.

    Subclasses ``ValueError`` so the existing ``except ValueError`` parsing
    guards in tool handlers keep catching it without change.
    """


def optional_string(value: object, *, field_name: str) -> str | None:
    """Return a trimmed optional string, preserving a present blank as blank."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ToolArgumentError(f"{field_name} must be a string")
    return value.strip()


def required_string(value: object, *, field_name: str, strip: bool = True) -> str:
    """Return a required non-blank string.

    ``strip=False`` keeps the original value verbatim (for fields where leading
    or trailing whitespace is meaningful) while still rejecting a blank value.
    """
    if not isinstance(value, str) or not value.strip():
        raise ToolArgumentError(f"{field_name} must be a non-empty string")
    return value.strip() if strip else value


@overload
def optional_int(
    value: object,
    *,
    field_name: str,
    default: int,
    minimum: int | None = ...,
    maximum: int | None = ...,
) -> int: ...


@overload
def optional_int(
    value: object,
    *,
    field_name: str,
    default: None = ...,
    minimum: int | None = ...,
    maximum: int | None = ...,
) -> int | None: ...


def optional_int(
    value: object,
    *,
    field_name: str,
    default: int | None = None,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int | None:
    """Return an optional strict integer with inclusive bounds."""
    if value is None:
        return default
    number = _to_int(value, field_name)
    _check_int_range(number, field_name=field_name, minimum=minimum, maximum=maximum)
    return number


def required_int(
    value: object,
    *,
    field_name: str,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    """Return a required strict integer with inclusive bounds."""
    if value is None:
        raise ToolArgumentError(f"{field_name} must be an integer")
    number = _to_int(value, field_name)
    _check_int_range(number, field_name=field_name, minimum=minimum, maximum=maximum)
    return number


@overload
def optional_number(
    value: object,
    *,
    field_name: str,
    default: float,
    minimum: float | None = ...,
    minimum_exclusive: bool = ...,
) -> float: ...


@overload
def optional_number(
    value: object,
    *,
    field_name: str,
    default: None = ...,
    minimum: float | None = ...,
    minimum_exclusive: bool = ...,
) -> float | None: ...


def optional_number(
    value: object,
    *,
    field_name: str,
    default: float | None = None,
    minimum: float | None = None,
    minimum_exclusive: bool = False,
) -> float | None:
    """Return an optional strict real number with an optional lower bound."""
    if value is None:
        return default
    number = _to_float(value, field_name)
    _check_float_minimum(
        number, field_name=field_name, minimum=minimum, exclusive=minimum_exclusive
    )
    return number


def optional_bool(value: object, *, field_name: str, default: bool) -> bool:
    """Return an optional strict boolean."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    raise ToolArgumentError(f"{field_name} must be a boolean")


def looks_like_line_numbered_content(text: str) -> bool:
    """Return whether ``text`` is dominated by the read tool's reference gutter.

    The read tool prefixes each line with ``N| `` and an in-line continuation with
    ``N:C| ``. If a model echoes either display format back into a write or edit,
    the file is silently corrupted with reference gutters. This detects that case
    so the write path can reject it, while still letting sparse literal-pipe content
    through (a lone ``1|value`` line, a Markdown table). The signal is deliberately
    strict: at least two lines, a majority of non-blank lines prefixed by a line
    number and the separator, and those line numbers running consecutively.
    """
    if not isinstance(text, str):
        return False

    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) < _LINE_NUMBER_GUTTER_MIN_LINES:
        return False

    numbers: list[int] = []
    for line in lines:
        prefix, separator, _rest = line.lstrip().partition(LINE_NUMBER_GUTTER_SEPARATOR)
        line_number, colon, character = prefix.partition(":")
        if separator and line_number.isdigit() and (not colon or character.isdigit()):
            numbers.append(int(line_number))

    if len(numbers) < _LINE_NUMBER_GUTTER_MIN_LINES:
        return False
    if len(numbers) / len(lines) < _LINE_NUMBER_GUTTER_DOMINANCE:
        return False

    consecutive = sum(
        1
        for previous, current in zip(numbers, numbers[1:], strict=False)
        if current == previous + 1
    )
    return consecutive >= len(numbers) - 1


def _to_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolArgumentError(f"{field_name} must be an integer")
    return value


def _to_float(value: object, field_name: str) -> float:
    if isinstance(value, bool):
        raise ToolArgumentError(f"{field_name} must be a number")
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        raise ToolArgumentError(f"{field_name} must be a number")
    if not math.isfinite(number):
        raise ToolArgumentError(f"{field_name} must be a finite number")
    return number


def _check_int_range(
    number: int, *, field_name: str, minimum: int | None, maximum: int | None
) -> None:
    if minimum is not None and maximum is not None and not (minimum <= number <= maximum):
        raise ToolArgumentError(f"{field_name} must be between {minimum} and {maximum}")
    if minimum is not None and number < minimum:
        raise ToolArgumentError(f"{field_name} must be >= {minimum}")
    if maximum is not None and number > maximum:
        raise ToolArgumentError(f"{field_name} must be <= {maximum}")


def _check_float_minimum(
    number: float, *, field_name: str, minimum: float | None, exclusive: bool
) -> None:
    if minimum is None:
        return
    if exclusive and number <= minimum:
        raise ToolArgumentError(f"{field_name} must be > {minimum}")
    if not exclusive and number < minimum:
        raise ToolArgumentError(f"{field_name} must be >= {minimum}")


__all__ = [
    "LINE_NUMBER_GUTTER_SEPARATOR",
    "ToolArgumentError",
    "looks_like_line_numbered_content",
    "optional_bool",
    "optional_int",
    "optional_number",
    "optional_string",
    "required_int",
    "required_string",
]
