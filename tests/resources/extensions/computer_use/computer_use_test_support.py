"""Computer Use test support: an in-memory desktop, the registered Tools and call helpers.

``FakeTarget`` implements ``DesktopTarget`` with two displays, windows of
several apps and recorded input. ``Harness`` registers the
Extension like the runtime does and runs calls through production dispatch.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import threading
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MethodType, SimpleNamespace
from typing import Any, cast

import pytest
import pytest_asyncio
from PIL import Image, ImageDraw

from core.extensions.extensions import ExtensionAPI, ExtensionDeclarations
from core.extensions.operations import ExtensionHost
from core.providers.adapter import tool_result_text
from core.tools import ToolContext, ToolRegistry
from core.tools.availability import ToolAccess
from core.tools.tools import tool_failure_for_exception
from resources.extensions.computer_use import extension as computer_use
from resources.extensions.computer_use.target import (
    AppInfo,
    Display,
    InputInterrupted,
    TargetError,
    WindowInfo,
)

TOOLS = ("computer", "computer_batch", "computer_apps")
BACKGROUND = (20, 20, 20)
GRAY = (128, 128, 128)


def app(name: str, *, running: bool = True) -> AppInfo:
    return AppInfo(name, frozenset({f"exe:{name.lower()}.exe"}), running, True)


NOTEPAD = app("Notepad")
CHROME = app("Google Chrome")
TERMINAL = app("Windows Terminal")
SLACK = app("Slack")
PAINT = app("Paint")
EXPLORER = app("File Explorer")
VBOT = app("vBot")
CALCULATOR = app("Calculator", running=False)
CALC = app("LibreOffice Calc", running=False)

# The primary display needs no scaling; the secondary one maps each frame pixel to 2x2.
PRIMARY = Display("\\\\.\\DISPLAY1", "Main", 0, 0, 1280, 720, True, 100)
WIDE = Display("\\\\.\\DISPLAY2", "Wide", -3136, 0, 3136, 1000, False, 150)


def window(handle: int, application: AppInfo, box: tuple[int, int, int, int]) -> WindowInfo:
    return WindowInfo(handle, f"{application.name} window", application, *box, False)


def color(handle: int) -> tuple[int, int, int]:
    """The distinct color the fake capture paints window *handle* with."""
    return (40 + handle * 20, 200 - handle * 15, 60 + handle * 10)


@dataclass
class FakeTarget:
    """An in-memory ``DesktopTarget``: windows topmost first, input recorded in order."""

    displays_: list[Display] = field(default_factory=lambda: [PRIMARY, WIDE])
    windows_: list[WindowInfo] = field(
        default_factory=lambda: [
            window(1, NOTEPAD, (100, 100, 600, 500)),
            window(2, CHROME, (700, 100, 1200, 500)),
            window(3, TERMINAL, (100, 520, 600, 700)),
            window(4, SLACK, (700, 520, 1200, 700)),
            window(5, VBOT, (1210, 0, 1280, 100)),
            window(6, PAINT, (-3000, 100, -1000, 900)),
            window(7, EXPLORER, (-3136, 0, 1280, 1000)),
        ]
    )
    apps_: list[AppInfo] = field(
        default_factory=lambda: [
            NOTEPAD,
            CHROME,
            TERMINAL,
            SLACK,
            PAINT,
            EXPLORER,
            VBOT,
            CALCULATOR,
            CALC,
        ]
    )
    pointer: tuple[int, int] = (640, 360)
    reason: str | None = None
    inputs: list[tuple[Any, ...]] = field(default_factory=list)
    opened: list[str] = field(default_factory=list)
    released: int = 0
    # Each set_activity call: True shows the activity sign, False hides it.
    activity: list[bool] = field(default_factory=list)
    closed: bool = False
    stop_event: threading.Event | None = None
    # Called with each input record before it is recorded, on the worker thread.
    on_input: Callable[[tuple[Any, ...]], None] | None = None
    fail: dict[str, TargetError] = field(default_factory=dict)

    def readiness(self) -> str | None:
        return self.reason

    def displays(self) -> list[Display]:
        return list(self.displays_)

    def capture(self, display: Display) -> Image.Image:
        image = Image.new("RGB", (display.width, display.height), BACKGROUND)
        draw = ImageDraw.Draw(image)
        for item in reversed(self.windows_):
            box = (
                item.left - display.left,
                item.top - display.top,
                item.right - display.left - 1,
                item.bottom - display.top - 1,
            )
            draw.rectangle(box, fill=color(item.handle))
        return image

    def windows(self) -> list[WindowInfo]:
        return list(self.windows_)

    def foreground(self) -> WindowInfo:
        return self.windows_[0]

    def window_at(self, x: int, y: int) -> WindowInfo | None:
        return next((item for item in self.windows_ if item.contains(x, y)), None)

    def apps(self) -> list[AppInfo]:
        return list(self.apps_)

    def open(self, application: AppInfo) -> None:
        self.opened.append(application.name)
        self.front(application)

    def front(self, application: AppInfo) -> None:
        """Bring the topmost window of *application* to the front."""
        item = next(item for item in self.windows_ if item.app.matches(application))
        self.windows_.remove(item)
        self.windows_.insert(0, item)

    def cursor(self) -> tuple[int, int]:
        return self.pointer

    def _input(self, *record: Any) -> None:
        if record[0] in self.fail:
            raise self.fail[record[0]]
        if self.on_input is not None:
            self.on_input(record)
        if self.stop_event is not None and self.stop_event.is_set():
            raise InputInterrupted()
        self.inputs.append(record)

    def move(self, x: int, y: int) -> None:
        self._input("move", x, y)
        self.pointer = (x, y)

    def button(self, button: str, down: bool) -> None:
        self._input("button", button, down)

    def click(self, x: int, y: int, button: str, count: int, modifiers: list[str]) -> None:
        self._input("click", x, y, button, count, modifiers)
        self.pointer = (x, y)

    def drag(self, path: list[tuple[int, int]], seconds: float, modifiers: list[str]) -> None:
        self._input("drag", path, seconds, modifiers)
        self.pointer = path[-1]

    def scroll(self, x: int, y: int, direction: str, amount: int, modifiers: list[str]) -> None:
        self._input("scroll", x, y, direction, amount, modifiers)

    def keys(self, chord: list[str], repeat: int) -> None:
        self._input("keys", chord, repeat)

    def hold(self, chord: list[str], seconds: float) -> None:
        self._input("hold", chord, seconds)

    def type_text(self, text: str) -> None:
        self._input("type", text)

    def release_all(self) -> None:
        self.released += 1

    def set_stop_event(self, event: threading.Event) -> None:
        self.stop_event = event

    def set_activity(self, active: bool) -> None:
        self.activity.append(active)

    def close(self) -> None:
        self.closed = True


class FakeHotkey:
    def __init__(self, callback: Callable[[object], None]) -> None:
        self.callback = callback
        self.available = True
        self.armed: object | None = None
        self.started = False
        self.closed = False

    def start(self) -> None:
        self.started = True

    def set_armed(self, owner: object | None) -> None:
        self.armed = owner

    def close(self) -> None:
        self.closed = True

    def press_double_escape(self) -> None:
        if self.armed is not None:
            self.callback(self.armed)


@dataclass
class Harness:
    """The registered Extension over a ``FakeTarget``, called through production dispatch."""

    service: computer_use.ComputerUseService
    api: ExtensionAPI
    registry: ToolRegistry
    target: FakeTarget
    hotkeys: list[FakeHotkey]
    agent: Any
    context: ToolContext
    published: list[tuple[str, list[str], int]]
    sleeps: list[float]

    @property
    def hotkey(self) -> FakeHotkey:
        """The double-Esc hotkey, which the first call that takes the desktop starts."""
        (hotkey,) = self.hotkeys
        return hotkey

    def context_for(self, tool: str, **changes: Any) -> ToolContext:
        return replace(self.context, tool_name=tool, result_media=[], **changes)

    async def call(
        self, tool: str, arguments: Any, context: ToolContext | None = None
    ) -> dict[str, Any]:
        """Dispatch one call like the runtime: normalizer, validation, handler."""
        context = context or self.context_for(tool)
        try:
            return await self.registry.dispatch(context, arguments, list(TOOLS))
        except Exception as error:
            return tool_failure_for_exception(tool, error)

    async def computer(self, **arguments: Any) -> dict[str, Any]:
        return await self.call("computer", arguments)

    def ask_per_app(self) -> None:
        """Turn on the setting that makes the user approve each app per Session."""
        self.api.config[computer_use.ASK_SETTING] = True

    async def grant(self, *names: str, answer: str = "accept") -> dict[str, Any]:
        """Request *names* in ask mode and answer the pending input as the user."""
        task = asyncio.ensure_future(
            self.call("computer_apps", {"action": "request", "apps": list(names), "reason": "Test"})
        )
        request = await self.pending()
        await self.api.operations.invoke(
            "respond", {"request_id": request["id"], "response": {"action": answer}}
        )
        return await task

    async def pending(self) -> dict[str, Any]:
        """Wait for the next pending access request and return it."""
        for _ in range(1000):
            requests = self.api.operations.pending_inputs()  # type: ignore[misc]
            if requests:
                return requests[0]
            await asyncio.sleep(0.001)
        raise AssertionError("no access request appeared")


def registered_service(declarations: ExtensionDeclarations) -> computer_use.ComputerUseService:
    """The service behind the registered Tool handlers."""
    handler = declarations.tools[0].handler
    assert isinstance(handler, MethodType)
    service = handler.__self__
    assert isinstance(service, computer_use.ComputerUseService)
    return service


def model_text(result: dict[str, Any]) -> str:
    """Return the plain text the Model reads for a Tool Result."""
    return str(tool_result_text(json.dumps(result)))


def images(context: ToolContext) -> list[Image.Image]:
    """The images a call attached for the Model, in order."""
    return [
        Image.open(io.BytesIO(base64.b64decode(item["base64"]))) for item in context.result_media
    ]


@pytest_asyncio.fixture
async def computer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Harness]:
    """Yield a started Computer Use Extension over a fresh ``FakeTarget``."""
    target = FakeTarget()
    hotkeys: list[FakeHotkey] = []
    sleeps: list[float] = []

    async def no_wait(seconds: float, stop: asyncio.Event) -> bool:
        sleeps.append(seconds)
        await asyncio.sleep(0)
        return stop.is_set()

    def new_hotkey(callback: Callable[[object], None]) -> FakeHotkey:
        hotkeys.append(FakeHotkey(callback))
        return hotkeys[-1]

    monkeypatch.setattr(computer_use, "_new_target", lambda: target)
    monkeypatch.setattr(computer_use, "_new_hotkey", new_hotkey)
    monkeypatch.setattr(computer_use, "_sleep", no_wait)
    declarations = ExtensionDeclarations()
    api = ExtensionAPI(
        "computer_use", declarations, config={}, logger=logging.getLogger("test.computer_use")
    )
    computer_use.register(api)
    registry = ToolRegistry()
    families = {family.id: family.label for family in declarations.tool_families}
    for family_id, label in families.items():
        registry.register_family(family_id, label, extension="computer_use")
    for declaration in declarations.tools:
        registry.register(
            declaration.name,
            declaration.description,
            declaration.parameters,
            declaration.handler,
            requires_opt_in=declaration.requires_opt_in,
            display=declaration.display,
            ready=declaration.ready,
            readiness_hint=declaration.readiness_hint,
            extension="computer_use",
            family=declaration.family,
            result_schema=declaration.result_schema,
            parallel_safe=declaration.parallel_safe,
            open_input_schema=declaration.open_input_schema,
            argument_normalizer=declaration.argument_normalizer,
        )
    api.operations.bind(registry)
    service = registered_service(declarations)
    agent = SimpleNamespace(
        name="Helper",
        tool_access=ToolAccess(granted=TOOLS),
        memory_prompt_mode="off",
        workspace=str(tmp_path),
    )
    published: list[tuple[str, list[str], int]] = []
    host = SimpleNamespace(
        data_dir=tmp_path,
        resolve_tool_agent=lambda context: agent,
        publish_change=lambda resource, ids, revision: published.append(
            (resource, list(ids), revision)
        ),
    )
    context = ToolContext(
        agent_id="agent",
        session_id="session",
        run_id="run",
        tool_call_id="call",
        tool_name="computer",
        tool_call_index=0,
        workspace=tmp_path,
        vbot_root=tmp_path,
        data_root=tmp_path,
    )
    await service.start(cast(ExtensionHost, host))
    yield Harness(service, api, registry, target, hotkeys, agent, context, published, sleeps)
    await service.close()
