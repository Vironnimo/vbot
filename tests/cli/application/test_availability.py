"""The update check says whether the channel publishes a newer version, and keeps the answer."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from cli.application import availability
from cli.application.state import CHANNEL_DOWNLOADS, Installation

_START = datetime(2026, 11, 2, 8, 0, tzinfo=UTC)


def _install(root: Path) -> Installation:
    install = Installation(root, "server", "127.0.0.1", 8420, str(root / "data"))
    version = root / "versions" / "rel_active"
    version.mkdir(parents=True)
    (version / "release.json").write_text(
        json.dumps({"version": "0.4.35", "channel": "release"}), encoding="utf-8"
    )
    (root / "active-version").write_text("rel_active\n", encoding="ascii")
    return install


class _Channel:
    """The release channel's identity download; counts how often it is asked."""

    def __init__(self, version_id: str = "rel_newer", version: str = "0.4.36") -> None:
        self.identity = {
            "schema_version": 1,
            "version_id": version_id,
            "version": version,
            "channel": "release",
        }
        self.requests = 0
        self.offline = False

    def client(self) -> httpx.Client:
        def answer(request: httpx.Request) -> httpx.Response:
            assert str(request.url) == f"{CHANNEL_DOWNLOADS['release']}/vbot-release.json"
            self.requests += 1
            if self.offline:
                raise httpx.ConnectError("offline", request=request)
            return httpx.Response(200, json=self.identity)

        return httpx.Client(transport=httpx.MockTransport(answer))


@pytest.mark.parametrize(
    ("version_id", "version", "newer"),
    [
        pytest.param("rel_newer", "0.4.36", True, id="newer"),
        pytest.param("rel_active", "0.4.35", False, id="active"),
        pytest.param("rel_older", "0.4.34", False, id="older"),
    ],
)
def test_check_stores_whether_an_update_would_install_a_newer_version(
    tmp_path: Path, version_id: str, version: str, newer: bool
) -> None:
    install = _install(tmp_path)
    channel = _Channel(version_id, version)

    result = availability.check(install, client=channel.client(), now=lambda: _START)

    expected = {"version_id": version_id, "version": version, "label": version} if newer else None
    assert result.projection() == {
        "channel": "release",
        "checked_at": "2026-11-02T08:00:00.000000Z",
        "error": None,
        "available": expected,
    }
    # Every reader (tray, server, CLI) sees the stored answer.
    stored = availability.read(install)
    assert stored is not None and stored.projection() == result.projection()


def test_refresh_asks_the_channel_only_when_the_answer_is_due(tmp_path: Path) -> None:
    install = _install(tmp_path)
    channel = _Channel()
    client = channel.client()

    def refresh(after: timedelta) -> availability.Availability:
        return availability.refresh(install, client=client, now=lambda: _START + after)

    refresh(timedelta())
    refresh(timedelta(hours=5))
    assert channel.requests == 1
    refresh(availability.CHECK_INTERVAL)
    assert channel.requests == 2

    # An answer for another active version (an update installed one) is due at once.
    (tmp_path / "active-version").write_text("rel_newer\n", encoding="ascii")
    (tmp_path / "versions" / "rel_newer").mkdir()
    (tmp_path / "versions" / "rel_newer" / "release.json").write_text(
        json.dumps({"version": "0.4.36", "channel": "release"}), encoding="utf-8"
    )
    assert availability.read(install) is None
    assert refresh(availability.CHECK_INTERVAL + timedelta(minutes=1)).available is None
    assert channel.requests == 3


def test_a_failed_check_keeps_the_known_answer_and_is_retried_sooner(tmp_path: Path) -> None:
    install = _install(tmp_path)
    channel = _Channel()
    client = channel.client()
    availability.check(install, client=client, now=lambda: _START)

    channel.offline = True
    failed = availability.refresh(
        install, client=client, now=lambda: _START + availability.CHECK_INTERVAL
    )

    assert failed.error == "offline"
    assert failed.available is not None and failed.available.version_id == "rel_newer"
    assert failed.checked_at == "2026-11-02T08:00:00.000000Z"
    after_failure = _START + availability.CHECK_INTERVAL
    availability.refresh(install, client=client, now=lambda: after_failure + timedelta(minutes=30))
    assert channel.requests == 2
    availability.refresh(install, client=client, now=lambda: after_failure + timedelta(hours=1))
    assert channel.requests == 3


def test_an_unreadable_record_only_makes_the_check_due(tmp_path: Path) -> None:
    install = _install(tmp_path)
    (tmp_path / availability.RECORD_NAME).write_text("{not json", encoding="utf-8")
    channel = _Channel()

    assert availability.read(install) is None
    result = availability.refresh(install, client=channel.client(), now=lambda: _START)

    assert channel.requests == 1 and result.available is not None
