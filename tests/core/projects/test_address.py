"""Tests for the single ``agent@project`` address parse/format seam."""

from __future__ import annotations

import pytest

from core.projects import (
    InvalidAgentAddressError,
    format_agent_address,
    parse_agent_address,
)


@pytest.mark.parametrize(
    ("address", "parts"),
    [
        # A bare Agent id is an Identity address, unchanged.
        ("orchestrator", ("orchestrator", None)),
        ("orchestrator@vbot", ("orchestrator", "vbot")),
    ],
)
def test_parse_and_format_are_inverse(address: str, parts: tuple[str, str | None]) -> None:
    assert parse_agent_address(address) == parts
    assert format_agent_address(*parts) == address


@pytest.mark.parametrize(
    "address",
    [
        "",
        "not a valid id!",
        "bad id@vbot",
        "orchestrator@bad project",
        "orchestrator@vbot@extra",
    ],
)
def test_parse_rejects_invalid_addresses(address: str) -> None:
    with pytest.raises(InvalidAgentAddressError):
        parse_agent_address(address)
