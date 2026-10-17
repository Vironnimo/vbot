"""The Desktop window zoom as the page reaches it through the bridge."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from desktop.bridge import BridgeError, DesktopBridge
from desktop.speech.microphone import MicrophoneService
from desktop.wakeword.controller import VoiceController
from desktop.zoom import DEFAULT_ZOOM, ZOOM_LEVELS, DesktopZoom


def _bridge(zoom: DesktopZoom) -> DesktopBridge:
    return DesktopBridge(
        voice=cast(VoiceController, object()),
        microphone=cast(MicrophoneService, object()),
        zoom=zoom,
    )


def test_a_zoom_level_is_applied_and_stays_for_the_next_start(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"window": {"width": 900, "height": 700}}), encoding="utf-8")
    applied: list[float] = []
    zoom = DesktopZoom(settings_path=settings)
    zoom.attach(applied.append)
    bridge = _bridge(zoom)

    assert bridge.getZoom() == {
        "supported": True,
        "percent": DEFAULT_ZOOM,
        "levels": list(ZOOM_LEVELS),
        "default": DEFAULT_ZOOM,
    }
    # JavaScript numbers may arrive as floats.
    assert bridge.setZoom(125.0)["percent"] == 125
    assert applied == [1.25]

    stored = json.loads(settings.read_text(encoding="utf-8"))
    assert stored == {"window": {"width": 900, "height": 700}, "zoom": {"percent": 125}}
    restarted = DesktopZoom(settings_path=settings)
    assert restarted.factor() == 1.25
    assert restarted.status()["supported"] is False


@pytest.mark.parametrize("value", [33, 126, "125", True, None, [125]])
def test_a_value_that_is_no_zoom_level_is_refused(tmp_path: Path, value: Any) -> None:
    settings = tmp_path / "settings.json"
    applied: list[float] = []
    zoom = DesktopZoom(settings_path=settings)
    zoom.attach(applied.append)

    with pytest.raises(BridgeError, match="^zoom_invalid$"):
        _bridge(zoom).setZoom(value)

    assert applied == []
    assert not settings.exists()
    assert zoom.status()["percent"] == DEFAULT_ZOOM


@pytest.mark.parametrize("stored", [{"percent": 33}, {"percent": "125"}, {}, "125"])
def test_an_unknown_stored_zoom_reads_as_the_default(tmp_path: Path, stored: Any) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"zoom": stored}), encoding="utf-8")

    assert DesktopZoom(settings_path=settings).factor() == DEFAULT_ZOOM / 100
