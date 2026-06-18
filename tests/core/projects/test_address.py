"""Tests for the single ``agent@projekt`` address parse/format seam.

Coverage (AAA):
- a bare agent id parses to ``(agent_id, None)`` (identity, unchanged),
- a qualified ``agent@projekt`` parses to both validated parts,
- an invalid agent or project part, an empty string, or extra ``@`` raise,
- format is the exact inverse: ``None`` → bare, set → ``agent@projekt``,
- parse∘format round-trips for both forms.
"""

from __future__ import annotations

import pytest

from core.projects import (
    InvalidAgentAddressError,
    format_agent_address,
    parse_agent_address,
)


def test_parse_bare_agent_is_identity() -> None:
    agent_id, project_id = parse_agent_address("orchestrator")

    assert agent_id == "orchestrator"
    assert project_id is None


def test_parse_qualified_address_splits_both_parts() -> None:
    agent_id, project_id = parse_agent_address("orchestrator@vbot")

    assert agent_id == "orchestrator"
    assert project_id == "vbot"


def test_parse_empty_string_raises() -> None:
    with pytest.raises(InvalidAgentAddressError):
        parse_agent_address("")


def test_parse_invalid_bare_agent_raises() -> None:
    with pytest.raises(InvalidAgentAddressError):
        parse_agent_address("not a valid id!")


def test_parse_invalid_agent_part_raises() -> None:
    with pytest.raises(InvalidAgentAddressError):
        parse_agent_address("bad id@vbot")


def test_parse_invalid_project_part_raises() -> None:
    with pytest.raises(InvalidAgentAddressError):
        parse_agent_address("orchestrator@bad project")


def test_parse_double_separator_raises() -> None:
    with pytest.raises(InvalidAgentAddressError):
        parse_agent_address("orchestrator@vbot@extra")


def test_parse_empty_agent_with_project_raises() -> None:
    with pytest.raises(InvalidAgentAddressError):
        parse_agent_address("@vbot")


def test_format_none_project_is_bare() -> None:
    assert format_agent_address("orchestrator", None) == "orchestrator"


def test_format_with_project_is_qualified() -> None:
    assert format_agent_address("orchestrator", "vbot") == "orchestrator@vbot"


def test_parse_format_round_trip_identity() -> None:
    agent_id, project_id = parse_agent_address("builder")

    assert format_agent_address(agent_id, project_id) == "builder"


def test_parse_format_round_trip_qualified() -> None:
    agent_id, project_id = parse_agent_address("builder@vbot")

    assert format_agent_address(agent_id, project_id) == "builder@vbot"
