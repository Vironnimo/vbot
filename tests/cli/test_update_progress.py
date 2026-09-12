"""Update progress is observable before work and final state is explicit."""

import pytest

from cli import update_management
from cli._output import print_update_command_result
from cli._update_types import UpdateResult, _Step
from cli.server_management import CommandResult, WebUIProbeResult
from tests.cli.update_management_test_support import _instance, _ok, _write_state


@pytest.mark.parametrize("mode", ["completed", "pending", "skipped", "not_applicable", "failed"])
def test_update_restart_state_and_progress(tmp_path, monkeypatch, mode):
    (tmp_path / ".git").mkdir()
    _write_state(tmp_path, shape="desktop-client" if mode == "not_applicable" else "server")
    progress = []
    calls = []

    def emit(status, message):
        progress.append((status, message))

    def runner(command, cwd):
        assert progress
        if command[:2] == ["git", "rev-parse"]:
            return _ok("samesha")
        if command[:2] == ["git", "symbolic-ref"]:
            return _ok("main")
        return _ok()

    def snapshot(instance):
        assert progress[-1][0] == "busy"
        return _Step(True, "test-owned snapshot")

    def restart(instance, **kwargs):
        assert progress[-1][0] == "busy"
        calls.append(instance)
        return CommandResult(ok=mode != "failed", message="test-owned restart", instance=instance)

    monkeypatch.setattr(update_management, "has_vbot_run_context", lambda: mode == "pending")
    monkeypatch.setattr(update_management, "restart_server", restart)
    monkeypatch.setattr(update_management, "schedule_server_restart", restart)
    result = update_management.run_update(
        _instance(),
        root=tmp_path,
        runner=runner,
        session_snapshot_fn=snapshot,
        platform_name="posix",
        progress=emit,
        restart=mode != "skipped",
    )
    assert isinstance(result, UpdateResult)
    assert result.restart_state == mode
    assert result.ok == (mode != "failed")
    assert len(calls) == (0 if mode in {"skipped", "not_applicable"} else 1)
    assert ("info", "test-owned snapshot") in progress


@pytest.mark.parametrize(
    "mode,status",
    [
        ("completed", "[OK]"),
        ("pending", "[WARN]"),
        ("skipped", "[WARN]"),
        ("not_applicable", "[OK]"),
        ("failed", "[ERROR]"),
    ],
)
def test_update_summary_does_not_repeat_details_or_hide_pending_work(mode, status, capsys):
    result = UpdateResult(
        ok=mode != "failed",
        message="test-owned delivered detail\ntest-owned final detail",
        instance=_instance(),
        restart_state=mode,
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


def test_healthy_server_with_unavailable_webui_is_visibly_a_warning(capsys):
    result = UpdateResult(
        ok=True,
        message="test-owned restart",
        instance=_instance(),
        restart_state="completed",
        webui=WebUIProbeResult(available=False),
    )
    print_update_command_result(result, version_before="1.0", version_after="2.0")
    output = capsys.readouterr().out
    assert "[WARN]" in output
    assert "[OK]" not in output
    assert "WebUI: unavailable" in output


def test_snapshot_failure_is_reported_before_any_checkout_mutation(tmp_path):
    (tmp_path / ".git").mkdir()
    _write_state(tmp_path)
    progress = []

    def runner(command, cwd):
        assert command[:2] in (["git", "symbolic-ref"], ["git", "rev-parse"])
        return _ok("main" if command[1] == "symbolic-ref" else "samesha")

    result = update_management.run_update(
        _instance(),
        root=tmp_path,
        runner=runner,
        platform_name="posix",
        session_snapshot_fn=lambda instance: _Step(False, "test-owned snapshot failure"),
        progress=lambda status, message: progress.append((status, message)),
    )
    assert not result.ok
    assert progress[-1] == ("error", "test-owned snapshot failure")
