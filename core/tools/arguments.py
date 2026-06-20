"""Lenient coercion for model-supplied tool arguments.

Tool arguments arrive exactly as the model encoded them — nothing validates or
coerces them against the JSON Schema before a handler runs (the schema is only a
hint the model is free to ignore). Models routinely encode an omitted optional
field as ``""``, an integer as ``"5"``, or a boolean as ``"true"``/``"false"``.
Those encodings are unambiguous in intent but trip a strict ``isinstance`` check,
so an otherwise valid call fails.

These helpers accept the common creative-but-unambiguous encodings while still
rejecting genuinely wrong types (a word where a number belongs, an object where a
string belongs). They are the single home for that policy so the tool surface
stays consistent instead of each tool re-deriving it.

Every helper raises :class:`ToolArgumentError` (a ``ValueError``) on bad input.
Most tool handlers already wrap argument parsing in ``except ValueError`` and the
tool dispatch layer maps a stray ``ValueError`` to an ``invalid_arguments``
failure, so raising is the idiomatic signal here.
"""

from __future__ import annotations

from typing import overload

from core.tools.tools import JsonObject

_TRUE_STRINGS = frozenset({"true", "1", "yes", "on"})
_FALSE_STRINGS = frozenset({"false", "0", "no", "off"})


class ToolArgumentError(ValueError):
    """An invalid tool argument supplied by the model.

    Subclasses ``ValueError`` so the existing ``except ValueError`` parsing
    guards in tool handlers keep catching it without change.
    """


def optional_string(value: object, *, field_name: str) -> str | None:
    """Return a trimmed optional string, or ``None`` when absent or blank.

    A blank/whitespace value is treated as omitted — the documented "omitted"
    semantics for optional id-like fields — so a model that fills an optional
    field with ``""`` is not rejected. A present non-string value is an error.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ToolArgumentError(f"{field_name} must be a string")
    return value.strip() or None


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
    """Coerce an optional integer, accepting ``"5"`` and ``5.0``.

    Absent (``None``) or blank yields ``default``. A string or whole-valued
    float is accepted; a boolean, a fractional number, or non-numeric text is an
    error. The optional ``minimum``/``maximum`` bounds are inclusive.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
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
    """Coerce a required integer, accepting ``"5"`` and ``5.0``.

    Absent or blank is an error; otherwise the same acceptance rules and
    inclusive bounds as :func:`optional_int` apply.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
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
    """Coerce an optional real number, accepting numeric strings like ``"1.5"``.

    Absent or blank yields ``default``. ``minimum`` is inclusive unless
    ``minimum_exclusive`` is set (used for ``> 0`` bounds like a timeout).
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    number = _to_float(value, field_name)
    _check_float_minimum(
        number, field_name=field_name, minimum=minimum, exclusive=minimum_exclusive
    )
    return number


def coerce_bool(value: object, *, field_name: str, default: bool) -> bool:
    """Coerce a boolean, accepting ``"true"``/``"false"``, ``"yes"``/``"no"`` and ``0``/``1``.

    Absent (``None``) or blank yields ``default``. Any other value (a number
    other than 0/1, an unrecognized word, an object) is an error.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        if value in (0, 1):
            return bool(value)
        raise ToolArgumentError(f"{field_name} must be a boolean")
    if isinstance(value, str):
        text = value.strip().lower()
        if not text:
            return default
        if text in _TRUE_STRINGS:
            return True
        if text in _FALSE_STRINGS:
            return False
        raise ToolArgumentError(f"{field_name} must be a boolean")
    raise ToolArgumentError(f"{field_name} must be a boolean")


def normalize_aliases(arguments: JsonObject, aliases: dict[str, str]) -> JsonObject:
    """Return ``arguments`` with known alias keys renamed to their canonical key.

    Models trained on other tool schemas sometimes emit a field under a
    different casing (e.g. ``oldString`` for ``old_string``). Mapping a small set
    of known aliases onto the canonical key accepts those calls instead of
    failing them as unknown arguments. A canonical key already present wins; the
    alias is dropped. The input is not mutated.
    """
    if not any(alias in arguments for alias in aliases):
        return arguments
    normalized = dict(arguments)
    for alias, canonical in aliases.items():
        if alias in normalized:
            aliased_value = normalized.pop(alias)
            normalized.setdefault(canonical, aliased_value)
    return normalized


def _to_int(value: object, field_name: str) -> int:
    if isinstance(value, bool):
        raise ToolArgumentError(f"{field_name} must be an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not value.is_integer():
            raise ToolArgumentError(f"{field_name} must be an integer")
        return int(value)
    if isinstance(value, str):
        return _int_from_string(value, field_name)
    raise ToolArgumentError(f"{field_name} must be an integer")


def _int_from_string(text: str, field_name: str) -> int:
    stripped = text.strip()
    try:
        return int(stripped)
    except ValueError:
        pass
    try:
        number = float(stripped)
    except ValueError:
        raise ToolArgumentError(f"{field_name} must be an integer") from None
    if not number.is_integer():
        raise ToolArgumentError(f"{field_name} must be an integer")
    return int(number)


def _to_float(value: object, field_name: str) -> float:
    if isinstance(value, bool):
        raise ToolArgumentError(f"{field_name} must be a number")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            raise ToolArgumentError(f"{field_name} must be a number") from None
    raise ToolArgumentError(f"{field_name} must be a number")


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
    "ToolArgumentError",
    "coerce_bool",
    "normalize_aliases",
    "optional_int",
    "optional_number",
    "optional_string",
    "required_int",
    "required_string",
]
