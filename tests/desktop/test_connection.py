"""Tests for the Desktop in-window server selection (connection module)."""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from desktop import connection as desktop_connection
from desktop.connection import LAUNCH_WAIT_SECONDS, ConnectionController, ServerEntry
from desktop.main import (
    PROBE_INVALID_TARGET,
    PROBE_NOT_VBOT_SERVER,
    PROBE_SERVER_UNREACHABLE,
    PROBE_WEBUI_AVAILABLE,
    PROBE_WEBUI_UNAVAILABLE,
    DesktopProbeResult,
    DesktopTarget,
    SessionLink,
    validate_host,
)

_TEST_DESKTOP_SESSION_ID = "desktop-test-session"
PI_URL = f"http://pi.lan:9000/?accessor=desktop&desktop_session={_TEST_DESKTOP_SESSION_ID}"


class _FixedUuid:
    hex = _TEST_DESKTOP_SESSION_ID


@pytest.fixture(autouse=True)
def _use_stable_desktop_session_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep navigation expectations deterministic outside the UUID-specific tests."""

    monkeypatch.setattr(desktop_connection, "uuid4", lambda: _FixedUuid())


class FakeWindow:
    """Records the navigation calls the controller makes on the live window."""

    def __init__(self) -> None:
        self.loaded_urls: list[str] = []
        self.loaded_html: list[str] = []
        self._loaded = threading.Condition()

    def load_url(self, url: str) -> None:
        with self._loaded:
            self.loaded_urls.append(url)
            self._loaded.notify_all()

    def load_html(self, content: str) -> None:
        with self._loaded:
            self.loaded_html.append(content)
            self._loaded.notify_all()

    def wait_for_loads(self, count: int) -> None:
        """Wait until the controller (or its launch wait thread) loaded ``count`` documents."""

        with self._loaded:
            assert self._loaded.wait_for(
                lambda: len(self.loaded_urls) + len(self.loaded_html) >= count, timeout=5
            )


def probe_returning(status: str) -> Callable[[DesktopTarget], DesktopProbeResult]:
    """Build a probe stub that classifies every target with a fixed status."""

    def _probe(target: DesktopTarget) -> DesktopProbeResult:
        return DesktopProbeResult(status=status, target=target)

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


def _settings(tmp_path: Path, data: dict[str, Any] | None = None) -> Path:
    settings_file = tmp_path / "settings.json"
    if data is not None:
        settings_file.write_text(json.dumps(data), encoding="utf-8")
    return settings_file


def _stored(settings_file: Path) -> dict[str, Any]:
    stored: dict[str, Any] = json.loads(settings_file.read_text(encoding="utf-8"))
    return stored


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


def test_server_entry_storage_and_display_name() -> None:
    labelled = ServerEntry("pi.lan", 9000, "Living room")
    plain = ServerEntry("pi.lan", 9000)

    assert labelled.to_storage() == {"host": "pi.lan", "port": 9000, "label": "Living room"}
    assert ServerEntry.from_storage(labelled.to_storage()) == labelled
    assert plain.to_storage() == {"host": "pi.lan", "port": 9000}
    assert labelled.display_name() == "Living room (pi.lan:9000)"
    assert plain.display_name() == "pi.lan:9000"


def test_add_server_appends_and_replaces_the_same_target_in_place(tmp_path: Path) -> None:
    settings_file = _settings(tmp_path)

    desktop_connection.add_server("pi.lan", 9000, "Old", settings_file=settings_file)
    desktop_connection.add_server("other.lan", 8420, settings_file=settings_file)
    desktop_connection.add_server("pi.lan", 9000, "New", settings_file=settings_file)

    assert _stored(settings_file)["servers"] == [
        {"host": "pi.lan", "port": 9000, "label": "New"},
        {"host": "other.lan", "port": 8420},
    ]
    assert desktop_connection.list_servers(settings_file) == [
        ServerEntry("pi.lan", 9000, "New"),
        ServerEntry("other.lan", 8420),
    ]


@pytest.mark.parametrize(
    ("host", "port"),
    [("http://pi.lan", 9000), ("a');document.title='x';('", 9000), ("pi.lan", 0)],
    ids=["url-host", "quote-bearing-host", "invalid-port"],
)
def test_add_server_validates_the_target_before_persisting(
    tmp_path: Path, host: str, port: int
) -> None:
    settings_file = _settings(tmp_path)

    with pytest.raises(ValueError):
        desktop_connection.add_server(host, port, settings_file=settings_file)

    assert not settings_file.exists()


@pytest.mark.parametrize(
    ("removed_target", "last_used", "removed", "servers", "last_used_after"),
    [
        (("pi.lan", 9000), None, True, [{"host": "other.lan", "port": 8420}], None),
        (
            ("ghost.lan", 1234),
            None,
            False,
            [{"host": "pi.lan", "port": 9000}, {"host": "other.lan", "port": 8420}],
            None,
        ),
        (
            ("pi.lan", 9000),
            {"host": "pi.lan", "port": 9000},
            True,
            [{"host": "other.lan", "port": 8420}],
            None,
        ),
        (
            ("pi.lan", 9000),
            {"host": "other.lan", "port": 8420},
            True,
            [{"host": "other.lan", "port": 8420}],
            {"host": "other.lan", "port": 8420},
        ),
    ],
    ids=["removes", "unknown", "clears-its-last-used", "keeps-other-last-used"],
)
def test_remove_server_forgets_the_target_and_a_last_used_pointing_at_it(
    tmp_path: Path,
    removed_target: tuple[str, int],
    last_used: dict[str, Any] | None,
    removed: bool,
    servers: list[dict[str, Any]],
    last_used_after: dict[str, Any] | None,
) -> None:
    saved: dict[str, Any] = {
        "servers": [{"host": "pi.lan", "port": 9000}, {"host": "other.lan", "port": 8420}]
    }
    if last_used is not None:
        saved["last_used"] = last_used
    settings_file = _settings(tmp_path, saved)

    host, port = removed_target
    assert desktop_connection.remove_server(host, port, settings_file=settings_file) is removed

    stored = _stored(settings_file)
    assert stored["servers"] == servers
    assert stored.get("last_used") == last_used_after


@pytest.mark.parametrize(
    ("saved", "expected"),
    [
        (None, None),
        (
            {
                "servers": [
                    {"host": "pi.lan", "port": 9000, "label": "Pi"},
                    {"host": "other.lan", "port": 8420},
                ],
                "last_used": {"host": "pi.lan", "port": 9000},
            },
            ServerEntry("pi.lan", 9000, "Pi"),
        ),
        (
            {
                "servers": [{"host": "other.lan", "port": 8420}],
                "last_used": {"host": "pi.lan", "port": 9000},
            },
            ServerEntry("pi.lan", 9000),
        ),
        (
            {
                "servers": [
                    {"host": "first.lan", "port": 8420},
                    {"host": "second.lan", "port": 9000},
                ]
            },
            ServerEntry("first.lan", 8420),
        ),
    ],
    ids=["first-run", "last-used-with-label", "last-used-not-remembered", "first-server"],
)
def test_resolve_last_used_picks_the_launch_target(
    tmp_path: Path, saved: dict[str, Any] | None, expected: ServerEntry | None
) -> None:
    assert desktop_connection.resolve_last_used(_settings(tmp_path, saved)) == expected


def test_select_server_writes_last_used(tmp_path: Path) -> None:
    settings_file = _settings(tmp_path)

    desktop_connection.select_server("pi.lan", 9000, settings_file=settings_file)

    assert _stored(settings_file)["last_used"] == {"host": "pi.lan", "port": 9000}


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
    assert desktop_connection.list_servers(settings_file) == []
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
    assert desktop_connection.list_servers(settings_file) == []
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
    stored_last_used = desktop_connection.resolve_last_used(settings_file)
    assert stored_last_used == (ServerEntry(*NAS) if connected else None)


def test_an_invalid_host_renders_the_invalid_target_screen_without_io(tmp_path: Path) -> None:
    controller, window, _ = _controller(tmp_path, status=None)

    result = controller.launch_connect("http://pi.lan", 9000)

    assert result.status == PROBE_INVALID_TARGET
    assert window.loaded_urls == []
    assert 'role="alert"' in window.loaded_html[0]
    assert 'value="http://pi.lan"' in window.loaded_html[0]


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
    assert desktop_connection.resolve_last_used(settings_file) == ServerEntry("pi.lan", 9000, "Pi")


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
    assert 'value="8420"' in page


# -- Connection screen HTML ------------------------------------------------------------


def test_connection_html_lists_saved_servers_with_connect_hooks() -> None:
    servers = [ServerEntry("pi.lan", 9000, "Pi"), ServerEntry("10.0.0.5", 8500)]

    page = desktop_connection.build_connection_html(servers)

    assert "Pi (pi.lan:9000)" in page
    # Saved hosts/ports ride data-* attributes; a static handler reads them as
    # strings via dataset, never as interpolated JS (no inline onclick carries a
    # host in a JS-string context).
    assert 'data-host="pi.lan" data-port="9000"' in page
    assert 'data-host="10.0.0.5" data-port="8500"' in page
    assert "connectSaved('pi.lan'" not in page
    assert "connectSaved('10.0.0.5'" not in page
    assert "connectSaved(button.dataset.host" in page


def test_connection_html_awaits_bridge_result_before_navigation() -> None:
    page = desktop_connection.build_connection_html([])

    bridge_call = "await window.pywebview.api.connect(host, port)"
    navigation = "window.location.assign(result.url)"
    assert bridge_call in page
    assert navigation in page
    assert page.index(bridge_call) < page.index(navigation)
    # The user's own connect hides a launch wait's waiting state first.
    assert page.index("waiting.hidden = true") < page.index(bridge_call)
    assert ".textContent = title" in page
    assert ".textContent = body" in page
    assert "prefillConnectionTarget(host, port)" in page
    assert "innerHTML" not in page


@pytest.mark.parametrize("state", ["error", "waiting"])
def test_connection_html_escapes_the_target_host_in_its_banner_and_prefill(state: str) -> None:
    malicious = '<script>alert("x")</script>'
    target = DesktopTarget(malicious, 9000, "")
    if state == "error":
        page = desktop_connection.build_connection_html(
            [], DesktopProbeResult(status=PROBE_INVALID_TARGET, target=target)
        )
    else:
        page = desktop_connection.build_connection_html([], waiting_for=target)

    assert malicious not in page
    assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;" in page


def test_connection_html_escapes_saved_server_label_and_host() -> None:
    servers = [ServerEntry('<b>"evil"</b>', 9000, "<i>label</i>")]

    page = desktop_connection.build_connection_html(servers)

    assert "<b>" not in page
    assert "<i>label</i>" not in page
    assert "&lt;b&gt;" in page
    assert "&lt;i&gt;label&lt;/i&gt;" in page


def test_quote_bearing_host_never_breaks_out_of_the_data_attribute() -> None:
    # A host that would break out of the old inline onclick JS-string context.
    # Validation rejects it before storage; a directly built entry must still
    # render only html-escaped inside data-host, with no inline onclick.
    malicious = "a');document.title='x';('"
    with pytest.raises(ValueError):
        validate_host(malicious)

    page = desktop_connection.build_connection_html([ServerEntry(malicious, 9000)])

    assert 'data-host="a&#x27;);document.title=&#x27;x&#x27;;(&#x27;"' in page
    assert malicious not in page
    assert "');" not in page
    assert 'onclick="connectSaved(' not in page
