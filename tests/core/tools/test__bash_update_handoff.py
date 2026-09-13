from pathlib import Path

import pytest

from core.tools._bash_update_handoff import (
    acknowledge_update_handoff_ticket,
    create_update_handoff_ticket,
    read_handoff_ticket,
    read_update_handoff_ticket,
    ticket_id_from_path,
)


def test_ticket_acknowledgement_is_durable_and_scoped(tmp_path: Path) -> None:
    ticket = create_update_handoff_ticket(
        tmp_path,
        run_id="run-one",
        tool_call_id="call-one",
        agent_id="main",
        project_id="project",
        session_id="session-one",
    )
    assert read_handoff_ticket(tmp_path, ticket.ticket_id)["acknowledged"] is False
    assert ticket_id_from_path(tmp_path, ticket.path) == ticket.ticket_id

    acknowledge_update_handoff_ticket(ticket)

    stored = read_update_handoff_ticket(tmp_path, ticket.ticket_id)
    assert stored.acknowledged is True
    assert stored.run_id == "run-one"


def test_ticket_reader_rejects_traversal(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        read_update_handoff_ticket(tmp_path, "../outside")
