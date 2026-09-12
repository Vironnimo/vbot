"""Shared fixtures and fakes for computer use behavior tests."""

from __future__ import annotations

import asyncio
import base64
import io
import logging
from types import SimpleNamespace

import pytest
from PIL import Image

from core.extensions.extensions import ExtensionAPI, ExtensionDeclarations
from core.tools import ToolContext, ToolRegistry
from core.tools.availability import ToolAccess
from resources.extensions.computer_use import extension as computer_use
from resources.extensions.computer_use.driver import ComputerUseError


def png(size=(320, 180)):
    image = Image.new("RGB", size, "#abcdef")
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


class DesktopClient:
    def __init__(self):
        self.calls = []
        self.fail = None
        self.fail_capture_after_input = False
        self.inputs = 0
        self.snapshots = 0
        self.version = "0.23.2"
        self.broken = False
        self.closed = False
        self.size = (320, 180)
        self.connects = 0
        self.hook = None
        self.schemas = {
            name: {"properties": {"session": {}, "button": {}, "count": {}}}
            for name in [
                "start_session",
                "end_session",
                "get_window_state",
                "capture_pixels",
                "resolve_window",
                "list_monitors",
                "move_cursor",
                "get_desktop_state",
                "get_browser_state",
                "list_apps",
                "list_windows",
                "click",
                "double_click",
                "right_click",
                "type_text",
                "set_value",
                "press_key",
                "hotkey",
                "scroll",
                "drag",
                "launch_app",
                "invoke_menu",
                "set_window_frame",
                "verify_state",
                "browser_prepare",
                "browser_click",
                "browser_type",
                "browser_navigate",
                "browser_pointer",
                "browser_dialog",
                "browser_download",
                "browser_set_input_files",
            ]
        }

    def connect(self):
        self.connects += 1

    def call(self, name, arguments):
        self.calls.append((name, arguments))
        if self.hook:
            self.hook(name)
        if name == self.fail:
            raise ComputerUseError("test-owned failure")
        if name == "resolve_window":
            return {key: arguments[key] for key in ("pid", "window_id")}
        if name in {"get_window_state", "get_desktop_state", "capture_pixels"}:
            if self.fail_capture_after_input and self.inputs:
                raise ComputerUseError("test-owned capture failure")
            self.snapshots += 1
            result = {
                "elements": [
                    {
                        "element_index": 1,
                        "element_token": f"s{self.snapshots:08x}:1",
                        "role": "Edit",
                        "label": "Draft",
                    }
                ],
                "tree_markdown": "duplicate tree",
            }
            if arguments.get("include_screenshot", True):
                result["screenshot_png_b64"] = base64.b64encode(png(self.size)).decode()
            if name == "get_browser_state":
                result.update(target_id="b1", tab_id="t1")
            return result
        if name == "list_monitors":
            return {"monitors": [{"id": 1, "x": -1920, "y": 0, "width": 1920, "height": 1080}]}
        if name == "verify_state":
            return {"status": "satisfied", "verified": True}
        if name in {"start_session", "end_session"}:
            return {}
        self.inputs += 1
        return {
            "effect": "unverifiable",
            "delivery": {"mode": "background"},
            "text": "backend-echo",
        }

    def close(self):
        self.closed = True

    def interrupt(self):
        self.broken = True


@pytest.fixture
def computer(tmp_path, monkeypatch):
    # Timing is exercised explicitly below; unrelated integration tests need no real pause.
    monkeypatch.setattr(computer_use, "_POST_INPUT_OBSERVATION_MS", 0)
    monkeypatch.setattr(computer_use.EmergencyHotkey, "start", lambda self: None)
    declarations = ExtensionDeclarations()
    api = ExtensionAPI(
        "computer_use", declarations, config={}, logger=logging.getLogger("test.computer")
    )
    computer_use.register(api)
    declaration = declarations.tools[0]
    service = declaration.handler.__self__
    registry = ToolRegistry()
    registry.register(
        declaration.name,
        declaration.description,
        declaration.parameters,
        declaration.handler,
        requires_opt_in=True,
        open_input_schema=True,
    )
    api.operations.bind(registry)
    agent = SimpleNamespace(
        tool_access=ToolAccess(granted=("computer",)),
        memory_prompt_mode="off",
        workspace=str(tmp_path),
    )
    asyncio.run(
        service.start(SimpleNamespace(data_dir=tmp_path, resolve_agent=lambda project, name: agent))
    )
    client = DesktopClient()
    monkeypatch.setattr(service, "_client", lambda: client)
    context = ToolContext(
        agent_id="a",
        session_id="s",
        run_id="r",
        tool_call_id="t",
        tool_name="computer",
        tool_call_index=0,
        workspace=tmp_path,
        vbot_root=tmp_path,
        data_root=tmp_path,
    )
    yield service, context, client, agent
    service.close()


