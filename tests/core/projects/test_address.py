"""Tests for the single ``agent@projekt`` address parse/format seam."""

from __future__ import annotations

import re

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
    ("address", "message"),
    [
        ("", "agent address must be a non-empty string"),
        ("not a valid id!", "invalid agent id: 'not a valid id!'"),
        ("bad id@vbot", "invalid agent id in address 'bad id@vbot': 'bad id'"),
        (
            "orchestrator@bad project",
            "invalid project id in address 'orchestrator@bad project': 'bad project'",
        ),
        (
            "orchestrator@vbot@extra",
            "agent address must be 'agent' or 'agent@projekt', got: 'orchestrator@vbot@extra'",
        ),
    ],
)
def test_parse_rejects_invalid_addresses(address: str, message: str) -> None:
    with pytest.raises(InvalidAgentAddressError, match=re.escape(message)):
        parse_agent_address(address)
