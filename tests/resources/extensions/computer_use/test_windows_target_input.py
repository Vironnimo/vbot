"""Native input cleanup with an in-memory SendInput transport; never sends real input."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from resources.extensions.computer_use import _win_input
from resources.extensions.computer_use.target import InputInterrupted, TargetError


class InputDesktop:
    def __init__(self, monkeypatch):
        self.held = set()
        self.rejected = []
        self.reject_releases = 0
        self.reject_kind = None
        self.on_press = lambda: None
        self.pointer = (100, 100)
        self.target = _win_input.WindowsInput()
        user32 = SimpleNamespace(
            SendInput=self.send,
            GetForegroundWindow=lambda: 1,
            GetWindowThreadProcessId=lambda *_: 1,
            GetKeyboardLayout=lambda _: 1,
            MapVirtualKeyExW=lambda vk, *_: vk,
            GetDoubleClickTime=lambda: 500,
            GetCursorPos=self.cursor,
            SetCursorPos=lambda *_: 1,
        )
        monkeypatch.setattr(_win_input._win32, "api", lambda: SimpleNamespace(user32=user32))
        monkeypatch.setattr(_win_input._win32, "enter_thread", lambda: None)
        monkeypatch.setattr(_win_input._win32, "virtual_screen", lambda: (0, 0, 640, 480))
        monkeypatch.setattr(_win_input._win32, "buttons_swapped", lambda: False)
        monkeypatch.setattr(_win_input, "_input_method_open", lambda _: True)
        monkeypatch.setattr(self.target, "_pause", lambda _: self.target._check())

    def cursor(self, point):
        point._obj.x, point._obj.y = self.pointer
        return 1

    def send(self, count, events, _size):
        for index, event in enumerate(events):
            if event.type == 1:
                key = ("key", event.key.vk, event.key.scan)
                up = bool(event.key.flags & 2)
            elif event.mouse.flags & 6:
                key = ("button", "left")
                up = bool(event.mouse.flags & 4)
            else:
                continue
            if up and self.reject_releases and self.reject_kind in (None, key[0]):
                self.reject_releases -= 1
                self.rejected.append(key)
                return index
            if up:
                self.held.discard(key)
            else:
                self.held.add(key)
                self.on_press()
        return count

    def act(self, action):
        target = self.target
        match action:
            case "keys":
                target.keys(["ctrl", "shift", "enter"], 1)
            case "hold":
                target.hold(["ctrl", "shift"], 1)
            case "type":
                target.type_text("\U0001f600")  # both UTF-16 code units must be released
            case "click":
                target.click(100, 100, "left", 1, ["ctrl"])
            case "drag":
                target.drag([(100, 100), (101, 101)], 0, ["ctrl"])
            case "scroll":
                target.scroll(100, 100, "down", 1, ["ctrl"])
            case "activate":
                target._tap_unbound_key()
            case _:
                raise AssertionError(action)


@pytest.mark.parametrize(
    ("action", "kind"),
    [(action, "key") for action in ("keys", "hold", "type", "click", "drag", "scroll", "activate")]
    + [("click", "button"), ("drag", "button")],
)
def test_input_release_failure_is_reported_and_all_pressed_input_is_cleaned_up(
    monkeypatch, action, kind
):
    desktop = InputDesktop(monkeypatch)
    desktop.reject_releases = 1
    desktop.reject_kind = kind
    with pytest.raises(TargetError) as failed:
        desktop.act(action)
    assert failed.value.code == "computer_use_unavailable"
    assert desktop.rejected
    assert not desktop.held and not desktop.target._held


def test_refused_key_release_remains_tracked_until_windows_accepts_it(monkeypatch):
    desktop = InputDesktop(monkeypatch)
    desktop.reject_releases = 100
    with pytest.raises(TargetError):
        desktop.act("keys")
    assert desktop.held and desktop.target._held
    desktop.reject_releases = 0
    desktop.target.release_all()
    assert not desktop.held and not desktop.target._held


@pytest.mark.parametrize("action", ["keys", "type", "click", "activate"])
def test_stop_after_a_press_releases_input_without_masking_the_interruption(monkeypatch, action):
    desktop = InputDesktop(monkeypatch)
    desktop.on_press = desktop.target._stop.set
    with pytest.raises(InputInterrupted):
        desktop.act(action)
    assert not desktop.held and not desktop.target._held


def test_typing_preserves_an_intentionally_held_mouse_button(monkeypatch):
    desktop = InputDesktop(monkeypatch)
    desktop.target.button("left", True)
    desktop.act("keys")
    assert desktop.held == {("button", "left")}
    desktop.target.button("left", False)
    assert not desktop.held and not desktop.target._held
