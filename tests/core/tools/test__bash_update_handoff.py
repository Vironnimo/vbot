import logging
import os
import time
from pathlib import Path

import pytest

from core.tools._bash_update_handoff import (
    CONTINUATION_DIRECTORY,
    HANDOFF_DIRECTORY,
    UPDATE_HANDOFF_FILE_RETENTION,
    UpdateHandoffs,
    UpdateHandoffUnavailableError,
    read_handoff_ticket,
    read_update_handoff_ticket,
    ticket_id_from_path,
)


def _issue(handoffs: UpdateHandoffs):
    return handoffs.issue(
        run_id="run-one",
        tool_call_id="call-one",
        agent_id="main",
        project_id="project",
        session_id="session-one",
    )


def test_issued_token_writes_nothing_until_claimed(tmp_path: Path) -> None:
    handoffs = UpdateHandoffs(tmp_path)
    grant = _issue(handoffs)

    assert not (tmp_path / "runtime").exists()

    ticket = handoffs.mint(grant.token)

    assert grant.token not in ticket.path.name
    assert list((tmp_path / HANDOFF_DIRECTORY).iterdir()) == [ticket.path]
    assert ticket_id_from_path(tmp_path, ticket.path) == ticket.ticket_id
    stored = read_update_handoff_ticket(tmp_path, ticket.ticket_id)
    assert stored.acknowledged is False
    assert (stored.run_id, stored.tool_call_id, stored.agent_id) == ("run-one", "call-one", "main")
    assert (stored.project_id, stored.session_id) == ("project", "session-one")


def test_repeated_claim_returns_the_same_durable_ticket(tmp_path: Path) -> None:
    handoffs = UpdateHandoffs(tmp_path)
    grant = _issue(handoffs)

    first = handoffs.mint(grant.token)
    second = handoffs.mint(grant.token)

    assert second.path == first.path
    assert len(list((tmp_path / HANDOFF_DIRECTORY).iterdir())) == 1


def test_persisted_result_acknowledges_a_ticket_claimed_before_or_after(tmp_path: Path) -> None:
    handoffs = UpdateHandoffs(tmp_path)
    claimed_first = _issue(handoffs)
    ticket = handoffs.mint(claimed_first.token)
    # The foreground command finished before its Tool Result was persisted.
    claimed_first.release()

    claimed_first.acknowledge()

    assert read_handoff_ticket(tmp_path, ticket.ticket_id)["acknowledged"] is True

    acknowledged_first = _issue(handoffs)
    acknowledged_first.acknowledge()
    later = handoffs.mint(acknowledged_first.token)

    assert read_handoff_ticket(tmp_path, later.ticket_id)["acknowledged"] is True


def test_unknown_released_and_malformed_tokens_are_unavailable(tmp_path: Path) -> None:
    handoffs = UpdateHandoffs(tmp_path)
    grant = _issue(handoffs)
    token = grant.token
    grant.release()

    for candidate in (token, "unknown-token", "../outside", "x" * 500, ""):
        with pytest.raises(UpdateHandoffUnavailableError):
            handoffs.mint(candidate)
    assert not (tmp_path / "runtime").exists()


def test_a_new_server_process_forgets_every_token(tmp_path: Path) -> None:
    grant = _issue(UpdateHandoffs(tmp_path))

    with pytest.raises(UpdateHandoffUnavailableError):
        UpdateHandoffs(tmp_path).mint(grant.token)


def test_failed_acknowledgement_logs_no_capability(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    handoffs = UpdateHandoffs(tmp_path)
    grant = _issue(handoffs)
    ticket = handoffs.mint(grant.token)
    ticket.path.unlink()

    with caplog.at_level(logging.WARNING, logger="vbot.tools.bash"):
        grant.acknowledge()

    assert [record.levelno for record in caplog.records] == [logging.WARNING]
    assert ticket.ticket_id not in caplog.text
    assert grant.token not in caplog.text


def test_ticket_reader_rejects_traversal(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        read_update_handoff_ticket(tmp_path, "../outside")


def _file(path: Path, *, age_seconds: float, now: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")
    os.utime(path, (now - age_seconds, now - age_seconds))
    return path


def test_startup_sweep_removes_only_expired_files_and_logs_counts(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    now = time.time()
    expired = UPDATE_HANDOFF_FILE_RETENTION.total_seconds() + 60
    recent = UPDATE_HANDOFF_FILE_RETENTION.total_seconds() - 60
    old_ticket = _file(
        tmp_path / HANDOFF_DIRECTORY / "old-ticket.json", age_seconds=expired, now=now
    )
    new_ticket = _file(
        tmp_path / HANDOFF_DIRECTORY / "new-ticket.json", age_seconds=recent, now=now
    )
    old_receipt = _file(
        tmp_path / CONTINUATION_DIRECTORY / "upd_old.json", age_seconds=expired, now=now
    )
    new_receipt = _file(
        tmp_path / CONTINUATION_DIRECTORY / "upd_new.json", age_seconds=recent, now=now
    )

    with caplog.at_level(logging.INFO, logger="vbot.tools.bash"):
        UpdateHandoffs(tmp_path).remove_expired_files(now=now)

    assert not old_ticket.exists() and not old_receipt.exists()
    assert new_ticket.exists() and new_receipt.exists()
    assert [record.getMessage() for record in caplog.records] == [
        "Removed expired update handoff files: tickets=1 continuation_receipts=1"
    ]
    assert "old-ticket" not in caplog.text


def test_startup_sweep_is_silent_when_nothing_expired(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    now = time.time()
    _file(tmp_path / HANDOFF_DIRECTORY / "recent.json", age_seconds=60, now=now)

    with caplog.at_level(logging.INFO, logger="vbot.tools.bash"):
        UpdateHandoffs(tmp_path).remove_expired_files(now=now)
        UpdateHandoffs(tmp_path / "missing").remove_expired_files(now=now)

    assert caplog.records == []
