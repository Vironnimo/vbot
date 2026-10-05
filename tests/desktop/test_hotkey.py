"""Global hotkey validation, stored preference, and the controller's registration contract."""

from __future__ import annotations

import json
import queue
import threading
from pathlib import Path
from typing import Any

import pytest

from desktop import hotkey

CTRL_ALT = hotkey.MOD_NOREPEAT | hotkey.MOD_CONTROL | hotkey.MOD_ALT
BARE = {"ctrl": False, "alt": False, "shift": False, "win": False}


def _setting(**changes: Any) -> dict[str, Any]:
    return {**hotkey.LIVE_VOICE_HOTKEY.defaults, **changes}


def _stored(tmp_path: Path) -> dict[str, Any]:
    return hotkey.read_hotkey_setting(hotkey.LIVE_VOICE_HOTKEY, tmp_path / "settings.json")


class FakeHotkeyApi:
    """Scripted Win32 seam recording the thread each call ran on."""

    def __init__(self, *, register_error: int = 0, layout_keys: dict[int, int] | None = None):
        self.register_error = register_error
        self.layout_keys = layout_keys or {}
        self.messages: queue.Queue[tuple[int, int] | None] = queue.Queue()
        self.calls: list[tuple[str, Any]] = []
        self.thread_ids: dict[str, int] = {}
        self.unregistered = threading.Event()
        self.down: set[int] = set()

    def prepare_thread(self) -> None:
        self.thread_ids["prepare"] = threading.get_native_id()
        self.calls.append(("prepare", None))

    def layout_virtual_key(self, scan_code: int) -> int:
        return self.layout_keys.get(scan_code, 0)

    def register(self, hotkey_id: int, modifiers: int, virtual_key: int) -> int:
        self.thread_ids["register"] = threading.get_native_id()
        self.calls.append(("register", (hotkey_id, modifiers, virtual_key)))
        return self.register_error

    def unregister(self, hotkey_id: int) -> None:
        self.thread_ids["unregister"] = threading.get_native_id()
        self.calls.append(("unregister", hotkey_id))
        self.unregistered.set()

    def next_message(self, timeout: float | None) -> tuple[int, int] | None:
        try:
            return self.messages.get(timeout=5 if timeout is None else timeout)
        except queue.Empty:
            if timeout is None:
                raise
            return hotkey.WM_NULL, 0

    def key_down(self, virtual_key: int) -> bool:
        return virtual_key in self.down

    def post(self, thread_id: int, message: int, wparam: int) -> bool:
        self.calls.append(("post", message))
        self.messages.put(None if message == hotkey.WM_QUIT else (message, wparam))
        return True

    def registrations(self) -> list[tuple[int, int, int]]:
        return [args for name, args in self.calls if name == "register"]


# -- Combination validation ----------------------------------------------------


@pytest.mark.parametrize(
    ("changes", "virtual_key"),
    [
        ({"key": "KeyA"}, 0x41),
        ({"key": "KeyZ"}, 0x5A),
        ({"key": "Digit0"}, 0x30),
        ({"key": "Digit9"}, 0x39),
        ({"key": "F1"}, 0x70),
        ({"key": "F24"}, 0x87),
        ({"key": "Space"}, 0x20),
        ({**BARE, "key": "F13"}, 0x7C),
        ({**BARE, "key": "F24"}, 0x87),
    ],
)
def test_supported_combinations_map_to_windows_virtual_keys(
    changes: dict[str, Any], virtual_key: int
) -> None:
    spec = hotkey.parse_hotkey(_setting(**changes))

    assert spec is not None
    assert spec.virtual_key == virtual_key


@pytest.mark.parametrize(
    "changes",
    [
        *(
            {"key": key}
            for key in ("Enter", "KeyAA", "Keya", "Digit10", "F0", "F25", "F01", "F\u00b2")
        ),
        *({"key": key} for key in ("ArrowUp", "", "Numpad1")),
        {**BARE, "key": "Space"},  # ordinary keys need a modifier
        {**BARE, "key": "F12"},
        {"ctrl": "yes"},
    ],
)
def test_unsupported_combinations_are_rejected(changes: dict[str, Any]) -> None:
    assert hotkey.parse_hotkey(_setting(**changes)) is None