def capture(computer, **kwargs):
    service, context, _, _ = computer
    args = {"action": "capture", "pid": 1, "window_id": 2, "mode": "som", **kwargs}
    return service.handle(context, args)


def call(computer, action, **kwargs):
    service, context, _, _ = computer
    return service.handle(context, {"action": action, "pid": 1, "window_id": 2, **kwargs})


@pytest.fixture
def lifecycle_connection(computer, monkeypatch):
    """Exercise the real adapter with Cua's named and implicit MCP lifecycles."""
    from contextlib import asynccontextmanager

    from resources.extensions.computer_use import driver

    connections = []
    schemas = {
        "get_config": {},
        "start_session": {"session": {"type": "string"}},
        "end_session": {"session": {"type": "string"}},
        # Cua 0.23.2 Windows discovery schemas do not accept session labels.
        "get_window_state": {"session": {}, "pid": {}, "window_id": {}, "include_screenshot": {}},
        "click": {"session": {}, "target": {}, "element_token": {}, "delivery_mode": {}},
        "list_apps": {},
        "list_windows": {"pid": {"type": "integer"}, "on_screen_only": {"type": "boolean"}},
    }

    @asynccontextmanager
    async def stdio(self, environment):
        yield None, None

    class Session:
        def __init__(self, *args, **kwargs):
            self.calls = []
            self.ended = set()
            self.closed = False
            self.fail_start = False
            self.fail_transport_on = None
            connections.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            self.closed = True

        async def initialize(self):
            return SimpleNamespace(server_info=SimpleNamespace(version="0.23.2"))

        async def list_tools(self):
            return SimpleNamespace(
                tools=[
                    SimpleNamespace(
                        name=name,
                        input_schema={
                            "type": "object",
                            "properties": properties,
                            "additionalProperties": False,
                        },
                    )
                    for name, properties in schemas.items()
                ]
            )

        async def call_tool(self, name, arguments):
            assert not self.closed
            assert arguments.keys() <= schemas[name].keys()
            self.calls.append((name, arguments))
            if name == self.fail_transport_on:
                raise ConnectionError("test-owned transport loss")
            session = arguments.get("session", "implicit")
            failed = False
            revived = session in self.ended
            if name == "start_session":
                failed = self.fail_start
                if not failed:
                    self.ended.discard(session)
            elif name == "end_session":
                self.ended.add(session)
            else:
                failed = session in self.ended
            payload = (
                {
                    "isError": True,
                    "structuredContent": {"code": "session_ended"},
                    "content": [{"type": "text", "text": "test-owned ended session"}],
                }
                if failed
                else {
                    "structuredContent": {
                        "max_image_dimension": 0,
                        "apps": [],
                        "windows": [],
                        "revived": revived,
                    }
                }
            )
            return SimpleNamespace(model_dump=lambda **kwargs: payload)

    def client(executable):
        connection = driver.CuaDriver(executable)
        connection.desktop = None
        return connection

    service = computer[0]
    service.executable = "test-owned-driver"
    monkeypatch.setattr(driver.CuaDriver, "_stdio", stdio)
    monkeypatch.setattr(driver, "ClientSession", Session)
    monkeypatch.setattr(computer_use, "CuaDriver", client)
    monkeypatch.setattr(
        service, "_client", computer_use.ComputerUseService._client.__get__(service)
    )
    return connections
