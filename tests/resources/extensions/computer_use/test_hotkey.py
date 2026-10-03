"""The double-Esc emergency stop: which key events it counts."""

from __future__ import annotations

import threading

import pytest

from resources.extensions.computer_use import hotkey as hotkey_module
from resources.extensions.computer_use.hotkey import EmergencyHotkey

ESCAPE = 0x1B


@pytest.mark.parametrize("flags", [0x10, 0x02])  # injected, lower-integrity injected
def test_emergency_stop_ignores_injected_escape(flags):
    hotkey = EmergencyHotkey(lambda owner: None)
    hotkey.set_armed(object())
    for _ in range(2):
        hotkey._key_event(ESCAPE, True, flags)
        hotkey._key_event(ESCAPE, False, flags)
    assert not hotkey.pending_owner


def test_emergency_stop_needs_two_separate_physical_presses(monkeypatch):
    now = [10.0]
    monkeypatch.setattr(hotkey_module.time, "monotonic", lambda: now[0])
    hotkey = EmergencyHotkey(lambda owner: None)
    hotkey.set_armed(object())
    hotkey._key_event(ESCAPE, True, 0)
    now[0] += 0.2
    hotkey._key_event(ESCAPE, True, 0)  # OS auto-repeat.
    assert not hotkey.pending_owner
    hotkey._key_event(ESCAPE, False, 0)
    hotkey._key_event(ESCAPE, True, 0)
    assert hotkey.pending_owner


@pytest.mark.parametrize("between", ["timeout", "other_key", "disarm", "inactive"])
def test_emergency_stop_does_not_join_unrelated_presses(monkeypatch, between):
    now = [10.0]
    monkeypatch.setattr(hotkey_module.time, "monotonic", lambda: now[0])
    hotkey = EmergencyHotkey(lambda owner: None)
    hotkey.set_armed(None if between == "inactive" else object())
    hotkey._key_event(ESCAPE, True, 0)
    hotkey._key_event(ESCAPE, False, 0)
    if between == "timeout":
        now[0] += 0.7
    elif between == "other_key":
        hotkey._key_event(0x41, True, 0)
    elif between == "disarm":
        hotkey.set_armed(None)
        hotkey.set_armed(object())
    else:
        hotkey.set_armed(object())
    hotkey._key_event(ESCAPE, True, 0)
    assert not hotkey.pending_owner


def test_emergency_stop_dispatch_never_blocks_keyboard_listener():
    entered = threading.Event()
    release = threading.Event()

    def stop(owner):
        entered.set()
        assert release.wait(1)

    hotkey = EmergencyHotkey(stop)
    hotkey._worker = threading.Thread(target=hotkey._dispatch)
    hotkey._worker.start()
    try:
        hotkey.set_armed(object())
        hotkey._key_event(ESCAPE, True, 0)
        hotkey._key_event(ESCAPE, False, 0)
        hotkey._key_event(ESCAPE, True, 0)
        assert entered.wait(0.5)
        assert hotkey.pending_owner
        # The listener can still process input while interruption is draining.
        hotkey._key_event(0x41, True, 0)
    finally:
        release.set()
        hotkey.close()
    assert not hotkey._worker.is_alive()
