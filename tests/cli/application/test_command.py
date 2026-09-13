"""Packaged application command dispatch keeps human and Agent update behavior distinct."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cli.application import command, operations, processes
from cli.application.state import ApplicationError, Installation, Operation
from cli.parser import parse_args
from cli.server_management import CommandResult, HealthProbeResult, WebUIProbeResult


def _install(root: Path) -> Installation:
    install = Installation(root, "server", "127.0.0.1", 8420, str((root / "data").resolve()))
    (root / "versions" / "rel_current").mkdir(parents=True)
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
        ticket = kwargs["handoff_ticket"]
        assert ticket is None or isinstance(ticket, str)
        requested.append((bool(kwargs["restart"]), ticket))
        return operation

    def wait(_install: Installation, operation_id: str, **_kwargs: object) -> Operation:
        waited.append(operation_id)
        return terminal

    monkeypatch.setattr(command, "discover", lambda: install)
    monkeypatch.setattr(operations, "request_update", request)
    monkeypatch.setattr(operations, "wait", wait)

    assert command.dispatch(parse_args(["update"])) == 0
    assert requested == [(True, None)]
    assert waited == ["upd_human"]


def test_agent_handoff_update_returns_after_acceptance_without_waiting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install = _install(tmp_path)
    ticket = install.root / "data" / "handoff.json"
    ticket.parent.mkdir(parents=True)
    operation = Operation(id="upd_agent", phase="queued")
    captured: list[str | None] = []

    def request(_install: Installation, **kwargs: object) -> Operation:
        value = kwargs["handoff_ticket"]
        assert value is None or isinstance(value, str)
        captured.append(value)
        return operation

    monkeypatch.setattr(command, "discover", lambda: install)
    monkeypatch.setenv("VBOT_UPDATE_HANDOFF", str(ticket))
    monkeypatch.setattr(
        "core.tools._bash_update_handoff.read_handoff_ticket", lambda *_args: {"acknowledged": True}
    )
    monkeypatch.setattr(operations, "request_update", request)
    monkeypatch.setattr(operations, "wait", lambda *_args, **_kwargs: pytest.fail("must not wait"))

    assert command.dispatch(parse_args(["update"])) == 0
    assert captured == [str(ticket)]


def test_tray_style_no_restart_update_does_not_forward_handoff_or_detach(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install = _install(tmp_path)
    operation = Operation(id="upd_tray", phase="queued")
    prepared = Operation(id="upd_tray", phase="prepared")
    captured: list[tuple[bool, str | None]] = []

    def request(_install: Installation, **kwargs: object) -> Operation:
        ticket = kwargs["handoff_ticket"]
        assert ticket is None or isinstance(ticket, str)
        captured.append((bool(kwargs["restart"]), ticket))
        return operation

    monkeypatch.setattr(command, "discover", lambda: install)
    monkeypatch.setenv("VBOT_UPDATE_HANDOFF", str(tmp_path / "handoff.json"))
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
    assert command.dispatch(parse_args(["application", "source", "main"])) == 0
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
