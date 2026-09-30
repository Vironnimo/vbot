"""Update progress is observable before work and final state is explicit."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cli import update_management
from cli._output import print_update_command_result
from cli._update_types import UpdateResult, _Step
from cli.server_management import CommandResult, ServerInstance, WebUIProbeResult
from cli.update_management import CommandRun
from tests.cli.update_management_test_support import (
    _instance,
    _ok,
    _upstream,
    _write_state,
    checkout,
)


@pytest.fixture(autouse=True)
def no_running_launchers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the Windows launcher guards away from this machine's real processes."""

    monkeypatch.setattr(update_management, "_running_process_id", lambda *_args, **_kw: None)


@pytest.mark.parametrize(
    ("mode", "restarted_by"),
    [
        ("completed", "restart"),
        # A Run's own server cannot restart inline: a detached helper does it later.
        ("pending", "schedule"),
        ("skipped", None),
        ("not_applicable", None),
        ("failed", "restart"),
    ],
)
def test_update_restart_state_and_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, restarted_by: str | None
) -> None:
    (tmp_path / ".git").mkdir()
    _write_state(tmp_path, shape="desktop-client" if mode == "not_applicable" else "server")
    progress: list[tuple[str, str]] = []
    calls: list[tuple[str, ServerInstance, str]] = []
    handle = checkout(upstream=_upstream(behind=1))

    def runner(command: list[str], cwd: Path) -> CommandRun:
        assert progress, "progress is announced before the first command"
        return handle(command)

    def snapshot(instance: ServerInstance) -> _Step:
        assert progress[-1][0] == "busy"
        return _Step(True, "test-owned snapshot")

    def recorder(label: str) -> Any:
        def restart(instance: ServerInstance, *, service_name: str, **_kw: Any) -> CommandResult:
            assert progress[-1][0] == "busy"
            calls.append((label, instance, service_name))
            return CommandResult(
                ok=mode != "failed", message="test-owned restart", instance=instance
            )

        return restart

    monkeypatch.setattr(update_management, "has_vbot_run_context", lambda: mode == "pending")
    monkeypatch.setattr(update_management, "restart_server", recorder("restart"))
    monkeypatch.setattr(update_management, "schedule_server_restart", recorder("schedule"))

    result = update_management.run_update(
        _instance(),
        root=tmp_path,
        runner=runner,
        data_snapshot_fn=snapshot,
        platform_name="posix",
        progress=lambda status, message: progress.append((status, message)),
        restart=mode != "skipped",
    )

    assert isinstance(result, UpdateResult)
    assert result.restart_state == mode
    assert result.ok == (mode != "failed")
    assert calls == ([] if restarted_by is None else [(restarted_by, _instance(), "vbot")])
    assert ("info", "test-owned snapshot") in progress


@pytest.mark.parametrize(
    ("mode", "status"),
    [
        ("completed", "[OK]"),
        ("pending", "[WARN]"),
        ("skipped", "[WARN]"),
        ("unchanged", "[OK]"),
        ("not_applicable", "[OK]"),
        ("failed", "[ERROR]"),
    ],
)
def test_update_summary_does_not_repeat_details_or_hide_pending_work(
    mode: str, status: str, capsys: pytest.CaptureFixture[str]
) -> None:
    result = UpdateResult(
        ok=mode != "failed",
        message="test-owned delivered detail\ntest-owned final detail",
        instance=_instance(),
        restart_state=mode,  # type: ignore[arg-type]
    )
    print_update_command_result(
        result,
        version_before="1.0",
        version_after="2.0",
        shown_messages={"test-owned delivered detail"},
    )
    output = capsys.readouterr().out
    assert "test-owned delivered detail" not in output
    assert "test-owned final detail" in output
    assert status in output
    assert "1.0" in output and "2.0" in output
    assert "\033[" not in output


@pytest.mark.parametrize("problem", ["webui", "attention"])
def test_healthy_server_with_a_remaining_problem_is_visibly_a_warning(
    problem: str, capsys: pytest.CaptureFixture[str]
) -> None:
    result = UpdateResult(
        ok=True,
        message="test-owned restart",
        instance=_instance(),
        restart_state="completed",
        webui=WebUIProbeResult(available=problem != "webui"),
        attention=("test-owned attention",) if problem == "attention" else (),
    )
    print_update_command_result(result, version_before="1.0", version_after="2.0")
    output = capsys.readouterr().out
    assert "[WARN]" in output
    assert "[OK]" not in output
    assert ("WebUI: unavailable" in output) is (problem == "webui")
    assert ("test-owned attention" in output) is (problem == "attention")


def test_snapshot_failure_is_reported_before_any_checkout_mutation(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    _write_state(tmp_path)
    progress: list[tuple[str, str]] = []
    read_only = {
        "symbolic-ref": _ok("main"),
        "rev-parse": _ok("samesha"),
        "status": _ok(""),
        "fetch": _ok(""),
        "rev-list": _upstream(behind=1),
    }

    def runner(command: list[str], cwd: Path) -> CommandRun:
        assert command[0] == "git" and command[1] in read_only, command
        return read_only[command[1]]

    result = update_management.run_update(
        _instance(),
        root=tmp_path,
        runner=runner,
        platform_name="posix",
        data_snapshot_fn=lambda instance: _Step(False, "test-owned snapshot failure"),
        progress=lambda status, message: progress.append((status, message)),
    )
    assert not result.ok
    assert progress[-1] == ("error", "test-owned snapshot failure")
