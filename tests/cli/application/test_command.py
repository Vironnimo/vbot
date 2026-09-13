"""Packaged application command dispatch keeps human and Agent update behavior distinct."""

from __future__ import annotations

from pathlib import Path

import pytest

from cli.application import command, operations
from cli.application.state import ApplicationError, Installation, Operation
from cli.parser import parse_args


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


def test_packaged_lifecycle_refuses_a_target_other_than_its_recorded_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(command, "discover", lambda: _install(tmp_path))

    with pytest.raises(ApplicationError, match="recorded server"):
        command.dispatch(parse_args(["server", "start", "--host", "192.0.2.1"]))
