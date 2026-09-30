"""Desktop update restart: relaunch contract, successor request and the handoff."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

from desktop import restart
from desktop.restart import (
    DesktopRestart,
    RelaunchContract,
    RestartError,
    RestartRequest,
    WindowPlacement,
)

COMMAND = ("C:/vBot/vBot.GUI.exe", "desktop")


def _contract(tmp_path: Path, active: str = "v1") -> RelaunchContract:
    version_file = tmp_path / "active-version"
    version_file.write_text(f"{active}\n", encoding="ascii")
    return RelaunchContract(version_file, "v1", COMMAND)


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        ({"version_file": "C:/vBot/active-version", "version": "v1", "command": COMMAND}, True),
        ({"version_file": "active-version", "version": "v1", "command": COMMAND}, False),
        ({"version_file": "C:/vBot/active-version", "version": "../v1", "command": COMMAND}, False),
        ({"version_file": "C:/vBot/active-version", "version": "v1", "command": []}, False),
        ({"version_file": "C:/vBot/active-version", "version": "v1", "command": ["x"]}, False),
        ("not json", False),
    ],
    ids=["valid", "relative-file", "unsafe-version", "no-command", "relative-command", "garbage"],
)
def test_only_a_complete_relaunch_contract_enables_restarts(value: Any, valid: bool) -> None:
    raw = value if isinstance(value, str) else json.dumps(value)

    contract = restart.relaunch_contract({restart.RELAUNCH_ENV: raw})

    assert (contract is not None) is valid
    if contract is not None:
        assert contract.version == "v1"
        assert contract.command == COMMAND
    assert restart.relaunch_contract({}) is None


def test_the_successor_takes_the_request_once_and_drops_invalid_parts(tmp_path: Path) -> None:
    request = RestartRequest(
        nonce="a" * 32,
        server=("pi.lan", 9000),
        location="#settings/desktop",
        placement=WindowPlacement("normal", 1200, 800, 40, 60),
    )
    restart.write_restart_request(tmp_path, request, created_at=1000.0)

    assert restart.take_restart_request(tmp_path, now=1100.0) == request
    assert restart.take_restart_request(tmp_path, now=1100.0) is None

    path = tmp_path / restart.RESTART_REQUEST_FILE_NAME
    path.write_text(
        json.dumps(
            {
                "created_at": 1000.0,
                "nonce": "b" * 32,
                "server": {"host": "pi.lan", "port": 0},
                "location": "#has space",
                "window": {"state": "minimized", "width": 900, "height": 700, "x": -32000},
            }
        ),
        encoding="utf-8",
    )
    assert restart.take_restart_request(tmp_path, now=1000.0) == RestartRequest(
        nonce="b" * 32, placement=WindowPlacement("minimized", 900, 700)
    )


@pytest.mark.parametrize(
    "document",
    [
        {"created_at": 1000.0},
        {"created_at": 1000.0, "nonce": "short"},
        {"created_at": 700.0, "nonce": "c" * 32},
        {"created_at": True, "nonce": "c" * 32},
    ],
    ids=["no-nonce", "bad-nonce", "stale", "bad-timestamp"],
)
def test_a_stale_or_malformed_request_starts_no_handoff(
    tmp_path: Path, document: dict[str, Any]
) -> None:
    path = tmp_path / restart.RESTART_REQUEST_FILE_NAME
    path.write_text(json.dumps(document), encoding="utf-8")

    assert restart.take_restart_request(tmp_path, now=1000.0) is None
    assert not path.exists()


class FakePage:
    def __init__(self, *, handles_restart: bool = False) -> None:
        self.updates: list[dict[str, Any]] = []
        self.restart_requests = 0
        self.handles_restart = handles_restart

    def publish_update(self, status: Mapping[str, Any]) -> None:
        self.updates.append(dict(status))

    def request_restart(self, on_result: Callable[[bool], None]) -> None:
        self.restart_requests += 1
        on_result(self.handles_restart)


class FakeProcess:
    def __init__(self, code: int | None = None) -> None:
        self.code = code

    def poll(self) -> int | None:
        return self.code


class Harness:
    """A restart wired to fakes: a controllable clock, window and relaunch."""

    def __init__(self, tmp_path: Path, *, page: FakePage | None = None) -> None:
        self.tmp_path = tmp_path
        self.config = tmp_path / "config"
        self.contract = _contract(tmp_path)
        self.page = page or FakePage()
        self.now = 1000.0
        self.foreground = False
        self.busy = False
        self.closed = threading.Event()
        self.spawned: list[tuple[str, ...]] = []
        self.process = FakeProcess()
        self.request_on_spawn: dict[str, Any] | None = None
        self.restart = DesktopRestart(
            self.contract,
            config_directory=self.config,
            page=self.page,
            active_server=lambda: ("pi.lan", 9000),
            location=lambda: "#chat",
            placement=lambda: WindowPlacement("normal", 1200, 800, 10, 20),
            foreground=lambda: self.foreground,
            shell_busy=lambda: self.busy,
            close_window=self.closed.set,
            spawn=self._spawn,
            clock=lambda: self.now,
            wall_clock=lambda: 5000.0,
        )

    def _spawn(self, command: tuple[str, ...]) -> FakeProcess:
        self.spawned.append(command)
        path = self.config / restart.RESTART_REQUEST_FILE_NAME
        self.request_on_spawn = json.loads(path.read_text(encoding="utf-8"))
        return self.process

    def activate(self, version: str) -> None:
        self.contract.version_file.write_text(f"{version}\n", encoding="ascii")

    def wait_for_status(self, **expected: bool) -> None:
        for _ in range(400):
            if self.page.updates and all(
                self.page.updates[-1][key] is value for key, value in expected.items()
            ):
                return
            threading.Event().wait(0.01)
        raise AssertionError(f"status never became {expected}: {self.page.updates}")


def test_a_newer_active_version_makes_the_desktop_pending(tmp_path: Path) -> None:
    harness = Harness(tmp_path)

    harness.restart.check()
    assert harness.restart.status() == {"pending": False, "restarting": False, "failed": False}
    with pytest.raises(RestartError) as raised:
        harness.restart.restart()
    assert raised.value.error_code == "no_update_pending"

    harness.activate("v2")
    harness.restart.check()

    assert harness.restart.status()["pending"] is True
    assert harness.page.updates == [{"pending": True, "restarting": False, "failed": False}]


def test_a_requested_restart_closes_the_window_only_after_the_successor_reports(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    harness.activate("v2")
    harness.restart.check()

    harness.restart.restart()
    harness.wait_for_status(restarting=True)
    for _ in range(400):
        if harness.spawned:
            break
        threading.Event().wait(0.01)

    assert harness.spawned == [COMMAND]
    request = harness.request_on_spawn
    assert request is not None
    assert request["server"] == {"host": "pi.lan", "port": 9000}
    assert request["location"] == "#chat"
    assert request["window"] == {"state": "normal", "width": 1200, "height": 800, "x": 10, "y": 20}
    assert not harness.closed.is_set()
    assert harness.restart.accept_handoff("not-the-nonce") is False
    assert harness.restart.accept_handoff(request["nonce"]) is True
    assert harness.closed.wait(2)


class ExpiringProcess(FakeProcess):
    """A relaunch that keeps running while the handoff deadline passes."""

    def __init__(self, harness: Harness) -> None:
        super().__init__()
        self.harness = harness

    def poll(self) -> int | None:
        self.harness.now += restart.HANDOFF_TIMEOUT_SECONDS
        return None


@pytest.mark.parametrize("failure", ["exit-code", "timeout"])
def test_a_failed_handoff_keeps_the_window_and_reports_the_failure(
    tmp_path: Path, failure: str
) -> None:
    harness = Harness(tmp_path)
    harness.activate("v2")
    harness.restart.check()
    harness.process = FakeProcess(1) if failure == "exit-code" else ExpiringProcess(harness)

    harness.restart.restart()
    harness.wait_for_status(restarting=False, failed=True)

    assert not harness.closed.is_set()
    assert not (harness.config / restart.RESTART_REQUEST_FILE_NAME).exists()
    assert harness.restart.status() == {"pending": True, "restarting": False, "failed": True}


@pytest.mark.parametrize(
    ("foreground", "busy", "page_handles", "requested", "restarted"),
    [
        (False, False, False, True, True),
        (False, False, True, True, False),
        (True, False, False, False, False),
        (False, True, False, False, False),
    ],
    ids=["idle-unhandled-page", "page-takes-it", "window-in-front", "voice-busy"],
)
def test_an_idle_desktop_restarts_when_neither_user_voice_nor_page_objects(
    tmp_path: Path,
    foreground: bool,
    busy: bool,
    page_handles: bool,
    requested: bool,
    restarted: bool,
) -> None:
    harness = Harness(tmp_path, page=FakePage(handles_restart=page_handles))
    harness.activate("v2")
    harness.restart.check()
    harness.foreground, harness.busy = foreground, busy

    harness.now += restart.IDLE_AFTER_SECONDS - 1
    harness.restart.check()
    assert harness.page.restart_requests == 0

    harness.now += 1
    harness.restart.check()

    assert harness.page.restart_requests == (1 if requested else 0)
    if restarted:
        harness.wait_for_status(restarting=True)
    else:
        assert harness.restart.status()["restarting"] is False
    # A page that took the request is not asked again right away.
    harness.restart.check()
    assert harness.page.restart_requests == (1 if requested else 0)
