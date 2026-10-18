"""Tests for the Desktop in-window server selection and sign-in (connection module)."""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from desktop import connection as desktop_connection
from desktop import servers as desktop_servers
from desktop.access import ServerAccess
from desktop.connection import (
    LAUNCH_WAIT_SECONDS,
    SIGN_IN_LANDING_SECONDS,
    ConnectionController,
)
from desktop.connection_types import SIGN_IN_PAGE
from desktop.credentials import DeviceTokenStore
from desktop.probe import (
    ACCESS_MODE_AUTHENTICATED,
    DEFAULT_PORT,
    PROBE_INVALID_TARGET,
    PROBE_NOT_VBOT_SERVER,
    PROBE_SERVER_UNREACHABLE,
    PROBE_WEBUI_AVAILABLE,
    PROBE_WEBUI_UNAVAILABLE,
    DesktopProbeResult,
    DesktopTarget,
)
from desktop.server_address import ServerAddress
from desktop.servers import ServerEntry
from desktop.window import SessionLink
from tests.desktop.access_test_support import (
    DEVICE_NAME,
    WINDOW_NAME,
    FakeAccessServer,
    build_access,
    token_store,
)
from tests.desktop.server_test_support import CLOUD_ENTRY, _settings, _stored

_TEST_DESKTOP_SESSION_ID = "desktop-test-session"
PI_URL = f"http://pi.lan:9000/?accessor=desktop&desktop_session={_TEST_DESKTOP_SESSION_ID}"


class _FixedUuid:
    hex = _TEST_DESKTOP_SESSION_ID


@pytest.fixture(autouse=True)
def _use_stable_desktop_session_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep navigation expectations deterministic outside the UUID-specific tests."""

    monkeypatch.setattr(desktop_connection, "uuid4", lambda: _FixedUuid())


class FakeWindow:
    """Records the navigation calls the controller makes on the live window.

    ``current_url`` follows the loads like a browser; a test sets it to where a
    redirect or the page's own script took the window.
    """

    def __init__(self) -> None:
        self.loaded_urls: list[str] = []
        self.loaded_html: list[str] = []
        self.current_url: str | None = None
        self._loaded = threading.Condition()

    def load_url(self, url: str) -> None:
        with self._loaded:
            self.loaded_urls.append(url)
            self.current_url = url
            self._loaded.notify_all()

    def load_html(self, content: str) -> None:
        with self._loaded:
            self.loaded_html.append(content)
            self.current_url = None
            self._loaded.notify_all()

    def get_current_url(self) -> str | None:
        return self.current_url

    def wait_for_loads(self, count: int) -> None:
        """Wait until the controller (or its background thread) loaded ``count`` documents."""

        with self._loaded:
            assert self._loaded.wait_for(
                lambda: len(self.loaded_urls) + len(self.loaded_html) >= count, timeout=5
            )


def probe_returning(
    status: str, access_mode: str | None = None
) -> Callable[[DesktopTarget], DesktopProbeResult]:
    """Build a probe stub that classifies every target with a fixed status."""

    def _probe(target: DesktopTarget) -> DesktopProbeResult:
        return DesktopProbeResult(status=status, target=target, access_mode=access_mode)

    return _probe


class ScriptedProbe:
    """Answers each target from its script and lets one second pass per attempt.

    The last status of a script repeats. ``meanwhile`` runs at the start of the
    given attempt (counted over all targets), as if from another thread.
    """

    def __init__(
        self,
        scripts: dict[tuple[str, int], list[str]],
        *,
        seconds_per_attempt: float = 1.0,
    ) -> None:
        self.now = 0.0
        self.scripts = {target: list(script) for target, script in scripts.items()}
        self.seconds_per_attempt = seconds_per_attempt
        self.probed: list[tuple[str, int]] = []
        self.meanwhile: dict[int, Callable[[], None]] = {}

    def clock(self) -> float:
        return self.now

    def __call__(self, target: DesktopTarget) -> DesktopProbeResult:
        self.probed.append((target.host, target.port))
        action = self.meanwhile.pop(len(self.probed), None)
        if action is not None:
            action()
        self.now += self.seconds_per_attempt
        script = self.scripts[(target.host, target.port)]
        status = script.pop(0) if len(script) > 1 else script[0]
        return DesktopProbeResult(status=status, target=target)


def _waiting_state(page: str) -> str | None:
    """Return the connection screen's waiting state markup, if it shows one."""

    match = re.search(r'<div id="connection-waiting".*?</div>', page, re.DOTALL)
    return match.group(0) if match else None