def test_modifier_mask_always_disables_auto_repeat() -> None:
    spec = hotkey.parse_hotkey(_setting(ctrl=True, alt=False, shift=True, win=True, key="KeyL"))

    assert spec is not None
    assert spec.modifiers == (
        hotkey.MOD_NOREPEAT | hotkey.MOD_CONTROL | hotkey.MOD_SHIFT | hotkey.MOD_WIN
    )


# -- Controller ----------------------------------------------------------------


def _controller(
    tmp_path: Path,
    api: FakeHotkeyApi | None = None,
    *,
    supported: bool = True,
    on_press: Any = None,
    on_release: Any = None,
    on_escape: Any = None,
) -> tuple[hotkey.HotkeyController, list[FakeHotkeyApi]]:
    created: list[FakeHotkeyApi] = []

    def factory() -> FakeHotkeyApi:
        instance = api if api is not None and not created else FakeHotkeyApi()
        created.append(instance)
        return instance

    controller = hotkey.HotkeyController(
        preference=hotkey.LIVE_VOICE_HOTKEY,
        settings_path=tmp_path / "settings.json",
        handlers=hotkey.HotkeyHandlers(
            on_press=on_press or (lambda: None), on_release=on_release, on_escape=on_escape
        ),
        supported=supported,
        api_factory=factory,
    )
    return controller, created


def test_default_status_is_disabled_ctrl_alt_space(tmp_path: Path) -> None:
    controller, created = _controller(tmp_path)

    controller.start()

    assert controller.status() == {
        "supported": True,
        "enabled": False,
        "hotkey": {"ctrl": True, "alt": True, "shift": False, "win": False, "key": "Space"},
        "error_code": None,
    }
    assert created == []
    controller.stop()


def test_enabling_before_start_persists_and_registers_on_its_own_thread_until_stopped(
    tmp_path: Path,
) -> None:
    controller, created = _controller(tmp_path)

    status = controller.update({"enabled": True})
    assert status["enabled"] is True
    assert created == []

    controller.start()
    api = created[0]
    assert api.registrations() == [(1, CTRL_ALT, 0x20)]
    controller.stop()

    assert api.unregistered.wait(timeout=2)
    # Win32 binds a hotkey to the registering thread, so one thread owns its lifetime.
    assert api.thread_ids["prepare"] == api.thread_ids["register"] == api.thread_ids["unregister"]
    assert api.thread_ids["register"] != threading.get_native_id()
    stored = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    assert stored["live_voice"]["hotkey"]["enabled"] is True
    # A start that arrives during shutdown (the shown callback outlasting the
    # window) never registers the hotkey again.
    controller.start()
    assert len(created) == 1


def test_letters_follow_the_active_keyboard_layout(tmp_path: Path) -> None:
    # A German layout reports VK_Z for the physical key that US calls KeyY.
    controller, created = _controller(tmp_path, FakeHotkeyApi(layout_keys={0x15: 0x5A}))
    controller.start()

    controller.update({"enabled": True, "key": "KeyY"})

    assert created[0].registrations() == [(1, CTRL_ALT, 0x5A)]
    controller.stop()


def test_changing_the_combination_replaces_the_registration(tmp_path: Path) -> None:
    controller, created = _controller(tmp_path)
    controller.update({"enabled": True})
    controller.start()

    status = controller.update({"key": "KeyL", "shift": True})

    assert status["error_code"] is None
    assert status["hotkey"]["key"] == "KeyL"
    assert len(created) == 2
    assert created[0].unregistered.wait(timeout=2)
    assert created[1].registrations()[0][1] & hotkey.MOD_SHIFT
    controller.stop()
    assert created[1].unregistered.wait(timeout=2)


