"""Desktop update restart: relaunch contract, successor request and the handoff."""

from __future__ import annotations

import json
import math
import os
import threading
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any, override

import pytest

from desktop import restart
from desktop.restart import (
    DesktopRestart,
    RelaunchContract,
    RestartError,
    RestartRequest,
    WindowPlacement,
)

# A native absolute installation root: a drive path on Windows, a POSIX path elsewhere.
ROOT = Path(Path.cwd().anchor, "vBot")
VERSION_FILE = str(ROOT / "active-version")
COMMAND = (str(ROOT / "vBot.GUI.exe"), "desktop")


def _contract(tmp_path: Path, active: str = "v1") -> RelaunchContract:
    version_file = tmp_path / "active-version"
    version_file.write_text(f"{active}\n", encoding="ascii")
    return RelaunchContract(version_file, "v1", COMMAND)


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        ({"version_file": VERSION_FILE, "version": "v1", "command": COMMAND}, True),
        ({"version_file": "active-version", "version": "v1", "command": COMMAND}, False),
        ({"version_file": VERSION_FILE, "version": "../v1", "command": COMMAND}, False),
        ({"version_file": VERSION_FILE, "version": "v1", "command": []}, False),
        ({"version_file": VERSION_FILE, "version": "v1", "command": ["x"]}, False),
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
    """A page that takes restart requests (like the WebUI) unless told otherwise.

    ``then`` runs after a taken request, as the page's own ``restartDesktop``.
    """

    def __init__(self, *, handles_restart: bool = True) -> None:
        self.updates: list[dict[str, Any]] = []
        self.restart_requests = 0
        self.handles_restart = handles_restart
        self.then: Callable[[], None] | None = None

    def publish_update(self, status: Mapping[str, Any]) -> None:
        self.updates.append(dict(status))

    def request_restart(self, on_result: Callable[[bool], None]) -> None:
        self.restart_requests += 1
        on_result(self.handles_restart)
        if self.handles_restart and self.then is not None:
            self.then()


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
        # Voice records or calibrates until then.
        self.busy_until = 0.0
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
            shell_busy=lambda: self.now < self.busy_until,
            close_window=self.closed.set,
            spawn=self._spawn,
            clock=lambda: self.now,
            wall_clock=lambda: 5000.0,
        )

    def _spawn(self, command: tuple[str, ...]) -> FakeProcess:
        path = self.config / restart.RESTART_REQUEST_FILE_NAME
        self.request_on_spawn = json.loads(path.read_text(encoding="utf-8"))
        # Tests poll `spawned` from another thread, then read the request.
        self.spawned.append(command)
        return self.process

    def activate(self, version: str) -> None:
        path = self.contract.version_file
        previous = path.stat().st_mtime_ns
        path.write_text(f"{version}\n", encoding="ascii")
        # The watcher compares modification time and size; a same-size rewrite
        # within one file-time tick would otherwise look unchanged.
        changed = previous + 1_000_000_000
        os.utime(path, ns=(changed, changed))

    def wait_for_status(self, **expected: bool) -> None:
        for _ in range(400):
            if self.page.updates and all(
                self.page.updates[-1][key] is value for key, value in expected.items()
            ):
                return
            threading.Event().wait(0.01)
        raise AssertionError(f"status never became {expected}: {self.page.updates}")


@pytest.fixture
def harnesses(tmp_path: Path) -> Iterator[Callable[..., Harness]]:
    """Create harnesses whose handoffs still waiting end with the test."""

    created: list[Harness] = []

    def create(**kwargs: Any) -> Harness:
        harness = Harness(tmp_path, **kwargs)
        created.append(harness)
        return harness

    yield create
    for harness in created:
        harness.restart.close()


def test_a_newer_active_version_makes_the_desktop_pending(
    harnesses: Callable[..., Harness],
) -> None:
    harness = harnesses()

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
    harnesses: Callable[..., Harness],
) -> None:
    harness = harnesses()
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

    @override
    def poll(self) -> int | None:
        self.harness.now += restart.HANDOFF_TIMEOUT_SECONDS
        return None


@pytest.mark.parametrize("failure", ["exit-code", "timeout"])
def test_a_failed_handoff_keeps_the_window_and_retries_after_a_growing_delay(
    harnesses: Callable[..., Harness], failure: str
) -> None:
    harness = harnesses()
    harness.activate("v2")
    harness.restart.check()
    harness.process = FakeProcess(1) if failure == "exit-code" else ExpiringProcess(harness)

    for delay in (restart.FAILED_RETRY_SECONDS, 2 * restart.FAILED_RETRY_SECONDS):
        harness.restart.restart()
        harness.wait_for_status(restarting=False, failed=True)

        assert not harness.closed.is_set()
        assert not (harness.config / restart.RESTART_REQUEST_FILE_NAME).exists()
        assert harness.restart.status() == {"pending": True, "restarting": False, "failed": True}
        asked = harness.page.restart_requests
        harness.now += delay - 1
        harness.restart.check()
        assert harness.page.restart_requests == asked
        harness.now += 1
        harness.restart.check()
        assert harness.page.restart_requests == asked + 1


@pytest.mark.parametrize(
    ("handles", "restarts_itself", "restarts_after"),
    [
        (False, False, 0.0),
        (True, True, 0.0),
        (True, False, restart.RESTART_GRACE_SECONDS),
    ],
    ids=["unhandled-request", "page-restarts", "page-does-not-restart"],
)
def test_a_pending_desktop_asks_the_page_once_and_restarts_right_away(
    harnesses: Callable[..., Harness],
    handles: bool,
    restarts_itself: bool,
    restarts_after: float,
) -> None:
    harness = harnesses(page=FakePage(handles_restart=handles))
    if restarts_itself:
        harness.page.then = harness.restart.restart
    harness.activate("v2")

    harness.restart.check()
    assert harness.page.restart_requests == 1
    if restarts_after:
        harness.now += restarts_after - 1
        harness.restart.check()
        assert harness.restart.status()["restarting"] is False
        harness.now += 1
        harness.restart.check()

    assert harness.restart.status()["restarting"] is True
    assert harness.page.restart_requests == 1


@pytest.mark.parametrize(
    ("busy_for", "asked_after"),
    [(5.0, 5.0), (math.inf, restart.RESTART_GRACE_SECONDS)],
    ids=["voice-finishes", "voice-outlasts-the-grace"],
)
def test_voice_in_use_delays_the_restart_only_until_it_ends_or_the_grace_runs_out(
    harnesses: Callable[..., Harness], busy_for: float, asked_after: float
) -> None:
    harness = harnesses()
    harness.busy_until = harness.now + busy_for
    harness.activate("v2")

    harness.restart.check()
    assert harness.restart.status()["pending"] is True
    harness.now += asked_after - 1
    harness.restart.check()
    assert harness.page.restart_requests == 0

    harness.now += 1
    harness.restart.check()

    assert harness.page.restart_requests == 1
