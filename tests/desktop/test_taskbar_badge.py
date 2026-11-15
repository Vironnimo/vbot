"""The Desktop taskbar badge as the page reaches it through the bridge."""

from __future__ import annotations

from typing import Any, cast

import pytest

from desktop import taskbar_badge
from desktop.bridge import BridgeError, DesktopBridge
from desktop.speech.microphone import MicrophoneService
from desktop.taskbar_badge import BADGE_ERROR_INVALID, TaskbarBadge
from desktop.wakeword.controller import VoiceController


def _bridge(badge: TaskbarBadge) -> DesktopBridge:
    return DesktopBridge(
        voice=cast(VoiceController, object()),
        microphone=cast(MicrophoneService, object()),
        taskbar_badge=badge,
    )


def test_the_badge_shows_the_count_until_it_is_removed() -> None:
    shown: list[tuple[str, str]] = []
    badge = TaskbarBadge()
    badge.attach(lambda label, description: shown.append((label, description)))
    bridge = _bridge(badge)

    # JavaScript numbers may arrive as floats.
    assert bridge.setTaskbarBadge(1.0) == {"count": 1}
    assert bridge.setTaskbarBadge(1) == {"count": 1}  # unchanged: the taskbar is left alone
    assert bridge.setTaskbarBadge(9) == {"count": 9}
    assert bridge.setTaskbarBadge(10) == {"count": 10}
    assert bridge.setTaskbarBadge(0) == {"count": 0}
    bridge.setTaskbarBadge(3)
    badge.clear()  # the page that sent the count is gone

    assert [label for label, _ in shown] == ["1", "9", "9+", "", "3", ""]
    # No badge has no description for assistive technology either.
    assert [bool(description) for _, description in shown] == [True, True, True, False, True, False]


@pytest.mark.parametrize("value", [-1, 1.5, "3", True, None, [3]])
def test_a_value_that_is_no_count_is_refused(value: Any) -> None:
    shown: list[tuple[str, str]] = []
    badge = TaskbarBadge()
    badge.attach(lambda label, description: shown.append((label, description)))

    with pytest.raises(BridgeError) as refused:
        _bridge(badge).setTaskbarBadge(value)

    assert str(refused.value) == BADGE_ERROR_INVALID
    assert shown == []


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_other_platforms_have_no_badge(monkeypatch: pytest.MonkeyPatch, platform: str) -> None:
    monkeypatch.setattr(taskbar_badge.sys, "platform", platform)

    assert taskbar_badge.bind_window_badge(object()) is None
    assert TaskbarBadge().supported is False