@pytest.mark.parametrize(
    ("error", "code"),
    [(1409, hotkey.HOTKEY_ERROR_IN_USE), (5, hotkey.HOTKEY_ERROR_FAILED)],
)
def test_a_failed_registration_keeps_the_saved_setting_and_reports_its_error(
    tmp_path: Path, error: int, code: str
) -> None:
    api = FakeHotkeyApi(register_error=error)
    controller, _created = _controller(tmp_path, api)
    controller.start()

    status = controller.update({"enabled": True, "key": "KeyK"})

    assert status["error_code"] == code
    assert controller.status()["error_code"] == code
    assert _stored(tmp_path)["key"] == "KeyK"
    assert _stored(tmp_path)["enabled"] is True
    controller.stop()
    assert ("unregister", 1) not in api.calls
    assert not any(name == "post" for name, _ in api.calls)


@pytest.mark.parametrize(
    "changes",
    [
        {"enabled": True, "ctrl": False, "alt": False},
        {"key": "Enter"},
        {"enabled": "yes"},
        {"key": 5},
        "Ctrl+Alt+Space",
    ],
)
def test_invalid_updates_are_rejected_without_persisting(tmp_path: Path, changes: Any) -> None:
    controller, created = _controller(tmp_path)
    controller.start()

    status = controller.update(changes)

    assert status["error_code"] == hotkey.HOTKEY_ERROR_INVALID
    assert status["enabled"] is False
    assert not (tmp_path / "settings.json").exists()
    assert created == []
    controller.stop()


def test_disabling_always_succeeds_even_with_an_invalid_saved_combination(
    tmp_path: Path,
) -> None:
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps({"live_voice": {"hotkey": {"enabled": True, "key": "Enter"}}}),
        encoding="utf-8",
    )
    controller, created = _controller(tmp_path)
    controller.start()
    assert controller.status()["error_code"] == hotkey.HOTKEY_ERROR_INVALID

    status = controller.update({"enabled": False})

    assert status["enabled"] is False
    assert status["error_code"] is None
    assert created == []
    controller.stop()


def test_unsupported_platform_persists_without_registering(tmp_path: Path) -> None:
    controller, created = _controller(tmp_path, supported=False)
    controller.start()

    status = controller.update({"enabled": True})

    assert status == {
        "supported": False,
        "enabled": True,
        "hotkey": {"ctrl": True, "alt": True, "shift": False, "win": False, "key": "Space"},
        "error_code": None,
    }
    assert created == []
    controller.stop()


def test_each_press_reaches_the_callback_even_after_a_failing_one(tmp_path: Path) -> None:
    presses: list[int] = []
    second_press = threading.Event()

    def fail_once() -> None:
        presses.append(1)
        if len(presses) == 1:
            raise RuntimeError("boom")
        second_press.set()

    controller, created = _controller(tmp_path, on_press=fail_once)
    controller.update({"enabled": True})
    controller.start()

    created[0].messages.put((0x0100, 0))  # an unrelated message is ignored
    created[0].messages.put((hotkey.WM_HOTKEY, 1))
    created[0].messages.put((hotkey.WM_HOTKEY, 1))

    assert second_press.wait(timeout=2)
    assert len(presses) == 2
    assert controller.status()["error_code"] is None
    controller.stop()
    assert created[0].unregistered.wait(timeout=2)


def test_a_message_loop_that_fails_unregisters_and_stops_harmlessly_later(
    tmp_path: Path,
) -> None:
    controller, created = _controller(tmp_path)
    controller.update({"enabled": True})
    controller.start()

    created[0].messages.put(None)  # GetMessage failed: the loop ends on its own

    assert created[0].unregistered.wait(timeout=2)
    for thread in _hotkey_threads():
        thread.join(timeout=2)
    controller.stop()
    assert [name for name, _ in created[0].calls].count("unregister") == 1


def _hotkey_threads() -> list[threading.Thread]:
    return [thread for thread in threading.enumerate() if thread.name == "vbot-live-hotkey"]


def test_start_and_stop_are_idempotent(tmp_path: Path) -> None:
    controller, created = _controller(tmp_path)
    controller.update({"enabled": True})

    controller.start()
    controller.start()
    controller.stop()
    controller.stop()

    assert len(created) == 1
    assert created[0].unregistered.wait(timeout=2)


