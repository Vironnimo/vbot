"""The server follows its installation's update check and announces a changed answer."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

from cli.application import availability
from cli.application.availability import Availability, AvailableVersion
from cli.application.state import Installation
from server import update_availability
from server.rpc.application_methods import method_handlers
from server.rpc.dispatcher import dispatch_method
from server.update_availability import UpdateAvailabilityWatch

_AT = "2026-11-02T08:00:00.000000Z"


def _install(root: Path) -> Installation:
    install = Installation(root, "server", "127.0.0.1", 8420, str(root / "data"))
    version = root / "versions" / "rel_active"
    version.mkdir(parents=True)
    (version / "release.json").write_text(
        json.dumps({"version": "0.4.35", "channel": "release"}), encoding="utf-8"
    )
    (root / "active-version").write_text("rel_active\n", encoding="ascii")
    return install


def _answer(version_id: str | None) -> Availability:
    newer = None if version_id is None else AvailableVersion(version_id, "0.4.36", "0.4.36")
    return Availability("release", "rel_active", _AT, checked_at=_AT, available=newer)


@pytest.mark.asyncio
async def test_update_status_reports_the_stored_answer_of_the_owning_installation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def status(watch: UpdateAvailabilityWatch):
        state = type("State", (), {"update_availability": watch})()
        return dispatch_method(state, "application.update_status", {}, method_handlers())

    assert await status(UpdateAvailabilityWatch(None, lambda: None)) == {
        "installed": False,
        "available": None,
        "checked_at": None,
        "channel": None,
    }

    install = _install(tmp_path)
    watch = UpdateAvailabilityWatch(install, lambda: None)
    assert (await status(watch))["available"] is None
    # The record the tray wrote is read fresh; the failure reason stays private.
    failed = replace(_answer("rel_newer"), error="offline")
    monkeypatch.setattr(availability, "read", lambda _install: failed)
    assert await status(watch) == {
        "installed": True,
        "channel": "release",
        "checked_at": _AT,
        "available": {"version_id": "rel_newer", "version": "0.4.36", "label": "0.4.36"},
    }


@pytest.mark.asyncio
async def test_the_watch_publishes_only_a_changed_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    answers = [_answer(None), _answer(None), _answer("rel_newer")]
    current: list[Availability] = []
    published = asyncio.Event()
    publications: list[str] = []

    def refresh(_install: Installation) -> Availability:
        current.append(answers[min(len(current), len(answers) - 1)])
        return current[-1]

    def publish() -> None:
        publications.append(current[-1].available.version_id if current[-1].available else "")
        if len(publications) == 2:
            published.set()

    monkeypatch.setattr(update_availability.availability, "refresh", refresh)
    monkeypatch.setattr(update_availability.availability, "read", lambda _install: current[-1])
    watch = UpdateAvailabilityWatch(_install(tmp_path), publish, tick_s=0)
    watch.start()
    try:
        await asyncio.wait_for(published.wait(), timeout=5)
    finally:
        await watch.aclose()

    assert publications == ["", "rel_newer"]
