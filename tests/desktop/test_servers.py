"""Remembered-server identity, CRUD, storage and launch selection."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from desktop import servers as desktop_servers
from desktop.servers import ServerEntry
from tests.desktop.server_test_support import CLOUD_ENTRY, _settings, _stored


def test_server_entry_storage_and_display_name() -> None:
    labelled = ServerEntry("pi.lan", 9000, "Living room")
    plain = ServerEntry("pi.lan", 9000)
    https = ServerEntry("vbot.example.com", 443, "Cloud", "https")

    assert labelled.to_storage() == {"host": "pi.lan", "port": 9000, "label": "Living room"}
    assert ServerEntry.from_storage(labelled.to_storage()) == labelled
    assert plain.to_storage() == {"host": "pi.lan", "port": 9000}
    assert labelled.display_name() == "Living room (pi.lan:9000)"
    assert plain.display_name() == "pi.lan:9000"
    # An HTTPS server is stored with its scheme and shown and passed by its address.
    assert https.to_storage() == {
        "host": "vbot.example.com",
        "port": 443,
        "scheme": "https",
        "label": "Cloud",
    }
    assert ServerEntry.from_storage(https.to_storage()) == https
    assert https.display_name() == "Cloud (https://vbot.example.com)"
    assert https.to_bridge() == {
        "host": "https://vbot.example.com",
        "port": 443,
        "scheme": "https",
        "label": "Cloud",
    }
    assert plain.to_bridge() == {"host": "pi.lan", "port": 9000, "scheme": "http"}


def test_add_server_appends_and_replaces_the_same_target_in_place(tmp_path: Path) -> None:
    settings_file = _settings(tmp_path)

    desktop_servers.add_server("pi.lan", 9000, "Old", settings_file=settings_file)
    desktop_servers.add_server("other.lan", 8420, settings_file=settings_file)
    desktop_servers.add_server("https://VBOT.example.com", None, settings_file=settings_file)
    desktop_servers.add_server("pi.lan", 9000, "New", settings_file=settings_file)
    # The same HTTPS server spelled differently; the same host over HTTPS is another server.
    desktop_servers.add_server(
        "https://vbot.example.com:443/", 8420, "Cloud", settings_file=settings_file
    )
    desktop_servers.add_server("https://pi.lan:9000", None, settings_file=settings_file)

    assert _stored(settings_file)["servers"] == [
        {"host": "pi.lan", "port": 9000, "label": "New"},
        {"host": "other.lan", "port": 8420},
        {"host": "vbot.example.com", "port": 443, "scheme": "https", "label": "Cloud"},
        {"host": "pi.lan", "port": 9000, "scheme": "https"},
    ]
    assert desktop_servers.list_servers(settings_file) == [
        ServerEntry("pi.lan", 9000, "New"),
        ServerEntry("other.lan", 8420),
        ServerEntry("vbot.example.com", 443, "Cloud", "https"),
        ServerEntry("pi.lan", 9000, None, "https"),
    ]


def test_concurrent_server_mutations_preserve_both_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_file = _settings(tmp_path)
    first_writing = threading.Event()
    release_first = threading.Event()
    second_finished = threading.Event()
    write_servers = desktop_servers.write_servers

    def paused_write(entries: list[dict[str, Any]], path: Path | None) -> None:
        if entries == [{"host": "first.lan", "port": 9000}]:
            first_writing.set()
            assert release_first.wait(timeout=2)
        write_servers(entries, path)

    def add_second() -> None:
        desktop_servers.add_server("second.lan", 9000, settings_file=settings_file)
        second_finished.set()

    monkeypatch.setattr(desktop_servers, "write_servers", paused_write)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            desktop_servers.add_server, "first.lan", 9000, settings_file=settings_file
        )
        assert first_writing.wait(timeout=2)
        second = pool.submit(add_second)
        try:
            assert not second_finished.wait(timeout=0.1)
        finally:
            release_first.set()
        first.result(timeout=2)
        second.result(timeout=2)

    assert {entry.host for entry in desktop_servers.list_servers(settings_file)} == {
        "first.lan",
        "second.lan",
    }


@pytest.mark.parametrize(
    ("host", "port"),
    [("https://pi.lan/app", 9000), ("a');document.title='x';('", 9000), ("pi.lan", 0)],
    ids=["url-with-path", "quote-bearing-host", "invalid-port"],
)
def test_add_server_validates_the_target_before_persisting(
    tmp_path: Path, host: str, port: int
) -> None:
    settings_file = _settings(tmp_path)

    with pytest.raises(ValueError):
        desktop_servers.add_server(host, port, settings_file=settings_file)

    assert not settings_file.exists()


@pytest.mark.parametrize(
    ("removed_target", "last_used", "removed", "servers", "last_used_after"),
    [
        (
            ("pi.lan", 9000),
            None,
            True,
            [{"host": "other.lan", "port": 8420}, CLOUD_ENTRY],
            None,
        ),
        (
            ("ghost.lan", 1234),
            None,
            False,
            [{"host": "pi.lan", "port": 9000}, {"host": "other.lan", "port": 8420}, CLOUD_ENTRY],
            None,
        ),
        (
            ("pi.lan", 9000),
            {"host": "pi.lan", "port": 9000},
            True,
            [{"host": "other.lan", "port": 8420}, CLOUD_ENTRY],
            None,
        ),
        (
            ("pi.lan", 9000),
            {"host": "other.lan", "port": 8420},
            True,
            [{"host": "other.lan", "port": 8420}, CLOUD_ENTRY],
            {"host": "other.lan", "port": 8420},
        ),
        (
            ("https://vbot.example.com", 443),
            CLOUD_ENTRY,
            True,
            [{"host": "pi.lan", "port": 9000}, {"host": "other.lan", "port": 8420}],
            None,
        ),
        # The HTTP server of the same host is another server.
        (
            ("vbot.example.com", 443),
            CLOUD_ENTRY,
            False,
            [{"host": "pi.lan", "port": 9000}, {"host": "other.lan", "port": 8420}, CLOUD_ENTRY],
            CLOUD_ENTRY,
        ),
    ],
    ids=[
        "removes",
        "unknown",
        "clears-its-last-used",
        "keeps-other-last-used",
        "https",
        "http-of-an-https-host",
    ],
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
        "servers": [
            {"host": "pi.lan", "port": 9000},
            {"host": "other.lan", "port": 8420},
            CLOUD_ENTRY,
        ]
    }
    if last_used is not None:
        saved["last_used"] = last_used
    settings_file = _settings(tmp_path, saved)

    host, port = removed_target
    assert desktop_servers.remove_server(host, port, settings_file=settings_file) is removed

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
        (
            {
                "servers": [
                    {"host": "vbot.example.com", "port": 443},
                    {**CLOUD_ENTRY, "label": "Cloud"},
                ],
                "last_used": CLOUD_ENTRY,
            },
            ServerEntry("vbot.example.com", 443, "Cloud", "https"),
        ),
    ],
    ids=[
        "first-run",
        "last-used-with-label",
        "last-used-not-remembered",
        "first-server",
        "https-last-used",
    ],
)
def test_resolve_last_used_picks_the_launch_target(
    tmp_path: Path, saved: dict[str, Any] | None, expected: ServerEntry | None
) -> None:
    assert desktop_servers.resolve_last_used(_settings(tmp_path, saved)) == expected


def test_select_server_writes_last_used(tmp_path: Path) -> None:
    settings_file = _settings(tmp_path)

    desktop_servers.select_server("pi.lan", 9000, settings_file=settings_file)
    assert _stored(settings_file)["last_used"] == {"host": "pi.lan", "port": 9000}

    desktop_servers.select_server("https://vbot.example.com", None, settings_file=settings_file)
    assert _stored(settings_file)["last_used"] == CLOUD_ENTRY