def _wait(predicate: Any, timeout: float = 2.0) -> None:
    done = threading.Event()
    deadline = threading.Timer(timeout, done.set)
    deadline.start()
    try:
        while not predicate():
            assert not done.wait(0.005), "condition not reached in time"
    finally:
        deadline.cancel()


def test_a_held_combination_reports_its_release_and_escape_while_held(tmp_path: Path) -> None:
    events: list[str] = []
    controller, created = _controller(
        tmp_path,
        on_press=lambda: events.append("press"),
        on_release=lambda: events.append("release"),
        on_escape=lambda: events.append("escape"),
    )
    controller.update({"enabled": True})
    controller.start()
    api = created[0]
    api.down |= {0x20, hotkey.VK_CONTROL, hotkey.VK_MENU}

    api.messages.put((hotkey.WM_HOTKEY, hotkey.HOTKEY_ID))
    _wait(lambda: events == ["press"])
    # Escape with the modifiers still held is not the registered bare Escape.
    api.down.add(hotkey.VK_ESCAPE)
    _wait(lambda: events == ["press", "escape"])
    api.down.discard(hotkey.VK_MENU)  # letting go of any key of the combination ends the hold
    _wait(lambda: events == ["press", "escape", "release"])

    controller.stop()
    assert api.unregistered.wait(timeout=2)
    assert events == ["press", "escape", "release"]


def test_escape_is_claimed_only_while_armed_and_survives_a_new_combination(
    tmp_path: Path,
) -> None:
    escapes: list[int] = []
    escape = (hotkey.ESCAPE_HOTKEY_ID, hotkey.MOD_NOREPEAT, hotkey.VK_ESCAPE)
    controller, created = _controller(tmp_path, on_escape=lambda: escapes.append(1))
    controller.update({"enabled": True})
    controller.start()

    controller.arm_escape(True)
    _wait(lambda: escape in created[0].registrations())
    created[0].messages.put((hotkey.WM_HOTKEY, hotkey.ESCAPE_HOTKEY_ID))
    _wait(lambda: escapes == [1])
    assert created[0].thread_ids["register"] != threading.get_native_id()

    controller.update({"key": "KeyL"})
    assert escape in created[1].registrations()
    assert ("unregister", hotkey.ESCAPE_HOTKEY_ID) in created[0].calls

    controller.arm_escape(False)
    _wait(lambda: ("unregister", hotkey.ESCAPE_HOTKEY_ID) in created[1].calls)
    controller.stop()
    assert created[1].unregistered.wait(timeout=2)


def test_arming_escape_without_a_handler_claims_nothing(tmp_path: Path) -> None:
    controller, created = _controller(tmp_path)
    controller.update({"enabled": True})
    controller.start()

    controller.arm_escape(True)
    controller.stop()

    assert created[0].unregistered.wait(timeout=2)
    assert created[0].registrations() == [(hotkey.HOTKEY_ID, CTRL_ALT, 0x20)]


@pytest.mark.parametrize(
    ("live_voice", "expected"),
    [
        (None, {}),
        ([], {}),
        ({"hotkey": "Ctrl+Alt+Space"}, {}),
        (
            {"hotkey": {"enabled": True, "ctrl": "yes", "alt": False, "key": " KeyL "}},
            {"enabled": True, "alt": False, "key": "KeyL"},
        ),
    ],
    ids=["unset", "not-an-object", "not-a-hotkey-object", "per-field-fallback"],
)
def test_the_stored_hotkey_falls_back_per_field_to_disabled_ctrl_alt_space(
    tmp_path: Path, live_voice: object, expected: dict[str, Any]
) -> None:
    (tmp_path / "settings.json").write_text(
        json.dumps({"live_voice": live_voice}), encoding="utf-8"
    )

    assert _stored(tmp_path) == {
        "enabled": False,
        "ctrl": True,
        "alt": True,
        "shift": False,
        "win": False,
        "key": "Space",
        **expected,
    }
