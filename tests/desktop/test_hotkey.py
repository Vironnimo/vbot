"""Live voice hotkey validation, registration thread, and controller contract."""

from __future__ import annotations

import json
import queue
import threading
from pathlib import Path
from typing import Any

import pytest

from desktop import hotkey
from desktop.settings import DEFAULT_LIVE_HOTKEY_SETTINGS, read_live_hotkey_settings


def _setting(**changes: Any) -> dict[str, Any]:
    return {**DEFAULT_LIVE_HOTKEY_SETTINGS, **changes}


class FakeHotkeyApi:
    """Scripted Win32 seam recording the thread each call ran on."""

    def __init__(self, *, register_error: int = 0, layout_keys: dict[int, int] | None = None):
        self.register_error = register_error
        self.layout_keys = layout_keys or {}
        self.messages: queue.Queue[tuple[int, int] | None] = queue.Queue()
        self.calls: list[tuple[str, Any]] = []
        self.thread_ids: dict[str, int] = {}
        self.unregistered = threading.Event()

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

    def next_message(self) -> tuple[int, int] | None:
        return self.messages.get(timeout=5)

    def wake(self, thread_id: int) -> bool:
        self.calls.append(("wake", thread_id))
        self.messages.put(None)
        return True

    def registrations(self) -> list[tuple[int, int, int]]:
        return [args for name, args in self.calls if name == "register"]


# -- Pure validation -----------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "virtual_key"),
    [
        ("KeyA", 0x41),
        ("KeyZ", 0x5A),
        ("Digit0", 0x30),
        ("Digit9", 0x39),
        ("F1", 0x70),
        ("F24", 0x87),
        ("Space", 0x20),
    ],
)
def test_supported_keys_map_to_windows_virtual_keys(key: str, virtual_key: int) -> None:
    spec = hotkey.parse_hotkey(_setting(key=key))

    assert spec is not None
    assert spec.virtual_key == virtual_key


@pytest.mark.parametrize(
    "key",
    ["Enter", "KeyAA", "Keya", "Digit10", "F0", "F25", "F01", "ArrowUp", "", "Numpad1"],
)
def test_unsupported_keys_are_rejected(key: str) -> None:
    assert hotkey.parse_hotkey(_setting(key=key)) is None


def test_modifier_mask_always_disables_auto_repeat() -> None:
    spec = hotkey.parse_hotkey(_setting(ctrl=True, alt=False, shift=True, win=True, key="KeyL"))

    assert spec is not None
    assert spec.modifiers == (
        hotkey.MOD_NOREPEAT | hotkey.MOD_CONTROL | hotkey.MOD_SHIFT | hotkey.MOD_WIN
    )


def test_ordinary_keys_require_a_modifier_but_f13_to_f24_may_stand_alone() -> None:
    bare = {"ctrl": False, "alt": False, "shift": False, "win": False}

    assert hotkey.parse_hotkey(_setting(**bare, key="Space")) is None
    assert hotkey.parse_hotkey(_setting(**bare, key="F12")) is None
    assert hotkey.parse_hotkey(_setting(**bare, key="F13")) is not None
    assert hotkey.parse_hotkey(_setting(**bare, key="F24")) is not None


def test_non_boolean_modifier_is_rejected() -> None:
    assert hotkey.parse_hotkey(_setting(ctrl="yes")) is None


# -- Registration thread -------------------------------------------------------


def test_registration_thread_owns_register_loop_and_unregister() -> None:
    api = FakeHotkeyApi()
    pressed = threading.Event()
    thread = hotkey._HotkeyThread(
        hotkey.parse_hotkey(_setting(key="Space")),  # type: ignore[arg-type]
        pressed.set,
        api,
    )

    assert thread.start() is None
    api.messages.put((0x0100, 0))  # unrelated message
    api.messages.put((hotkey.WM_HOTKEY, 1))
    assert pressed.wait(timeout=2)
    thread.stop()

    assert api.unregistered.wait(timeout=2)
    assert api.thread_ids["prepare"] == api.thread_ids["register"] == api.thread_ids["unregister"]
    assert api.thread_ids["register"] != threading.get_native_id()
    assert api.registrations() == [
        (1, hotkey.MOD_NOREPEAT | hotkey.MOD_CONTROL | hotkey.MOD_ALT, 0x20)
    ]


