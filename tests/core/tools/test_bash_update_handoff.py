"""Update handoff tokens: what a shell command may claim, and when it expires."""

import asyncio
import logging
import os
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import core.tools.bash as bash_module
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
from core.tools.bash import bash_handler
from core.tools.process_manager import ProcessManager
from tests.core.tools.bash_test_support import AGENT_ID, make_context, python_command
from tests.core.tools.bash_test_support import manager as manager
from tests.core.tools.bash_test_support import shell_env_cache as shell_env_cache


def _issue(handoffs: UpdateHandoffs):
    return handoffs.issue(
        run_id="run-one",
        tool_call_id="call-one",
        agent_id="main",
        project_id="project",
        session_id="session-one",
    )


def test_issued_token_writes_one_durable_ticket_once_claimed(tmp_path: Path) -> None:
    handoffs = UpdateHandoffs(tmp_path)
    grant = _issue(handoffs)

    assert not (tmp_path / "runtime").exists()

    ticket = handoffs.mint(grant.token)
    # A repeated claim returns the same ticket instead of writing another.
    assert handoffs.mint(grant.token).path == ticket.path

    assert grant.token not in ticket.path.name
    assert list((tmp_path / HANDOFF_DIRECTORY).iterdir()) == [ticket.path]
    assert ticket_id_from_path(tmp_path, ticket.path) == ticket.ticket_id
    stored = read_update_handoff_ticket(tmp_path, ticket.ticket_id)
    assert stored.acknowledged is False
    assert (stored.run_id, stored.tool_call_id, stored.agent_id) == ("run-one", "call-one", "main")
    assert (stored.project_id, stored.session_id) == ("project", "session-one")


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


def test_unknown_released_malformed_and_forgotten_tokens_are_unavailable(tmp_path: Path) -> None:
    handoffs = UpdateHandoffs(tmp_path)
    released = _issue(handoffs)
    released.release()
    # A new server process forgets every token the previous one issued.
    forgotten = _issue(UpdateHandoffs(tmp_path)).token

    for candidate in (released.token, forgotten, "unknown-token", "../outside", "x" * 500, ""):
        with pytest.raises(UpdateHandoffUnavailableError):
            handoffs.mint(candidate)
    assert not (tmp_path / "runtime").exists()
    with pytest.raises(ValueError):
        read_update_handoff_ticket(tmp_path, "../outside")


