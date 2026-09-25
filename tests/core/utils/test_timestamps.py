"""The canonical UTC timestamp form: formatting, parsing and normalization."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from core.utils.timestamps import (
    canonical_timestamp,
    format_canonical_timestamp,
    parse_canonical_timestamp,
    parse_timestamp,
    utc_now_timestamp,
)

_PLUS_TWO = timezone(timedelta(hours=2))


@pytest.mark.parametrize(
    "value",
    [
        "2026-07-01T12:00:00Z",
        "2026-07-01T12:00:00z",
        "2026-07-01T12:00:00+00:00",
        "2026-07-01T14:00:00.000000+02:00",
        datetime(2026, 7, 1, 14, tzinfo=_PLUS_TWO),
    ],
)
def test_every_aware_form_normalizes_to_one_fixed_width_text(value: str | datetime) -> None:
    assert canonical_timestamp(value) == "2026-07-01T12:00:00.000000Z"


@pytest.mark.parametrize(
    "value", ["", "2026-07-01T12:00:00", "not a time", datetime(2026, 7, 1, 12)]
)
def test_a_value_without_an_offset_is_never_guessed(value: str | datetime) -> None:
    with pytest.raises(ValueError):
        canonical_timestamp(value)


def test_parse_returns_aware_utc_and_rejects_naive_text() -> None:
    parsed = parse_timestamp("2026-07-01T14:00:00.250000+02:00")
    assert parsed == datetime(2026, 7, 1, 12, 0, 0, 250_000, tzinfo=UTC)
    assert parsed.tzinfo is UTC
    with pytest.raises(ValueError, match="timezone"):
        parse_timestamp("2026-07-01T12:00:00")


def test_formatting_and_the_clock_use_the_canonical_form() -> None:
    with pytest.raises(ValueError, match="timezone"):
        format_canonical_timestamp(datetime(2026, 7, 1, 12))
    now = utc_now_timestamp()
    assert len(now) == len("2026-07-01T12:00:00.000000Z") and now.endswith("Z")
    assert canonical_timestamp(now) == now
    assert parse_timestamp(now) <= datetime.now(UTC)


def test_stored_values_parse_only_in_the_canonical_form() -> None:
    assert parse_canonical_timestamp("2026-07-01T12:00:00.250000Z") == datetime(
        2026, 7, 1, 12, 0, 0, 250_000, tzinfo=UTC
    )


@pytest.mark.parametrize(
    "value",
    [
        "2026-07-01T12:00:00Z",
        "2026-07-01T12:00:00.000000+00:00",
        "2026-07-01T12:00:00.000000z",
        "2026-07-01T12:00:00.25Z",
        "2026-07-01T12:00:00.000000",
        "",
    ],
)
def test_a_stored_value_in_any_other_form_is_bad_data(value: str) -> None:
    with pytest.raises(ValueError):
        parse_canonical_timestamp(value)
