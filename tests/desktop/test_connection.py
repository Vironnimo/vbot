"""Tests for the Desktop in-window server selection (connection module)."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from desktop import connection as desktop_connection
from desktop.main import (
    PROBE_INVALID_TARGET,
    PROBE_NOT_VBOT_SERVER,
    PROBE_SERVER_UNREACHABLE,
    PROBE_WEBUI_AVAILABLE,
    PROBE_WEBUI_UNAVAILABLE,
    DesktopProbeResult,
    DesktopTarget,
    validate_host,
)

# -- Test doubles ------------------------------------------------------------


class FakeWindow:
    """Records the navigation calls the controller makes on the live window."""

    def __init__(self) -> None:
        self.loaded_urls: list[str] = []
        self.loaded_html: list[str] = []

    def load_url(self, url: str) -> None:
        self.loaded_urls.append(url)

    def load_html(self, content: str) -> None:
        self.loaded_html.append(content)


def probe_returning(status: str) -> Callable[[DesktopTarget], DesktopProbeResult]:
    """Build a probe stub that classifies every target with a fixed status."""

    def _probe(target: DesktopTarget) -> DesktopProbeResult:
        return DesktopProbeResult(status=status, target=target)

    return _probe


def recording_probe(
    status: str,
) -> tuple[Callable[[DesktopTarget], DesktopProbeResult], list[DesktopTarget]]:
    """Build a probe stub that records every probed target and returns ``status``."""

    seen: list[DesktopTarget] = []

    def _probe(target: DesktopTarget) -> DesktopProbeResult:
        seen.append(target)
        return DesktopProbeResult(status=status, target=target)

    return _probe, seen


@dataclass
class FakeMenuAction:
    title: str
    function: Callable[[], Any]


@dataclass
class FakeMenuSeparator:
    pass


@dataclass
class FakeMenu:
    title: str
    items: list[Any]


@dataclass
class FakeMenuModule:
    """Stand-in for ``webview.menu`` recording the constructed menu tree."""

    Menu: Callable[..., Any] = field(default=lambda title, items: FakeMenu(title, items))
    MenuAction: Callable[..., Any] = field(
        default=lambda title, function: FakeMenuAction(title, function)
    )
    MenuSeparator: Callable[..., Any] = field(default=lambda: FakeMenuSeparator())


def _write(settings_file: Path, data: dict[str, Any]) -> None:
    settings_file.write_text(json.dumps(data), encoding="utf-8")


# -- ServerEntry -------------------------------------------------------------


def test_server_entry_storage_round_trip_with_label() -> None:
    entry = desktop_connection.ServerEntry("pi.lan", 9000, "Living room")

    assert entry.to_storage() == {"host": "pi.lan", "port": 9000, "label": "Living room"}
    assert desktop_connection.ServerEntry.from_storage(entry.to_storage()) == entry


def test_server_entry_storage_omits_empty_label() -> None:
    entry = desktop_connection.ServerEntry("pi.lan", 9000)

    assert entry.to_storage() == {"host": "pi.lan", "port": 9000}


def test_server_entry_display_name_with_and_without_label() -> None:
    assert desktop_connection.ServerEntry("pi.lan", 9000).display_name() == "pi.lan:9000"
    assert desktop_connection.ServerEntry("pi.lan", 9000, "Pi").display_name() == "Pi (pi.lan:9000)"


# -- Remembered-servers operations -------------------------------------------


def test_list_servers_reads_stored_entries(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    _write(
        settings_file,
        {"servers": [{"host": "pi.lan", "port": 9000, "label": "Pi"}]},
    )

    assert desktop_connection.list_servers(settings_file) == [
        desktop_connection.ServerEntry("pi.lan", 9000, "Pi")
    ]


def test_add_server_appends_and_persists(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"

    desktop_connection.add_server("pi.lan", 9000, "Pi", settings_file=settings_file)

    stored = json.loads(settings_file.read_text(encoding="utf-8"))
    assert stored["servers"] == [{"host": "pi.lan", "port": 9000, "label": "Pi"}]


def test_add_server_replaces_existing_same_host_port_in_place(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    _write(
        settings_file,
        {
            "servers": [
                {"host": "pi.lan", "port": 9000, "label": "Old"},
                {"host": "other.lan", "port": 8420},
            ]
        },
    )

    desktop_connection.add_server("pi.lan", 9000, "New", settings_file=settings_file)

    stored = json.loads(settings_file.read_text(encoding="utf-8"))
    assert stored["servers"] == [
        {"host": "pi.lan", "port": 9000, "label": "New"},
        {"host": "other.lan", "port": 8420},
    ]


def test_add_server_validates_host_before_persisting(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"

    with pytest.raises(ValueError, match="host name or IP address"):
        desktop_connection.add_server("http://pi.lan", 9000, settings_file=settings_file)

    assert not settings_file.exists()


def test_add_server_rejects_invalid_port(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"

    with pytest.raises(ValueError, match="between 1 and 65535"):
        desktop_connection.add_server("pi.lan", 0, settings_file=settings_file)


def test_remove_server_drops_entry_and_reports_true(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    _write(
        settings_file,
        {
            "servers": [
                {"host": "pi.lan", "port": 9000},
                {"host": "other.lan", "port": 8420},
            ]
        },
    )

    removed = desktop_connection.remove_server("pi.lan", 9000, settings_file=settings_file)

    assert removed is True
    stored = json.loads(settings_file.read_text(encoding="utf-8"))
    assert stored["servers"] == [{"host": "other.lan", "port": 8420}]


def test_remove_server_unknown_returns_false_and_keeps_list(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    _write(settings_file, {"servers": [{"host": "pi.lan", "port": 9000}]})

    removed = desktop_connection.remove_server("ghost.lan", 1234, settings_file=settings_file)

    assert removed is False
    stored = json.loads(settings_file.read_text(encoding="utf-8"))
    assert stored["servers"] == [{"host": "pi.lan", "port": 9000}]


def test_remove_server_clears_last_used_when_it_pointed_at_removed(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    _write(
        settings_file,
        {
            "servers": [{"host": "pi.lan", "port": 9000}],
            "last_used": {"host": "pi.lan", "port": 9000},
        },
    )

    desktop_connection.remove_server("pi.lan", 9000, settings_file=settings_file)

    stored = json.loads(settings_file.read_text(encoding="utf-8"))
    assert "last_used" not in stored


def test_remove_server_keeps_unrelated_last_used(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    _write(
        settings_file,
        {
            "servers": [
                {"host": "pi.lan", "port": 9000},
                {"host": "other.lan", "port": 8420},
            ],
            "last_used": {"host": "other.lan", "port": 8420},
        },
    )

    desktop_connection.remove_server("pi.lan", 9000, settings_file=settings_file)

    stored = json.loads(settings_file.read_text(encoding="utf-8"))
    assert stored["last_used"] == {"host": "other.lan", "port": 8420}


def test_select_server_writes_last_used(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"

    desktop_connection.select_server("pi.lan", 9000, settings_file=settings_file)

    stored = json.loads(settings_file.read_text(encoding="utf-8"))
    assert stored["last_used"] == {"host": "pi.lan", "port": 9000}


# -- Last-used resolution ----------------------------------------------------


def test_resolve_last_used_returns_none_on_first_run(tmp_path: Path) -> None:
    assert desktop_connection.resolve_last_used(tmp_path / "settings.json") is None


def test_resolve_last_used_prefers_reference_and_carries_label(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    _write(
        settings_file,
        {
            "servers": [
                {"host": "pi.lan", "port": 9000, "label": "Pi"},
                {"host": "other.lan", "port": 8420},
            ],
            "last_used": {"host": "pi.lan", "port": 9000},
        },
    )

    assert desktop_connection.resolve_last_used(settings_file) == desktop_connection.ServerEntry(
        "pi.lan", 9000, "Pi"
    )


def test_resolve_last_used_for_reference_not_in_list_has_no_label(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    _write(
        settings_file,
        {
            "servers": [{"host": "other.lan", "port": 8420}],
            "last_used": {"host": "pi.lan", "port": 9000},
        },
    )

    assert desktop_connection.resolve_last_used(settings_file) == desktop_connection.ServerEntry(
        "pi.lan", 9000
    )


def test_resolve_last_used_falls_back_to_first_server(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    _write(
        settings_file,
        {
            "servers": [
                {"host": "first.lan", "port": 8420},
                {"host": "second.lan", "port": 9000},
            ]
        },
    )

    assert desktop_connection.resolve_last_used(settings_file) == desktop_connection.ServerEntry(
        "first.lan", 8420
    )


# -- Controller: connect / switch / reconnect --------------------------------


def test_connect_navigates_window_to_webui_with_accessor_param(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    window = FakeWindow()
    controller = desktop_connection.ConnectionController(
        settings_file=settings_file,
        window=window,
        probe=probe_returning(PROBE_WEBUI_AVAILABLE),
    )

    result = controller.connect("pi.lan", 9000, "Pi")

    assert result.status == PROBE_WEBUI_AVAILABLE
    assert window.loaded_urls == ["http://pi.lan:9000/?accessor=desktop"]
    assert window.loaded_html == []


def test_connect_success_remembers_and_marks_last_used(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    controller = desktop_connection.ConnectionController(
        settings_file=settings_file,
        window=FakeWindow(),
        probe=probe_returning(PROBE_WEBUI_AVAILABLE),
    )

    controller.connect("pi.lan", 9000, "Pi")

    stored = json.loads(settings_file.read_text(encoding="utf-8"))
    assert stored["servers"] == [{"host": "pi.lan", "port": 9000, "label": "Pi"}]
    assert stored["last_used"] == {"host": "pi.lan", "port": 9000}


@pytest.mark.parametrize(
    ("status", "expected_text"),
    [
        (PROBE_SERVER_UNREACHABLE, "Server unreachable"),
        (PROBE_WEBUI_UNAVAILABLE, "WebUI unavailable"),
        (PROBE_NOT_VBOT_SERVER, "Not a vBot server"),
    ],
)
def test_connect_failure_shows_connection_screen_inline(
    tmp_path: Path,
    status: str,
    expected_text: str,
) -> None:
    window = FakeWindow()
    controller = desktop_connection.ConnectionController(
        settings_file=tmp_path / "settings.json",
        window=window,
        probe=probe_returning(status),
    )

    result = controller.connect("pi.lan", 9000)

    assert result.status == status
    assert window.loaded_urls == []
    assert len(window.loaded_html) == 1
    assert expected_text in window.loaded_html[0]
    # Failed host/port are prefilled so the user fixes the target in place.
    assert 'value="pi.lan"' in window.loaded_html[0]
    assert 'value="9000"' in window.loaded_html[0]


def test_connect_failure_does_not_remember_server(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    controller = desktop_connection.ConnectionController(
        settings_file=settings_file,
        window=FakeWindow(),
        probe=probe_returning(PROBE_SERVER_UNREACHABLE),
    )

    controller.connect("pi.lan", 9000)

    assert desktop_connection.list_servers(settings_file) == []


def test_connect_invalid_host_renders_invalid_target_screen(tmp_path: Path) -> None:
    window = FakeWindow()
    # The real probe classifies a configuration_error target as invalid without I/O.
    controller = desktop_connection.ConnectionController(
        settings_file=tmp_path / "settings.json",
        window=window,
    )

    result = controller.connect("http://pi.lan", 9000)

    assert result.status == PROBE_INVALID_TARGET
    assert window.loaded_urls == []
    assert "Invalid host or port" in window.loaded_html[0]
    assert 'value="http://pi.lan"' in window.loaded_html[0]


def test_switch_to_connects_to_chosen_server(tmp_path: Path) -> None:
    window = FakeWindow()
    controller = desktop_connection.ConnectionController(
        settings_file=tmp_path / "settings.json",
        window=window,
        probe=probe_returning(PROBE_WEBUI_AVAILABLE),
    )

    controller.switch_to("10.0.0.5", 8500)

    assert window.loaded_urls == ["http://10.0.0.5:8500/?accessor=desktop"]


def test_reconnect_uses_last_used_target(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    _write(
        settings_file,
        {
            "servers": [{"host": "pi.lan", "port": 9000, "label": "Pi"}],
            "last_used": {"host": "pi.lan", "port": 9000},
        },
    )
    window = FakeWindow()
    probe, seen = recording_probe(PROBE_WEBUI_AVAILABLE)
    controller = desktop_connection.ConnectionController(
        settings_file=settings_file, window=window, probe=probe
    )

    result = controller.reconnect()

    assert result is not None
    assert seen[0].host == "pi.lan"
    assert seen[0].port == 9000
    assert window.loaded_urls == ["http://pi.lan:9000/?accessor=desktop"]


def test_reconnect_first_run_shows_screen_without_error(tmp_path: Path) -> None:
    window = FakeWindow()
    controller = desktop_connection.ConnectionController(
        settings_file=tmp_path / "settings.json",
        window=window,
        probe=probe_returning(PROBE_WEBUI_AVAILABLE),
    )

    result = controller.reconnect()

    assert result is None
    assert window.loaded_urls == []
    assert len(window.loaded_html) == 1
    # No probe ran, so no error banner is rendered.
    assert 'role="alert"' not in window.loaded_html[0]


def test_auto_connect_first_run_opens_connection_screen(tmp_path: Path) -> None:
    window = FakeWindow()
    controller = desktop_connection.ConnectionController(
        settings_file=tmp_path / "settings.json",
        window=window,
        probe=probe_returning(PROBE_WEBUI_AVAILABLE),
    )

    assert controller.auto_connect() is None
    assert window.loaded_html != []


def test_auto_connect_with_saved_server_connects(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    _write(settings_file, {"servers": [{"host": "pi.lan", "port": 9000}]})
    window = FakeWindow()
    controller = desktop_connection.ConnectionController(
        settings_file=settings_file,
        window=window,
        probe=probe_returning(PROBE_WEBUI_AVAILABLE),
    )

    controller.auto_connect()

    assert window.loaded_urls == ["http://pi.lan:9000/?accessor=desktop"]


def test_attach_window_binds_later_created_window(tmp_path: Path) -> None:
    controller = desktop_connection.ConnectionController(
        settings_file=tmp_path / "settings.json",
        probe=probe_returning(PROBE_WEBUI_AVAILABLE),
    )
    window = FakeWindow()
    controller.attach_window(window)

    controller.connect("pi.lan", 9000)

    assert window.loaded_urls == ["http://pi.lan:9000/?accessor=desktop"]


def test_controller_without_window_raises(tmp_path: Path) -> None:
    controller = desktop_connection.ConnectionController(
        settings_file=tmp_path / "settings.json",
        probe=probe_returning(PROBE_WEBUI_AVAILABLE),
    )

    with pytest.raises(RuntimeError, match="no window attached"):
        controller.connect("pi.lan", 9000)


def test_controller_delegates_server_list_ops(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    controller = desktop_connection.ConnectionController(settings_file=settings_file)

    controller.add_server("pi.lan", 9000, "Pi")

    assert controller.list_servers() == [desktop_connection.ServerEntry("pi.lan", 9000, "Pi")]
    assert controller.remove_server("pi.lan", 9000) is True
    assert controller.list_servers() == []


# -- Native menu -------------------------------------------------------------


def test_build_server_menu_structure() -> None:
    controller = desktop_connection.ConnectionController()
    menus = desktop_connection.build_server_menu(controller, menu_module=FakeMenuModule())

    assert len(menus) == 1
    server_menu = menus[0]
    assert server_menu.title == desktop_connection.MENU_TITLE_SERVER
    titles = [getattr(item, "title", None) for item in server_menu.items]
    assert titles == [
        desktop_connection.MENU_ACTION_SWITCH,
        None,  # the separator carries no title
        desktop_connection.MENU_ACTION_RECONNECT,
    ]
    assert isinstance(server_menu.items[1], FakeMenuSeparator)


def test_menu_switch_action_shows_connection_screen(tmp_path: Path) -> None:
    window = FakeWindow()
    controller = desktop_connection.ConnectionController(
        settings_file=tmp_path / "settings.json", window=window
    )
    menus = desktop_connection.build_server_menu(controller, menu_module=FakeMenuModule())
    switch_action = menus[0].items[0]

    switch_action.function()

    assert len(window.loaded_html) == 1
    assert "Connect to a server" in window.loaded_html[0]


def test_menu_reconnect_action_invokes_controller(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    _write(settings_file, {"last_used": {"host": "pi.lan", "port": 9000}})
    window = FakeWindow()
    controller = desktop_connection.ConnectionController(
        settings_file=settings_file,
        window=window,
        probe=probe_returning(PROBE_WEBUI_AVAILABLE),
    )
    menus = desktop_connection.build_server_menu(controller, menu_module=FakeMenuModule())
    reconnect_action = menus[0].items[2]

    reconnect_action.function()

    assert window.loaded_urls == ["http://pi.lan:9000/?accessor=desktop"]


# -- Connection screen HTML --------------------------------------------------


def test_connection_html_lists_saved_servers_with_connect_hooks() -> None:
    servers = [
        desktop_connection.ServerEntry("pi.lan", 9000, "Pi"),
        desktop_connection.ServerEntry("10.0.0.5", 8500),
    ]

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


def test_connection_html_empty_state_when_no_servers() -> None:
    page = desktop_connection.build_connection_html([])

    assert "No servers saved yet." in page


def test_connection_html_no_error_banner_without_probe_result() -> None:
    page = desktop_connection.build_connection_html([])

    assert 'role="alert"' not in page
    # Default suggestion prefilled, never an auto-connect target.
    assert 'value="127.0.0.1"' in page
    assert 'value="8420"' in page


@pytest.mark.parametrize(
    ("status", "expected_text"),
    [
        (PROBE_SERVER_UNREACHABLE, "Server unreachable"),
        (PROBE_WEBUI_UNAVAILABLE, "WebUI unavailable"),
        (PROBE_NOT_VBOT_SERVER, "Not a vBot server"),
        (PROBE_INVALID_TARGET, "Invalid host or port"),
    ],
)
def test_connection_html_renders_each_probe_failure_inline(
    status: str,
    expected_text: str,
) -> None:
    target = DesktopTarget("pi.lan", 9000, "http://pi.lan:9000/")
    page = desktop_connection.build_connection_html(
        [], DesktopProbeResult(status=status, target=target)
    )

    assert 'role="alert"' in page
    assert expected_text in page
    # The failed host/port prefill the form so the user corrects it in place.
    assert 'value="pi.lan"' in page
    assert 'value="9000"' in page


def test_connection_html_escapes_failed_host_in_error_and_prefill() -> None:
    malicious = '<script>alert("x")</script>'
    target = DesktopTarget(malicious, 9000, "")
    page = desktop_connection.build_connection_html(
        [], DesktopProbeResult(status=PROBE_INVALID_TARGET, target=target)
    )

    assert malicious not in page
    assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;" in page


def test_connection_html_escapes_saved_server_label_and_host() -> None:
    servers = [desktop_connection.ServerEntry('<b>"evil"</b>', 9000, "<i>label</i>")]

    page = desktop_connection.build_connection_html(servers)

    assert "<b>" not in page
    assert "<i>label</i>" not in page
    assert "&lt;b&gt;" in page
    assert "&lt;i&gt;label&lt;/i&gt;" in page


def test_quote_bearing_host_rejected_and_never_breaks_out_of_data_attribute(
    tmp_path: Path,
) -> None:
    # A host that would break out of the old inline onclick JS-string context.
    malicious = "a');document.title='x';('"

    # (a) Defense-in-depth: the host is rejected before it can ever be stored,
    # both at the low-level validator and the persisting add_server path.
    with pytest.raises(ValueError, match="host name or IP address"):
        validate_host(malicious)
    settings_file = tmp_path / "settings.json"
    with pytest.raises(ValueError, match="host name or IP address"):
        desktop_connection.add_server(malicious, 9000, settings_file=settings_file)
    assert not settings_file.exists()

    # (b) Even when a ServerEntry is constructed directly (bypassing validation)
    # the renderer only emits the host html-escaped inside a data-host attribute,
    # with no executable inline onclick breakout — the single quote is encoded
    # and the raw "');" sequence never appears outside the escaped attribute.
    page = desktop_connection.build_connection_html(
        [desktop_connection.ServerEntry(malicious, 9000)]
    )

    assert 'data-host="a&#x27;);document.title=&#x27;x&#x27;;(&#x27;"' in page
    assert "a');document.title='x';('" not in page
    assert "');" not in page
    assert 'onclick="connectSaved(' not in page


def test_connection_module_does_not_import_server_or_core() -> None:
    source = Path(desktop_connection.__file__).read_text(encoding="utf-8")

    assert "from server" not in source
    assert "import server" not in source
    assert "from core" not in source
    assert "import core" not in source