def test_letter_follows_the_active_keyboard_layout() -> None:
    # A German layout reports VK_Z for the physical key that US calls KeyY.
    api = FakeHotkeyApi(layout_keys={0x15: 0x5A})
    thread = hotkey._HotkeyThread(
        hotkey.parse_hotkey(_setting(key="KeyY")),  # type: ignore[arg-type]
        lambda: None,
        api,
    )

    assert thread.start() is None
    thread.stop()

    assert api.registrations()[0][2] == 0x5A


@pytest.mark.parametrize(
    ("error", "code"),
    [(1409, hotkey.HOTKEY_ERROR_IN_USE), (5, hotkey.HOTKEY_ERROR_FAILED)],
)
def test_registration_errors_are_reported_without_a_loop(error: int, code: str) -> None:
    api = FakeHotkeyApi(register_error=error)
    thread = hotkey._HotkeyThread(
        hotkey.parse_hotkey(_setting()),  # type: ignore[arg-type]
        lambda: None,
        api,
    )

    assert thread.start() == code
    thread.stop()

    assert ("unregister", 1) not in api.calls
    assert not any(name == "wake" for name, _ in api.calls)


def test_press_handler_failure_keeps_the_hotkey_registered() -> None:
    api = FakeHotkeyApi()
    presses: list[int] = []

    def fail_once() -> None:
        presses.append(1)
        if len(presses) == 1:
            raise RuntimeError("boom")

    thread = hotkey._HotkeyThread(hotkey.parse_hotkey(_setting()), fail_once, api)  # type: ignore[arg-type]
    assert thread.start() is None
    api.messages.put((hotkey.WM_HOTKEY, 1))
    api.messages.put((hotkey.WM_HOTKEY, 1))
    api.messages.put(None)
    assert api.unregistered.wait(timeout=2)

    assert len(presses) == 2


# -- Controller ----------------------------------------------------------------


def _controller(
    tmp_path: Path,
    api: FakeHotkeyApi | None = None,
    *,
    supported: bool = True,
    on_press: Any = None,
) -> tuple[hotkey.LiveHotkeyController, list[FakeHotkeyApi]]:
    created: list[FakeHotkeyApi] = []

    def factory() -> FakeHotkeyApi:
        instance = api if api is not None and not created else FakeHotkeyApi()
        created.append(instance)
        return instance

    controller = hotkey.LiveHotkeyController(
        settings_path=tmp_path / "settings.json",
        on_press=on_press or (lambda: None),
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


def test_enabling_before_start_persists_and_registers_on_start(tmp_path: Path) -> None:
    controller, created = _controller(tmp_path)

    status = controller.update({"enabled": True})
    assert status["enabled"] is True
    assert created == []

    controller.start()
    assert len(created) == 1
    assert created[0].registrations()
    controller.stop()

    assert created[0].unregistered.wait(timeout=2)
    stored = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    assert stored["live_voice"]["hotkey"]["enabled"] is True


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


def test_in_use_combination_keeps_the_saved_setting_and_reports_error(tmp_path: Path) -> None:
    controller, created = _controller(tmp_path, FakeHotkeyApi(register_error=1409))
    controller.start()

    status = controller.update({"enabled": True, "key": "KeyK"})

    assert status["error_code"] == hotkey.HOTKEY_ERROR_IN_USE
    assert controller.status()["error_code"] == hotkey.HOTKEY_ERROR_IN_USE
    assert read_live_hotkey_settings(tmp_path / "settings.json")["key"] == "KeyK"
    assert read_live_hotkey_settings(tmp_path / "settings.json")["enabled"] is True
    controller.stop()


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


def test_press_reaches_the_controller_callback(tmp_path: Path) -> None:
    pressed = threading.Event()
    controller, created = _controller(tmp_path, on_press=pressed.set)
    controller.update({"enabled": True})
    controller.start()

    created[0].messages.put((hotkey.WM_HOTKEY, 1))

    assert pressed.wait(timeout=2)
    controller.stop()


def test_start_and_stop_are_idempotent(tmp_path: Path) -> None:
    controller, created = _controller(tmp_path)
    controller.update({"enabled": True})

    controller.start()
    controller.start()
    controller.stop()
    controller.stop()

    assert len(created) == 1
    assert created[0].unregistered.wait(timeout=2)