def test_minting_and_failed_acknowledgement_log_no_capability(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    handoffs = UpdateHandoffs(tmp_path)
    grant = _issue(handoffs)

    with caplog.at_level(logging.INFO, logger="vbot.tools.bash"):
        ticket = handoffs.mint(grant.token)
        handoffs.mint(grant.token)
        ticket.path.unlink()
        grant.acknowledge()

    # One line for the first claim, one warning for the lost acknowledgement.
    assert [record.levelno for record in caplog.records] == [logging.INFO, logging.WARNING]
    assert ticket.ticket_id not in caplog.text
    assert grant.token not in caplog.text
    assert ticket.path.name not in caplog.text


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

    with caplog.at_level(logging.DEBUG, logger="vbot.tools.bash"):
        UpdateHandoffs(tmp_path).remove_expired_files(now=now)

    assert not old_ticket.exists() and not old_receipt.exists()
    assert new_ticket.exists() and new_receipt.exists()
    # Routine maintenance: one DEBUG count summary that never names a file.
    [record] = caplog.records
    assert record.levelno == logging.DEBUG
    assert "tickets=1" in record.getMessage()
    assert "continuation_receipts=1" in record.getMessage()
    assert "old-ticket" not in caplog.text


def test_startup_sweep_is_silent_when_nothing_expired(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    now = time.time()
    _file(tmp_path / HANDOFF_DIRECTORY / "recent.json", age_seconds=60, now=now)

    with caplog.at_level(logging.DEBUG, logger="vbot.tools.bash"):
        UpdateHandoffs(tmp_path).remove_expired_files(now=now)
        UpdateHandoffs(tmp_path / "missing").remove_expired_files(now=now)

    assert caplog.records == []


# --- Tokens handed to shell commands ---------------------------------------

_PRINT_HANDOFF = "import os; print(os.environ.get('VBOT_UPDATE_HANDOFF', 'missing'), flush=True)"


@pytest.mark.asyncio
async def test_bash_exports_update_handoff_token_without_writing_a_ticket(
    manager: ProcessManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("VBOT_UPDATE_HANDOFF", raising=False)
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    data_dir = tmp_path / "data"
    handoffs = UpdateHandoffs(data_dir)
    persisted: list[Any] = []
    context = replace(make_context(tmp_path), result_persisted_hook=persisted.append)

    result = await bash_handler(
        context, {"command": _PRINT_HANDOFF}, manager, update_handoffs=handoffs
    )

    token = result["data"]["output"].strip()
    assert token not in {"", "missing"}
    assert len(persisted) == 1
    assert not (data_dir / "runtime").exists()
    # The foreground process has exited: nothing can claim its token any more.
    with pytest.raises(UpdateHandoffUnavailableError):
        handoffs.mint(token)

    # A call whose result is never persisted gets no token.
    unpersisted = await bash_handler(
        make_context(tmp_path), {"command": _PRINT_HANDOFF}, manager, update_handoffs=handoffs
    )
    assert unpersisted["data"]["output"].strip() == "missing"
    # Neither does a call whose Run ids cannot scope one; its command still runs.
    unscoped = await bash_handler(
        replace(context, session_id=""),
        {"command": _PRINT_HANDOFF},
        manager,
        update_handoffs=handoffs,
    )
    assert unscoped["data"]["output"].strip() == "missing"


@pytest.mark.asyncio
async def test_background_update_handoff_is_claimable_until_its_process_exits(
    manager: ProcessManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    handoffs = UpdateHandoffs(tmp_path / "data")
    persisted: list[Any] = []
    context = replace(make_context(tmp_path), result_persisted_hook=persisted.append)

    result = await bash_handler(
        context,
        {
            "command": (
                f"{_PRINT_HANDOFF}\nfrom pathlib import Path\nimport time\n"
                "while not Path('release').exists():\n    time.sleep(0.01)"
            ),
            "mode": "background",
        },
        manager,
        update_handoffs=handoffs,
    )
    process_id = result["data"]["process_id"]
    token = ""
    async with asyncio.timeout(5):
        while not token:
            token = str((await manager.snapshot(process_id, AGENT_ID))["output"]).strip()
            await asyncio.sleep(0.01)

    ticket = handoffs.mint(token)
    assert handoffs.mint(token).path == ticket.path
    assert read_handoff_ticket(tmp_path / "data", ticket.ticket_id)["acknowledged"] is False
    persisted[0]()
    assert read_handoff_ticket(tmp_path / "data", ticket.ticket_id)["acknowledged"] is True

    (tmp_path / "release").write_text("done", encoding="utf-8")
    wait_task = manager.get_process(process_id, AGENT_ID).wait_task
    assert wait_task is not None
    await asyncio.wait_for(asyncio.shield(wait_task), 5)
    await asyncio.sleep(0)
    with pytest.raises(UpdateHandoffUnavailableError):
        handoffs.mint(token)


@pytest.mark.asyncio
async def test_failed_spawn_releases_its_update_handoff(
    manager: ProcessManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handoffs = UpdateHandoffs(tmp_path / "data")
    offered: list[str] = []

    async def failing_spawn(*_args: Any, env: dict[str, str], **_kwargs: Any) -> str:
        offered.append(env["VBOT_UPDATE_HANDOFF"])
        raise OSError("spawn unavailable")

    monkeypatch.setattr(manager, "spawn", failing_spawn)
    context = replace(make_context(tmp_path), result_persisted_hook=lambda _callback: None)

    result = await bash_handler(
        context, {"command": "print('never')"}, manager, update_handoffs=handoffs
    )

    assert result["error"] == {
        "code": "process_spawn_failed",
        "message": "failed to start process: spawn unavailable",
    }
    with pytest.raises(UpdateHandoffUnavailableError):
        handoffs.mint(offered[0])
