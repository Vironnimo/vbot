"""Packaged application command dispatch keeps human and Agent update behavior distinct."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cli.application import command, operations, processes
from cli.application.state import ApplicationError, Installation, Operation
from cli.main import run
from cli.parser import parse_args
from cli.server_management import CommandResult, HealthProbeResult, WebUIProbeResult


def _install(root: Path) -> Installation:
    install = Installation(root, "server", "127.0.0.1", 8420, str((root / "data").resolve()))
    (root / "versions" / "rel_current").mkdir(parents=True)
    (root / "versions" / "rel_current" / "release.json").write_text(
        '{"version":"1.2.3"}', encoding="utf-8"
    )
    (root / "active-version").write_text("rel_current\n", encoding="ascii")
    return install


def test_human_packaged_update_waits_for_its_terminal_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install = _install(tmp_path)
    requested: list[tuple[bool, str | None]] = []
    waited: list[str] = []
    operation = Operation(id="upd_human", phase="queued")
    terminal = Operation(id="upd_human", phase="completed")

    def request(_install: Installation, **kwargs: object) -> Operation:
        token = kwargs["handoff_token"]
        assert token is None or isinstance(token, str)
        requested.append((bool(kwargs["restart"]), token))
        return operation

    def wait(_install: Installation, operation_id: str, **_kwargs: object) -> Operation:
        waited.append(operation_id)
        return terminal

    monkeypatch.setattr(command, "discover", lambda: install)
    # A packaged update can run inside an Agent's handoff child process, so the
    # ambient environment may carry the token this human path must ignore.
    monkeypatch.delenv("VBOT_UPDATE_HANDOFF", raising=False)
    monkeypatch.setattr(operations, "request_update", request)
    monkeypatch.setattr(operations, "wait", wait)

    assert command.dispatch(parse_args(["update"])) == 0
    assert requested == [(True, None)]
    assert waited == ["upd_human"]


def test_agent_handoff_update_returns_after_acceptance_without_waiting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install = _install(tmp_path)
    operation = Operation(id="upd_agent", phase="queued")
    captured: list[str | None] = []

    def request(_install: Installation, **kwargs: object) -> Operation:
        value = kwargs["handoff_token"]
        assert value is None or isinstance(value, str)
        captured.append(value)
        return operation

    monkeypatch.setattr(command, "discover", lambda: install)
    # The CLI forwards the opaque token; only the running server can claim it.
    monkeypatch.setenv("VBOT_UPDATE_HANDOFF", "opaque-token")
    monkeypatch.setattr(operations, "request_update", request)
    monkeypatch.setattr(operations, "wait", lambda *_args, **_kwargs: pytest.fail("must not wait"))

    assert command.dispatch(parse_args(["update"])) == 0
    assert captured == ["opaque-token"]


def test_tray_style_no_restart_update_does_not_forward_handoff_or_detach(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install = _install(tmp_path)
    operation = Operation(id="upd_tray", phase="queued")
    prepared = Operation(id="upd_tray", phase="prepared")
    captured: list[tuple[bool, str | None]] = []

    def request(_install: Installation, **kwargs: object) -> Operation:
        token = kwargs["handoff_token"]
        assert token is None or isinstance(token, str)
        captured.append((bool(kwargs["restart"]), token))
        return operation

    monkeypatch.setattr(command, "discover", lambda: install)
    monkeypatch.setenv("VBOT_UPDATE_HANDOFF", "opaque-token")
    monkeypatch.setattr(operations, "request_update", request)
    monkeypatch.setattr(operations, "wait", lambda *_args, **_kwargs: prepared)

    assert command.dispatch(parse_args(["update", "--no-restart"])) == 0
    assert captured == [(False, None)]


def test_source_checkout_update_dispatch_remains_unclaimed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(command, "discover", lambda: None)

    assert command.dispatch(parse_args(["update", "--no-restart"])) is None


def test_source_selection_records_mode_without_starting_or_updating(tmp_path, monkeypatch, capsys):
    install = _install(tmp_path)
    monkeypatch.setattr(command, "discover", lambda: install)
    selected = []

    def select(candidate, mode, *, from_checkout):
        selected.append((candidate.root, mode, from_checkout))
        return {"source_track": mode}

    monkeypatch.setattr("cli.application.source_updates.select_source", select)
    monkeypatch.setattr(operations, "request_update", lambda *a, **kw: pytest.fail("not an update"))
    assert run(["application", "source", "main", "--output", "plain"]) == 0
    assert selected == [(install.root, "main", None)]
    assert json.loads(capsys.readouterr().out)["next_command"] == "vbot update"


def test_source_selection_refuses_pending_update_before_mutation(tmp_path, monkeypatch):
    install = _install(tmp_path)
    Operation(id="upd_pending", phase="queued").save(install)
    monkeypatch.setattr(command, "discover", lambda: install)
    monkeypatch.setattr(
        "cli.application.source_updates.select_source",
        lambda *a, **kw: pytest.fail("must not change"),
    )
    with pytest.raises(ApplicationError):
        command.dispatch(parse_args(["application", "source", "release"]))


def test_packaged_lifecycle_refuses_a_target_other_than_its_recorded_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(command, "discover", lambda: _install(tmp_path))

    with pytest.raises(ApplicationError, match="recorded server"):
        command.dispatch(parse_args(["server", "start", "--host", "192.0.2.1"]))


@pytest.mark.parametrize(
    ("result_ok", "message", "health", "expected_exit", "expected_lines"),
    [
        (
            True,
            "running",
            HealthProbeResult(reachable=True, is_vbot=True, status_code=200),
            0,
            ("running: yes", "webui: available"),
        ),
        (
            True,
            "not running",
            HealthProbeResult(reachable=False, is_vbot=False),
            0,
            ("running: no", "webui: unavailable"),
        ),
        (
            False,
            "port occupied by non-vBot process",
            HealthProbeResult(reachable=True, is_vbot=False, status_code=200),
            0,
            ("running: no", "conflict: port occupied by non-vBot process"),
        ),
    ],
)
def test_packaged_server_status_uses_shared_output_and_exit_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    result_ok: bool,
    message: str,
    health: HealthProbeResult,
    expected_exit: int,
    expected_lines: tuple[str, ...],
) -> None:
    install = _install(tmp_path)
    instance = processes.target(install)
    result = CommandResult(
        ok=result_ok,
        message=message,
        instance=instance,
        health=health,
        webui=WebUIProbeResult(available=health.is_vbot),
        log_path=instance.log_path,
    )

    def fake_get_status(resolved_instance: object) -> CommandResult:
        assert resolved_instance == instance
        return result

    monkeypatch.setattr(command, "discover", lambda: install)
    monkeypatch.setattr("cli.server_management.get_status", fake_get_status)

    exit_code = command.dispatch(parse_args(["server", "status"]))

    assert exit_code == expected_exit
    output_lines = capsys.readouterr().out.splitlines()
    assert "command: server status" in output_lines
    assert f"url: {instance.url}" in output_lines
    assert f"data_dir: {instance.data_dir}" in output_lines
    assert f"log_path: {instance.log_path}" in output_lines
    for line in expected_lines:
        assert line in output_lines


@pytest.mark.parametrize(
    "phase, code",
    [("completed", 0), ("prepared", 0), ("failed", 1), ("rolled_back", 1), ("needs_attention", 1)],
)
def test_native_update_readable_output_uses_shared_status_markers(
    tmp_path, monkeypatch, capsys, phase, code
):
    install = _install(tmp_path)
    terminal = Operation(
        id="upd_output",
        phase=phase,
        previous_version="rel_current",
        candidate_version="rel_next",
        server_was_running=True,
        message="terminal-test-detail",
        error="test-error" if code else None,
    )
    monkeypatch.setattr(command, "discover", lambda: install)
    monkeypatch.delenv("VBOT_UPDATE_HANDOFF", raising=False)
    monkeypatch.setattr(operations, "request_update", lambda *a, **kw: Operation(id=terminal.id))
    monkeypatch.setattr(
        "cli.update_management.read_checkout_version",
        lambda path: "1.2.3" if "rel_current" in str(path) else "1.2.4",
    )

    def wait(_install, operation_id, *, progress):
        assert operation_id == terminal.id
        progress(Operation(id=terminal.id, phase="preparing", message="progress-test-detail"))
        progress(terminal)
        return terminal

    monkeypatch.setattr(operations, "wait", wait)
    assert run(["update"]) == code
    output = capsys.readouterr().out
    assert "[WORK]" in output
    assert output.count("progress-test-detail") == 1
    assert output.count("[OK]" if phase == "completed" else "[ERROR]" if code else "[INFO]") == 1
    assert '"operation_id"' not in output
    assert "preparing:" not in output and "completed:" not in output
    if phase in {"completed", "prepared"}:
        assert "1.2.4" in output
    if phase != "completed":
        assert "vbot update status upd_output" in output
    if phase == "prepared":
        assert "vbot update activate upd_output" in output
    if code:
        assert "test-error" in output


def test_native_update_plain_output_is_one_structured_result(tmp_path, monkeypatch, capsys):
    install = _install(tmp_path)
    terminal = Operation(id="upd_plain", phase="completed")
    monkeypatch.setattr(command, "discover", lambda: install)
    monkeypatch.delenv("VBOT_UPDATE_HANDOFF", raising=False)
    monkeypatch.setattr(operations, "request_update", lambda *a, **kw: terminal)

    def wait(_install, operation_id, **kwargs):
        assert not kwargs
        return terminal

    monkeypatch.setattr(operations, "wait", wait)
    assert run(["update", "--output", "plain"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == operations.public_result(terminal)
    assert not captured.err


def test_target_commit_is_shown_before_waiting_for_preparation(tmp_path, monkeypatch, capsys):
    install = _install(tmp_path)
    operation = Operation(
        id="upd_target",
        previous_label="1.2.3 (aaaaaaaa)",
        target_label="1.2.3 (bbbbbbbb)",
        phase="preparing",
        message="Building assets",
    )

    def wait(_install, operation_id, *, progress):
        progress(operation)
        output = capsys.readouterr().out
        assert output.index("aaaaaaaa") < output.index("bbbbbbbb") < output.index("Building assets")
        operation.phase = "completed"
        return operation

    monkeypatch.setattr(operations, "wait", wait)
    command._wait_update(install, operation)


def test_current_version_output_has_no_restart_or_reopen_instruction(tmp_path, capsys):
    install = _install(tmp_path)
    operation = Operation(
        id="upd_current",
        phase="completed",
        previous_version="rel_current",
        candidate_version="rel_current",
        target_label="1.2.3 (aaaaaaaa)",
    )
    command._print_update_result(install, operation)
    output = capsys.readouterr().out
    assert "already up to date" in output
    assert "1.2.3 (aaaaaaaa)" in output
    assert "->" not in output and "reopen" not in output and "server restarted" not in output


def test_detached_update_keeps_operation_handle_without_claiming_completion(
    tmp_path, monkeypatch, capsys
):
    install = _install(tmp_path)
    monkeypatch.setattr(command, "discover", lambda: install)
    monkeypatch.delenv("VBOT_UPDATE_HANDOFF", raising=False)
    monkeypatch.setattr(operations, "request_update", lambda *a, **kw: Operation(id="upd_detached"))
    monkeypatch.setattr(operations, "wait", lambda *a, **kw: pytest.fail("must not wait"))
    assert run(["update", "--detach"]) == 0
    output = capsys.readouterr().out
    assert "vbot update status upd_detached" in output
    assert "[INFO]" in output and "[OK]" not in output
    assert '"operation_id"' not in output


@pytest.mark.parametrize(
    "selection, confirm, expected",
    [
        ("1", "YES", "app-only"),
        ("2", "DELETE", "data-only"),
        ("3", "DELETE", "all"),
        ("4", "", None),
        ("1", "no", None),
    ],
)
def test_packaged_uninstall_reuses_guided_scope_and_confirmation(
    tmp_path, monkeypatch, selection, confirm, expected
):
    install = _install(tmp_path)
    monkeypatch.setattr(command, "discover", lambda: install)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    answers = iter((selection, confirm))
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    called = []

    def remove(_install, *, remove_data, data_only):
        called.append("data-only" if data_only else "all" if remove_data else "app-only")
        return {"data_removed": remove_data, "server_restarted": False}

    monkeypatch.setattr("cli.application.integration.uninstall", remove)
    assert run(["uninstall"]) == 0
    assert called == ([] if expected is None else [expected])