def _connection_log_levels(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.levelname for record in caplog.records if record.name == "vbot.desktop.connection"
    ]


def _controller(
    tmp_path: Path,
    status: str | None = PROBE_WEBUI_AVAILABLE,
    *,
    saved: dict[str, Any] | None = None,
) -> tuple[ConnectionController, FakeWindow, Path]:
    """A controller over a fake window; ``status=None`` keeps the real probe."""

    settings_file = _settings(tmp_path, saved)
    window = FakeWindow()
    if status is None:
        controller = ConnectionController(settings_file=settings_file, window=window)
    else:
        controller = ConnectionController(
            settings_file=settings_file, window=window, probe=probe_returning(status)
        )
    return controller, window, settings_file


def _scripted_controller(
    tmp_path: Path, probe: ScriptedProbe
) -> tuple[ConnectionController, FakeWindow, Path]:
    """A controller whose probe and clock come from ``probe``: no attempt really waits."""

    settings_file = _settings(tmp_path)
    window = FakeWindow()
    controller = ConnectionController(
        settings_file=settings_file, window=window, probe=probe, clock=probe.clock
    )
    return controller, window, settings_file


# -- Remembered servers ------------------------------------------------------------


# -- Controller: launch connect ------------------------------------------------------

PI = ("pi.lan", 9000)
NAS = ("nas.lan", 8420)


def test_a_successful_connect_navigates_remembers_and_announces_the_server(
    tmp_path: Path,
) -> None:
    controller, window, settings_file = _controller(tmp_path)
    urls: list[str] = []
    controller.set_active_server_listener(urls.append)

    result = controller.launch_connect("pi.lan", 9000, "Pi")

    assert result.status == PROBE_WEBUI_AVAILABLE
    assert window.loaded_urls == [PI_URL]
    assert window.loaded_html == []
    stored = _stored(settings_file)
    assert stored["servers"] == [{"host": "pi.lan", "port": 9000, "label": "Pi"}]
    assert stored["last_used"] == {"host": "pi.lan", "port": 9000}
    # The listener (Voice) receives the plain base URL, not the navigation URL.
    assert urls == ["http://pi.lan:9000/"]
    assert controller.active_server_url() == "http://pi.lan:9000/"


@pytest.mark.parametrize(
    "script",
    [
        [PROBE_WEBUI_UNAVAILABLE],
        [PROBE_NOT_VBOT_SERVER],
        [PROBE_SERVER_UNREACHABLE, PROBE_WEBUI_UNAVAILABLE],
    ],
    ids=["webui-unavailable", "not-vbot", "webui-unavailable-while-waiting"],
)
def test_a_launch_connect_shows_any_other_failure_inline_at_once_and_changes_nothing(
    tmp_path: Path, script: list[str]
) -> None:
    # The server would answer later; only an unreachable one is waited for.
    probe = ScriptedProbe({PI: [*script, PROBE_WEBUI_AVAILABLE]})
    controller, window, settings_file = _scripted_controller(tmp_path, probe)
    urls: list[str] = []
    controller.set_active_server_listener(urls.append)

    result = controller.launch_connect("pi.lan", 9000)
    window.wait_for_loads(len(script))
    controller.close()

    assert result.status == script[0]
    assert probe.probed == [PI] * len(script)
    assert window.loaded_urls == []
    page = window.loaded_html[-1]
    assert 'role="alert"' in page
    assert _waiting_state(page) is None
    # Failed host/port are prefilled so the user fixes the target in place.
    assert 'value="pi.lan"' in page
    assert 'value="9000"' in page
    assert desktop_servers.list_servers(settings_file) == []
    assert urls == []
    assert controller.active_server_url() is None


