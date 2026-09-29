"""A persistent condition is logged when it starts, changes or ends, never on repetition."""

from __future__ import annotations

from core.utils.log_conditions import LoggedConditions


def test_a_condition_logs_its_start_changes_and_end_within_a_bounded_memory() -> None:
    conditions = LoggedConditions(limit=2)

    assert conditions.started("include", "missing") is True
    assert conditions.started("include", "missing") is False
    assert conditions.started("include", "unreadable") is True
    assert conditions.ended("include") is True
    # Only a logged condition has an end to log; after it, the start logs again.
    assert conditions.ended("include") is False
    assert conditions.started("include", "missing") is True

    # Beyond the limit the least recently seen condition is forgotten and logs again.
    assert conditions.started("second") is True
    assert conditions.started("include", "missing") is False
    assert conditions.started("third") is True
    assert conditions.started("include", "missing") is False
    assert conditions.started("second") is True
