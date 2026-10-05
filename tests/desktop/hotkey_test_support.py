"""Win32 hotkey double shared by the global hotkey and Desktop dictation tests."""

from __future__ import annotations

import queue
import threading
from typing import Any

from desktop import hotkey


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