def test_a_launch_connect_waits_for_an_unreachable_server_and_connects_like_a_first_try(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    probe = ScriptedProbe(
        {PI: [PROBE_SERVER_UNREACHABLE, PROBE_SERVER_UNREACHABLE, PROBE_WEBUI_AVAILABLE]}
    )
    controller, window, settings_file = _scripted_controller(tmp_path, probe)
    urls: list[str] = []
    controller.set_active_server_listener(urls.append)

    with caplog.at_level("INFO", logger="vbot.desktop.connection"):
        result = controller.launch_connect(
            "pi.lan",
            9000,
            "Pi",
            open_session=SessionLink(agent="builder@project", session="session-1"),
            location="#settings/desktop",
        )
        window.wait_for_loads(2)
    controller.close()

    assert result.status == PROBE_SERVER_UNREACHABLE
    [waiting_page] = window.loaded_html
    waiting = _waiting_state(waiting_page)
    assert waiting is not None and 'role="status"' in waiting and "pi.lan:9000" in waiting
    assert 'role="alert"' not in waiting_page
    assert probe.probed == [PI] * 3
    assert window.loaded_urls == [
        f"{PI_URL}&open_agent=builder%40project&open_session=session-1#settings/desktop"
    ]
    stored = _stored(settings_file)
    assert stored["servers"] == [{"host": "pi.lan", "port": 9000, "label": "Pi"}]
    assert stored["last_used"] == {"host": "pi.lan", "port": 9000}
    assert urls == ["http://pi.lan:9000/"]
    assert controller.active_target() == PI
    # One line when the wait starts and one when it ends, none per attempt.
    assert _connection_log_levels(caplog) == ["INFO", "INFO"]


def test_a_launch_connect_shows_the_unreachable_error_once_the_wait_runs_out(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    probe = ScriptedProbe(
        {PI: [PROBE_SERVER_UNREACHABLE]}, seconds_per_attempt=LAUNCH_WAIT_SECONDS / 4
    )
    controller, window, settings_file = _scripted_controller(tmp_path, probe)
    urls: list[str] = []
    controller.set_active_server_listener(urls.append)

    with caplog.at_level("INFO", logger="vbot.desktop.connection"):
        controller.launch_connect("pi.lan", 9000)
        window.wait_for_loads(2)
    controller.close()

    # Attempts at 0, 15, 30 and 45 s; the one ending at 60 s gives up.
    assert probe.probed == [PI] * 4
    waiting_page, error_page = window.loaded_html
    assert _waiting_state(waiting_page) is not None
    assert _waiting_state(error_page) is None
    assert 'role="alert"' in error_page
    assert 'value="pi.lan"' in error_page
    assert window.loaded_urls == []
    assert desktop_servers.list_servers(settings_file) == []
    assert urls == []
    assert _connection_log_levels(caplog) == ["INFO", "WARNING"]


@pytest.mark.parametrize("user_status", [PROBE_WEBUI_AVAILABLE, PROBE_SERVER_UNREACHABLE])
def test_a_user_connect_ends_a_launch_wait_and_is_never_overridden(
    tmp_path: Path, user_status: str
) -> None:
    # The launch target answers on the attempt during which the user connects.
    probe = ScriptedProbe(
        {PI: [PROBE_SERVER_UNREACHABLE, PROBE_WEBUI_AVAILABLE], NAS: [user_status]}
    )
    controller, window, settings_file = _scripted_controller(tmp_path, probe)
    urls: list[str] = []
    controller.set_active_server_listener(urls.append)
    user_choices: list[str] = []
    user_connected = threading.Event()

    def user_connects() -> None:
        user_choices.append(controller.prepare_connect("nas.lan", 8420).result.status)
        user_connected.set()

    probe.meanwhile[2] = user_connects

    controller.launch_connect("pi.lan", 9000)
    assert user_connected.wait(5)
    controller.close()

    # The user's own connect tried once and never waited.
    assert probe.probed == [PI, PI, NAS]
    assert user_choices == [user_status]
    # Only the waiting state was ever loaded; the user's page applies their outcome.
    [waiting_page] = window.loaded_html
    assert _waiting_state(waiting_page) is not None
    assert window.loaded_urls == []
    connected = user_status == PROBE_WEBUI_AVAILABLE
    assert urls == (["http://nas.lan:8420/"] if connected else [])
    assert controller.active_target() == (NAS if connected else None)
    stored_last_used = desktop_servers.resolve_last_used(settings_file)
    assert stored_last_used == (ServerEntry(*NAS) if connected else None)


def test_an_invalid_host_renders_the_invalid_target_screen_without_io(tmp_path: Path) -> None:
    controller, window, _ = _controller(tmp_path, status=None)

    result = controller.launch_connect("https://pi.lan/app", 9000)

    assert result.status == PROBE_INVALID_TARGET
    assert window.loaded_urls == []
    assert 'role="alert"' in window.loaded_html[0]
    assert 'value="https://pi.lan/app"' in window.loaded_html[0]


def test_connect_survives_active_server_listener_error(tmp_path: Path) -> None:
    controller, window, _ = _controller(tmp_path)

    def boom(_url: str) -> None:
        raise RuntimeError("worker rebuild failed")

    controller.set_active_server_listener(boom)

    # A failing listener must never break the navigation that already succeeded.
    assert controller.launch_connect("pi.lan", 9000).status == PROBE_WEBUI_AVAILABLE
    assert window.loaded_urls == [PI_URL]


def test_a_controller_navigates_only_once_a_window_is_attached(tmp_path: Path) -> None:
    controller = ConnectionController(
        settings_file=tmp_path / "settings.json", probe=probe_returning(PROBE_WEBUI_AVAILABLE)
    )
    with pytest.raises(RuntimeError):
        controller.launch_connect("pi.lan", 9000)

    window = FakeWindow()
    controller.attach_window(window)
    controller.launch_connect("pi.lan", 9000)

    assert window.loaded_urls == [PI_URL]


# -- Controller: bridge-safe connect ---------------------------------------------------


def test_prepare_connect_returns_url_without_replacing_calling_document(tmp_path: Path) -> None:
    controller, window, settings_file = _controller(tmp_path)

    prepared = controller.prepare_connect("pi.lan", 9000, "Pi")

    assert prepared.to_bridge_payload() == {"status": PROBE_WEBUI_AVAILABLE, "url": PI_URL}
    assert window.loaded_urls == []
    assert window.loaded_html == []
    assert desktop_servers.resolve_last_used(settings_file) == ServerEntry("pi.lan", 9000, "Pi")
    # Choosing the saved server again (the connection screen passes no label) keeps its name.
    controller.prepare_connect("pi.lan", 9000)
    assert controller.list_servers() == [ServerEntry("pi.lan", 9000, "Pi")]


def test_prepare_connect_returns_inline_error_without_replacing_calling_document(
    tmp_path: Path,
) -> None:
    controller, window, _ = _controller(tmp_path, PROBE_SERVER_UNREACHABLE)

    payload = controller.prepare_connect("pi.lan", 9000).to_bridge_payload()

    assert payload["status"] == PROBE_SERVER_UNREACHABLE
    assert set(payload) == {"status", "error_title", "error_body"}
    assert all(
        isinstance(payload[field], str) and payload[field]
        for field in ("error_title", "error_body")
    )
    assert window.loaded_urls == []
    assert window.loaded_html == []


def test_prepare_connect_invalid_port_returns_bridge_error_instead_of_raising(
    tmp_path: Path,
) -> None:
    controller, _, _ = _controller(tmp_path, status=None)

    prepared = controller.prepare_connect("pi.lan", "not-a-port")  # type: ignore[arg-type]

    assert prepared.result.status == PROBE_INVALID_TARGET
    assert prepared.error_title
    assert prepared.navigation_url is None


def test_active_server_url_follows_successful_connections_only(tmp_path: Path) -> None:
    results = [PROBE_WEBUI_AVAILABLE, PROBE_SERVER_UNREACHABLE]
    controller = ConnectionController(
        settings_file=tmp_path / "settings.json",
        window=FakeWindow(),
        probe=lambda target: DesktopProbeResult(status=results.pop(0), target=target),
    )

    controller.prepare_connect("pi.lan", 9000)
    controller.prepare_connect("nas.lan", 8420)

    # A failed attempt shows the connection screen; the last served origin stays.
    assert controller.active_server_url() == "http://pi.lan:9000/"


def test_each_controller_uses_a_new_webui_document_cache_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Uuid:
        def __init__(self, value: str) -> None:
            self.hex = value

    session_ids = iter(("first-launch", "second-launch"))
    monkeypatch.setattr(desktop_connection, "uuid4", lambda: _Uuid(next(session_ids)))

    first = ConnectionController(
        settings_file=tmp_path / "first.json", probe=probe_returning(PROBE_WEBUI_AVAILABLE)
    )
    second = ConnectionController(
        settings_file=tmp_path / "second.json", probe=probe_returning(PROBE_WEBUI_AVAILABLE)
    )

    first_url = first.prepare_connect("pi.lan", 9000).navigation_url
    second_url = second.prepare_connect("pi.lan", 9000).navigation_url

    assert first_url == "http://pi.lan:9000/?accessor=desktop&desktop_session=first-launch"
    assert second_url == "http://pi.lan:9000/?accessor=desktop&desktop_session=second-launch"


# -- Controller: launch auto-connect -------------------------------------------------


def test_auto_connect_reconnects_to_the_last_used_target(tmp_path: Path) -> None:
    saved = {
        "servers": [
            {"host": "other.lan", "port": 8420},
            {"host": "pi.lan", "port": 9000, "label": "Pi"},
        ],
        "last_used": {"host": "pi.lan", "port": 9000},
    }
    settings_file = _settings(tmp_path, saved)
    window = FakeWindow()
    probed: list[DesktopTarget] = []

    def probe(target: DesktopTarget) -> DesktopProbeResult:
        probed.append(target)
        return DesktopProbeResult(status=PROBE_WEBUI_AVAILABLE, target=target)

    controller = ConnectionController(settings_file=settings_file, window=window, probe=probe)

    result = controller.auto_connect()

    assert result is not None
    assert result.status == PROBE_WEBUI_AVAILABLE
    assert [(target.host, target.port) for target in probed] == [("pi.lan", 9000)]
    assert window.loaded_urls == [PI_URL]


def test_first_run_auto_connect_shows_the_connection_screen_without_an_error(
    tmp_path: Path,
) -> None:
    controller, window, _ = _controller(tmp_path)

    assert controller.auto_connect() is None
    assert window.loaded_urls == []
    [page] = window.loaded_html
    # No probe ran: no error banner, and the default suggestion is only a prefill.
    assert 'role="alert"' not in page
    assert _waiting_state(page) is None
    assert '<ul class="servers">' not in page
    assert "data-host=" not in page
    assert 'value="127.0.0.1"' in page
    assert f'value="{DEFAULT_PORT}"' in page


# -- Controller: sign-in -------------------------------------------------------------

CLOUD = ServerAddress("https", "vbot.example.com", 443)
CLOUD_HOST = CLOUD.bridge_host
CLOUD_APP_PATH = f"/?accessor=desktop&desktop_session={_TEST_DESKTOP_SESSION_ID}"
CLOUD_APP_URL = f"https://vbot.example.com{CLOUD_APP_PATH}"
TICKET_URL = re.compile(r"https://vbot\.example\.com/auth/ticket/[A-Za-z0-9_-]+(#.*)?")
# The connection screen offering to pair with the cloud server.
CLOUD_OFFER = (
    '<div id="connection-offer" class="pairing" role="status" '
    'data-host="https://vbot.example.com" data-port="443">'
)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class SignInRig:
    """A controller over a fake window, signing in to ``server`` in the access mode
    ``authenticated``; pairing polls every 10 ms."""

    def __init__(self, tmp_path: Path, server: FakeAccessServer, access_mode: str | None) -> None:
        self.server = server
        self.store: DeviceTokenStore = token_store(tmp_path)
        self.access: ServerAccess = build_access(
            server, tmp_path, store=self.store, wait=lambda event, _seconds: event.wait(0.01)
        )
        self.settings_file = _settings(tmp_path)
        self.window = FakeWindow()
        self.clock = Clock()
        self.controller = ConnectionController(
            settings_file=self.settings_file,
            window=self.window,
            probe=probe_returning(PROBE_WEBUI_AVAILABLE, access_mode),
            access=self.access,
            clock=self.clock,
        )

    def pair_earlier(self) -> None:
        device = self.server.add_device()
        self.store.save_device(CLOUD.origin, device.token, device.id)

    def ticket_redirected(self, landing: str | None = None) -> None:
        """The window redeemed the ticket it was sent to and followed the redirect.

        The server redirects to the ticket's ``next`` (or to ``landing``), and
        the fragment of the ticket URL stays with the redirect.
        """
        url = self.window.loaded_urls[-1]
        assert TICKET_URL.fullmatch(url)
        path, _, fragment = url.removeprefix("https://vbot.example.com").partition("#")
        self.server.redeem(path)
        redirect = self.server.redirects[-1] if landing is None else landing
        self.window.current_url = f"https://vbot.example.com{redirect}" + (
            f"#{fragment}" if fragment else ""
        )
        self.controller.page_loaded()

    def pairing_code(self) -> str:
        [pairing] = self.server.pairings.values()
        return pairing.code


@pytest.fixture
def rig(tmp_path: Path) -> SignInRig:
    return SignInRig(tmp_path, FakeAccessServer(), ACCESS_MODE_AUTHENTICATED)


def _eventually(condition: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 5
    while not condition():
        assert time.monotonic() < deadline, "condition not reached"
        time.sleep(0.005)


def test_a_server_without_sign_in_opens_directly(tmp_path: Path) -> None:
    rig = SignInRig(tmp_path, FakeAccessServer(), access_mode="lan")

    rig.controller.launch_connect(CLOUD_HOST, None)

    assert rig.window.loaded_urls == [CLOUD_APP_URL]
    assert rig.server.requests == []


def test_a_launch_connect_signs_the_window_in_with_the_stored_token(rig: SignInRig) -> None:
    rig.pair_earlier()
    urls: list[str] = []
    rig.controller.set_active_server_listener(urls.append)

    rig.controller.launch_connect(CLOUD_HOST, None, "Cloud")

    # The window opens a login ticket first.
    [ticket_url] = rig.window.loaded_urls
    assert TICKET_URL.fullmatch(ticket_url)
    stored = _stored(rig.settings_file)
    assert stored["servers"] == [{**CLOUD_ENTRY, "label": "Cloud"}]
    assert stored["last_used"] == CLOUD_ENTRY
    assert urls == ["https://vbot.example.com/"]

    rig.ticket_redirected()

    # The ticket itself lands the window on the WebUI with the Desktop's parameters.
    assert rig.server.calls("auth.login_ticket") == [{"name": WINDOW_NAME, "next": CLOUD_APP_PATH}]
    assert rig.window.current_url == CLOUD_APP_URL
    assert rig.window.loaded_urls == [ticket_url]
    # Later pages are the WebUI's own business.
    rig.controller.page_loaded()
    assert rig.window.loaded_urls == [ticket_url]


@pytest.mark.parametrize("server_lands", [True, False], ids=["on-the-page", "elsewhere"])
def test_a_signed_in_window_opens_the_session_and_place_it_was_asked_for(
    rig: SignInRig, server_lands: bool
) -> None:
    rig.pair_earlier()
    link = SessionLink(agent="coder@site", session="ses_1")
    page = f"{CLOUD_APP_PATH}&open_agent=coder%40site&open_session=ses_1"

    rig.controller.launch_connect(CLOUD_HOST, None, open_session=link, location="#settings")
    [ticket_url] = rig.window.loaded_urls
    rig.ticket_redirected(None if server_lands else "/")

    assert ticket_url.endswith("#settings")
    assert rig.server.calls("auth.login_ticket") == [{"name": WINDOW_NAME, "next": page}]
    # A window the server sent elsewhere opens the page itself.
    expected = [ticket_url] if server_lands else [ticket_url, f"{CLOUD.url[:-1]}{page}#settings"]
    assert rig.window.loaded_urls == expected


def test_a_connect_the_page_starts_never_hands_the_ticket_to_the_page(rig: SignInRig) -> None:
    rig.pair_earlier()
    rig.window.current_url = "https://other.example.com/"

    payload = rig.controller.prepare_connect(CLOUD_HOST, 443, on_screen=True).to_bridge_payload()

    assert payload == {"status": PROBE_WEBUI_AVAILABLE, "url": SIGN_IN_PAGE}
    assert rig.window.loaded_urls == []
    # The calling page itself loading again changes nothing.
    rig.controller.page_loaded()
    assert rig.window.loaded_urls == []

    # The page went to the sign-in page: the window opens the ticket, then the WebUI.
    rig.window.current_url = SIGN_IN_PAGE
    rig.controller.page_loaded()
    assert len(rig.window.loaded_urls) == 1
    rig.ticket_redirected()
    assert rig.window.current_url == CLOUD_APP_URL
    assert len(rig.window.loaded_urls) == 1


def test_a_sign_in_that_does_not_land_in_time_is_dropped(rig: SignInRig) -> None:
    rig.pair_earlier()
    rig.controller.prepare_connect(CLOUD_HOST, 443)

    rig.clock.now += SIGN_IN_LANDING_SECONDS + 1
    rig.window.current_url = SIGN_IN_PAGE
    rig.controller.page_loaded()

    assert rig.window.loaded_urls == []


def test_a_ticket_the_server_refuses_shows_the_sign_in_failure(rig: SignInRig) -> None:
    rig.pair_earlier()
    rig.controller.launch_connect(CLOUD_HOST, None)

    # The server answered the ticket with its error page: the window stays there.
    rig.controller.page_loaded()

    [page] = rig.window.loaded_html
    assert "Sign-in failed" in page and 'role="alert"' in page
    assert len(rig.window.loaded_urls) == 1


@pytest.mark.parametrize(
    ("answer", "title"),
    [
        (
            httpx.Response(403, json={"ok": False, "error": {"code": "access_denied"}}),
            "Access denied",
        ),
        (
            httpx.Response(
                429,
                headers={"Retry-After": "60"},
                json={"ok": False, "error": {"code": "rate_limited"}},
            ),
            "Too many attempts",
        ),
    ],
    ids=["access-denied", "rate-limited"],
)
def test_a_sign_in_the_server_refuses_shows_why(
    rig: SignInRig, answer: httpx.Response, title: str
) -> None:
    rig.pair_earlier()
    rig.server.scripted["/api/rpc"] = [answer]

    payload = rig.controller.prepare_connect(CLOUD_HOST, 443).to_bridge_payload()

    assert (payload["status"], payload["error_title"]) == (answer.json()["error"]["code"], title)
    assert "url" not in payload
    # The server answered, so it is remembered; it is not the window's server.
    assert desktop_servers.list_servers(rig.settings_file) == [
        ServerEntry("vbot.example.com", 443, None, "https")
    ]
    assert rig.controller.active_server_url() is None


def test_a_launch_connect_without_a_sign_in_offers_pairing_and_connects_once_approved(
    rig: SignInRig,
) -> None:
    rig.controller.launch_connect(CLOUD_HOST, None, "Cloud", location="#settings")

    # The screen offers pairing under the default name; the server hears nothing yet.
    [page] = rig.window.loaded_html
    assert CLOUD_OFFER in page
    assert f'value="{DEVICE_NAME}"' in page
    assert rig.server.pairings == {}
    assert rig.window.loaded_urls == []

    # The user asks for a code; the approval connects to where the launch was going.
    rig.controller.start_pairing(CLOUD_HOST, 443)
    [pairing] = rig.server.pairings.values()
    assert (pairing.kind, pairing.name) == ("desktop", DEVICE_NAME)
    rig.server.decide(pairing.code, "approved")
    rig.window.wait_for_loads(2)
    rig.controller.close()

    assert rig.access.signed_in(CLOUD)
    [ticket_url] = rig.window.loaded_urls
    assert TICKET_URL.fullmatch(ticket_url)
    assert ticket_url.endswith("#settings")
    assert _stored(rig.settings_file)["servers"] == [{**CLOUD_ENTRY, "label": "Cloud"}]
    rig.ticket_redirected()
    assert rig.window.current_url == f"{CLOUD_APP_URL}#settings"


@pytest.mark.parametrize(
    ("decision", "title"), [("denied", "Sign-in denied"), ("expired", "Code expired")]
)
def test_a_pairing_that_is_not_approved_shows_why(
    rig: SignInRig, decision: str, title: str
) -> None:
    rig.server.pairing_outcome = decision

    rig.controller.launch_connect(CLOUD_HOST, None)
    rig.controller.start_pairing(CLOUD_HOST, 443)
    rig.window.wait_for_loads(2)
    rig.controller.close()

    offer_page, failure_page = rig.window.loaded_html
    assert 'role="alert"' not in offer_page
    assert f'<h2 id="connection-error-title">{title}</h2>' in failure_page
    assert rig.window.loaded_urls == []
    assert not rig.access.signed_in(CLOUD)


def test_the_connection_screen_pairs_under_the_name_the_user_chose(rig: SignInRig) -> None:
    payload = rig.controller.prepare_connect(CLOUD_HOST, 443, on_screen=True).to_bridge_payload()

    assert payload["status"] == "pairing_required"
    assert payload["offer"]["origin"] == "https://vbot.example.com"
    assert payload["offer"]["name"] == DEVICE_NAME
    assert "https://vbot.example.com" in payload["error_body"]
    assert "url" not in payload and "pairing" not in payload
    assert rig.server.pairings == {}

    payload = rig.controller.start_pairing(CLOUD_HOST, 443, " Work laptop ").to_bridge_payload()

    [pairing] = rig.server.pairings.values()
    assert pairing.name == "Work laptop"
    assert payload["pairing"]["code"] == pairing.code
    assert payload["pairing"]["origin"] == "https://vbot.example.com"
    assert "only in vBot at https://vbot.example.com" in payload["error_body"]
    assert rig.window.loaded_urls == rig.window.loaded_html == []

    rig.server.decide(pairing.code, "approved")
    rig.window.wait_for_loads(1)
    rig.controller.close()

    assert TICKET_URL.fullmatch(rig.window.loaded_urls[0])


def test_a_connect_a_page_starts_offers_pairing_only_on_the_connection_screen(
    rig: SignInRig,
) -> None:
    rig.window.current_url = "https://other.example.com/"

    payload = rig.controller.prepare_connect(CLOUD_HOST, 443).to_bridge_payload()

    # The page is sent to the sign-in page; it never sees an offer, a code or a ticket.
    assert (payload["status"], payload["url"]) == ("pairing_required", SIGN_IN_PAGE)
    assert "offer" not in payload and "pairing" not in payload
    rig.controller.page_loaded()
    assert rig.window.loaded_html == []
    # Once there, the window shows the connection screen's offer; no pairing started.
    rig.window.current_url = SIGN_IN_PAGE
    rig.controller.page_loaded()
    [page] = rig.window.loaded_html
    assert CLOUD_OFFER in page
    assert rig.server.pairings == {}
    rig.controller.page_loaded()
    assert len(rig.window.loaded_html) == 1


def test_a_cancelled_pairing_leaves_the_window_alone(rig: SignInRig) -> None:
    rig.controller.launch_connect(CLOUD_HOST, None)
    rig.controller.start_pairing(CLOUD_HOST, 443)

    assert rig.controller.cancel_pairing() is True
    assert rig.controller.cancel_pairing() is False
    rig.controller.close()
    polls = rig.server.requests.copy()
    rig.server.decide(rig.pairing_code(), "approved")
    time.sleep(0.05)

    assert rig.server.requests == polls
    assert len(rig.window.loaded_html) == 1
    assert rig.window.loaded_urls == []


def test_a_lost_sign_in_offers_pairing_in_the_window_once(
    rig: SignInRig, caplog: pytest.LogCaptureFixture
) -> None:
    rig.pair_earlier()
    rig.controller.launch_connect(CLOUD_HOST, None)
    rig.ticket_redirected()
    # Another server's refusal is not the window's concern.
    rig.access.rejected("https://elsewhere.example.com/", None)

    with caplog.at_level(logging.INFO, logger="vbot.desktop.connection"):
        # A native request (Voice, dictation) is refused: the server revoked this Desktop.
        assert rig.access.rejected(CLOUD.url, rig.access.header(CLOUD.url)) is False
        rig.window.wait_for_loads(2)
        # Further refusals do not connect the window again.
        rig.access.rejected(CLOUD.url, None)
    rig.controller.close()

    [page] = rig.window.loaded_html
    assert CLOUD_OFFER in page
    assert rig.server.pairings == {}
    reconnects = [
        record for record in caplog.records if "connecting the window again" in record.message
    ]
    assert len(reconnects) == 1


def test_forgetting_a_sign_in_signs_out_without_pairing_again(rig: SignInRig) -> None:
    rig.pair_earlier()
    rig.controller.launch_connect(CLOUD_HOST, None, "Cloud")
    rig.ticket_redirected()
    assert [entry["signed_in"] for entry in rig.controller.describe_servers()] == [True]

    assert rig.controller.forget_sign_in(CLOUD_HOST, 443) is True

    assert rig.controller.describe_servers() == [
        {
            "host": CLOUD_HOST,
            "port": 443,
            "scheme": "https",
            "label": "Cloud",
            "active": True,
            "signed_in": False,
        }
    ]
    # The server's refusals that follow do not start a pairing in the window.
    rig.access.rejected(CLOUD.url, None)
    _eventually(lambda: rig.server.calls("devices.logout") != [])
    rig.controller.close()
    assert rig.window.loaded_html == []
    assert rig.server.pairings == {}


def test_removing_a_server_forgets_its_sign_in(rig: SignInRig) -> None:
    rig.pair_earlier()
    rig.controller.add_server(CLOUD_HOST, 443)

    assert rig.controller.remove_server(CLOUD_HOST, 443) is True

    assert rig.store.get(CLOUD.origin) is None
    assert rig.controller.list_servers() == []


def test_a_plain_http_server_that_requires_sign_in_is_never_paired(rig: SignInRig) -> None:
    # Without the installation's local access token for this address (a tunnel, say).
    rig.controller.launch_connect("127.0.0.1", 8420)

    [page] = rig.window.loaded_html
    assert '<h2 id="connection-error-title">Sign-in needs https://</h2>' in page
    assert rig.controller.start_pairing("127.0.0.1", 8420).status == "https_required"
    assert rig.server.requests == []
    assert rig.window.loaded_urls == []
