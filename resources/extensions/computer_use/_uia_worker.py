"""Private Windows UI Automation worker, launched by absolute file path.

COM objects never leave this MTA process. JSON carries observations and native
runtime identities; an action rechecks both identity and window ancestry.
"""

from __future__ import annotations

import ctypes
import json
import sys
import time
import uuid
from typing import Any

MAX_ELEMENTS = 256
MAX_TEXT = 4096
MAX_CONTENT = 24000
MAX_MESSAGE = 512_000


class RefusedError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class Automation:
    user32: Any

    def __init__(self) -> None:
        # comtypes initializes its importing thread as STA. This standalone
        # worker owns that apartment and switches it before creating any objects.
        import comtypes
        import comtypes.client

        comtypes.CoUninitialize()
        comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        comtypes.client.gen_dir = None  # Never write generated modules into an installation.
        self.uia = comtypes.client.GetModule("UIAutomationCore.dll")
        self.client = comtypes.client.CreateObject(
            self.uia.CUIAutomation8, interface=self.uia.IUIAutomation2
        )
        self.client.ConnectionTimeout = 1000
        self.client.TransactionTimeout = 1000
        self.walker = self.client.ControlViewWalker
        self.generation = uuid.uuid4().hex
        self.elements: dict[tuple[int, ...], Any] = {}
        if sys.platform != "win32":
            raise RuntimeError("Windows is required")
        self.user32 = ctypes.WinDLL("user32")
        self.user32.GetWindowThreadProcessId.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        self.user32.GetForegroundWindow.restype = ctypes.c_void_p
        self.user32.IsWindowEnabled.argtypes = [ctypes.c_void_p]

    def window(self, request: dict[str, Any], *, input_action: bool = False) -> Any:
        handle = int(request["handle"])
        pid = ctypes.c_uint32()
        thread = self.user32.GetWindowThreadProcessId(handle, ctypes.byref(pid))
        if not thread or pid.value != request["process_id"]:
            raise RefusedError(
                "window_unavailable",
                "The selected window closed or changed. Nothing was done. List windows again.",
            )
        if input_action and (
            int(self.user32.GetForegroundWindow() or 0) != handle
            or not self.user32.IsWindowEnabled(handle)
        ):
            raise RefusedError(
                "focus_changed",
                (
                    "The selected window is no longer ready for input. Nothing was "
                    "done. Take a screenshot."
                ),
            )
        return self.client.ElementFromHandle(handle)

    def pattern(self, element: Any, name: str) -> Any:
        pointer = element.GetCurrentPattern(getattr(self.uia, f"UIA_{name}PatternId"))
        return (
            pointer.QueryInterface(getattr(self.uia, f"IUIAutomation{name}Pattern"))
            if pointer
            else None
        )

    @staticmethod
    def identity(element: Any) -> list[int]:
        return list(element.GetRuntimeId())

    def describe(self, element: Any, depth: int, *, content: bool = True) -> dict[str, Any]:
        password = bool(element.CurrentIsPassword)
        box = element.CurrentBoundingRectangle
        result = {
            "runtime_id": self.identity(element),
            "generation": self.generation,
            "name": str(element.CurrentName or "")[:512],
            "role": str(element.CurrentLocalizedControlType or "")[:128],
            "control_type": int(element.CurrentControlType),
            "automation_id": str(element.CurrentAutomationId or "")[:512],
            "bounds": [box.left, box.top, box.right, box.bottom],
            "depth": depth,
            "focused": bool(element.CurrentHasKeyboardFocus),
            "enabled": bool(element.CurrentIsEnabled),
            "password": password,
            "offscreen": bool(element.CurrentIsOffscreen),
            "actions": [],
        }
        if password:
            # A misbehaving provider can put the value in Name as well.
            result["name"] = "Password field"
            return result
        actions = []
        if result["enabled"] and not result["offscreen"]:
            if bool(element.CurrentIsKeyboardFocusable):
                actions.append("focus")
            if bool(
                element.GetCurrentPropertyValue(self.uia.UIA_IsInvokePatternAvailablePropertyId)
            ):
                actions.append("invoke")
        if bool(element.GetCurrentPropertyValue(self.uia.UIA_IsValuePatternAvailablePropertyId)):
            value = self.pattern(element, "Value")
            if value:
                if content:
                    result["value"] = str(value.CurrentValue or "")[:MAX_TEXT]
                if result["enabled"] and not result["offscreen"] and not value.CurrentIsReadOnly:
                    actions.append("set_value")
        if content and bool(
            element.GetCurrentPropertyValue(self.uia.UIA_IsTextPatternAvailablePropertyId)
        ):
            text = self.pattern(element, "Text")
            if text:
                result["text"] = str(text.DocumentRange.GetText(MAX_TEXT))
                selected = text.GetSelection()
                result["selected_text"] = "\n".join(
                    str(selected.GetElement(index).GetText(MAX_TEXT))
                    for index in range(min(selected.Length, 4))
                )[:MAX_TEXT]
        result["actions"] = actions
        return result

    def inspect(self, request: dict[str, Any]) -> dict[str, Any]:
        root = self.window(request)
        deadline = time.monotonic() + 3
        result: list[dict[str, Any]] = []
        pending = [(root, 0)]
        count = 0
        content_size = 0
        truncated = False
        while (
            pending
            and count < MAX_ELEMENTS
            and content_size < MAX_CONTENT
            and time.monotonic() < deadline
        ):
            element, depth = pending.pop()
            count += 1
            item = self.describe(element, depth, content=content_size < MAX_CONTENT)
            if not item["offscreen"]:
                content_size += sum(
                    len(str(item.get(key, "")))
                    for key in ("name", "value", "text", "selected_text")
                )
                result.append(item)
                self.elements[tuple(item["runtime_id"])] = element
            if depth < 24 and not item["password"]:
                child = self.walker.GetFirstChildElement(element)
                children: list[tuple[Any, int]] = []
                while (
                    child
                    and len(children) + count + len(pending) < MAX_ELEMENTS
                    and time.monotonic() < deadline
                ):
                    children.append((child, depth + 1))
                    child = self.walker.GetNextSiblingElement(child)
                pending.extend(reversed(children))
                truncated |= bool(child)
            elif depth >= 24:
                truncated = True
        while len(self.elements) > 1024:
            del self.elements[next(iter(self.elements))]
        return {
            "elements": result,
            "truncated": truncated or bool(pending) or content_size >= MAX_CONTENT,
        }

    def resolve(self, request: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
        reference = request["reference"]
        if reference["generation"] != self.generation:
            raise RefusedError(
                "element_unavailable",
                "The element observation expired. Nothing was done. Inspect the window again.",
            )
        element = self.elements.get(tuple(reference["runtime_id"]))
        if element is None:
            raise RefusedError(
                "element_unavailable",
                "The element is no longer available. Nothing was done. Inspect the window again.",
            )
        root = self.window(request, input_action=request["operation"] == "act")
        current = self.describe(element, int(reference["depth"]), content=False)
        if any(
            current[key] != reference[key]
            for key in ("runtime_id", "name", "control_type", "automation_id", "password")
        ):
            raise RefusedError(
                "element_unavailable",
                "The element changed since inspection. Nothing was done. Inspect the window again.",
            )
        parent = element
        root_id = self.identity(root)
        for _ in range(64):
            if not parent:
                break
            if self.identity(parent) == root_id:
                return element, current
            parent = self.walker.GetParentElement(parent)
        raise RefusedError(
            "element_unavailable",
            (
                "The element no longer belongs to the selected window. Nothing "
                "was done. Inspect again."
            ),
        )

    def call(self, request: dict[str, Any]) -> dict[str, Any]:
        if request["operation"] == "inspect":
            return self.inspect(request)
        element, current = self.resolve(request)
        if request["operation"] == "resolve":
            return current
        action = request["action"]
        if action not in current["actions"]:
            raise RefusedError(
                "element_not_supported",
                f"The element does not support {action}. Nothing was done. "
                "Use the screenshot and mouse or keyboard instead.",
            )
        # Resolving ancestry and patterns can take time; refuse a foreground
        # switch that occurred during those reads before invoking the provider.
        self.window(request, input_action=True)
        if action == "focus":
            element.SetFocus()
        elif action == "invoke":
            self.pattern(element, "Invoke").Invoke()
        elif action == "set_value":
            self.pattern(element, "Value").SetValue(request["text"])
        return {}


def main() -> None:
    automation = Automation()
    while line := sys.stdin.readline(MAX_MESSAGE + 1):
        request: dict[str, Any] = {}
        try:
            if len(line) > MAX_MESSAGE:
                return
            request = json.loads(line)
            result = automation.call(request)
        except RefusedError as error:
            result = {"error": str(error), "code": error.code}
        except Exception:
            # Provider errors can follow an applied mutation; do not claim otherwise.
            mutation = request.get("operation") == "act"
            result = {
                "error": (
                    "UI Automation failed; the action's outcome is unknown. Check the "
                    "screenshot before repeating input, or inspect again for fresh "
                    "elements."
                )
                if mutation
                else (
                    "UI Automation could not read this window. No input was sent. "
                    "Use screenshot coordinates or inspect again."
                ),
                "code": "computer_use_failed" if mutation else "accessibility_unavailable",
            }
        print(json.dumps(result, ensure_ascii=True), flush=True)


if __name__ == "__main__":
    main()
