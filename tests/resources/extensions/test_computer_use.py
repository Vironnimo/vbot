"""Computer Use integration regressions with real PNGs and controlled driver effects."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from core.extensions.extensions import ExtensionAPI, ExtensionDeclarations
from core.tools import ToolContext, ToolRegistry
from core.tools.availability import ToolAccess
from resources.extensions.computer_use import extension as computer_use
from resources.extensions.computer_use import observations
from resources.extensions.computer_use.driver import ComputerUseError, unpack


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


def test_input_returns_fresh_capture_and_does_not_echo_text(computer):
    assert (
        call(computer, "type", text="private draft", apply=True)["error"]["code"]
        == "capture_required"
    )
    assert capture(computer)["ok"]
    result = call(computer, "type", text="private draft", element="1", apply=True)
    assert result["ok"] and result["data"]["applied"]
    assert result["data"]["observation"]["view_id"]
    assert result["data"]["effect"] == "unverifiable"
    assert "backend-echo" not in str(result) and "private draft" not in str(result)
    assert call(computer, "key", shortcut="enter", apply=True)["ok"]
    assert len(computer[1].result_media) == 3


def test_preview_sends_no_input_and_does_not_consume_capture(computer):
    assert capture(computer)["ok"]
    before = len(computer[2].calls)
    assert call(computer, "type", text="private draft", apply=False)["data"]["preview"]
    assert len(computer[2].calls) == before
    assert call(computer, "type", text="private draft", apply=True)["ok"]


def test_ax_is_driver_tree_only_and_som_removes_duplicate_tree(computer):
    result = capture(computer, mode="ax", query="Draft", limit=25)
    assert result["ok"]
    assert not computer[1].result_media
    assert "tree_markdown" not in result["data"]
    name, args = computer[2].calls[-1]
    assert name == "get_window_state"
    assert (
        args["include_screenshot"] is False
        and args["query"] == "Draft"
        and args["max_elements"] == 25
    )


def test_scaled_image_coordinates_and_native_crop_round_trip(computer):
    computer[2].size = (3840, 2160)
    result = capture(computer)["data"]
    assert (result["image_width"], result["image_height"]) == (1600, 900)
    assert Image.open(computer[1].presentation_images[-1]["path"]).size == (3840, 2160)
    zoom = call(
        computer, "zoom", view_id=result["view_id"], coordinate=[100, 100], to_coordinate=[200, 200]
    )["data"]
    assert (zoom["image_width"], zoom["image_height"]) == (240, 240)
    clicked = call(computer, "click", view_id=zoom["view_id"], coordinate=[50, 60], apply=True)
    assert clicked["ok"]
    _, args = next(item for item in computer[2].calls if item[0] == "click")
    assert (args["x"], args["y"]) == (290, 300)
    assert (
        call(computer, "click", view_id=result["view_id"], coordinate=[1, 1], apply=True)["error"][
            "code"
        ]
        == "stale_view"
    )


def test_original_resolution_and_image_edges(computer):
    computer[2].size = (1800, 1000)
    data = capture(computer, resolution="original")["data"]
    assert data["image_width"] == 1800
    result = call(computer, "click", view_id=data["view_id"], coordinate=[1800, 2], apply=True)
    assert result["error"]["code"] == "invalid_coordinates"
    assert not any(name == "click" for name, _ in computer[2].calls)


@pytest.mark.parametrize(
    "change",
    [{"session_id": "other"}, {"agent_id": "other"}, {"project_id": "other"}, {"run_id": "other"}],
)
def test_capture_authority_never_crosses_context(computer, change):
    service, context, client, _ = computer
    data = capture(computer)["data"]
    result = service.handle(
        replace(context, **change),
        {
            "action": "click",
            "pid": 1,
            "window_id": 2,
            "view_id": data["view_id"],
            "coordinate": [1, 1],
            "apply": True,
        },
    )
    assert result["error"]["code"] == "stale_view"
    assert not any(name == "click" for name, _ in client.calls)


def test_other_run_capture_invalidates_old_view(computer):
    service, context, _, _ = computer
    capture(computer)
    assert service.handle(
        replace(context, run_id="r2"), {"action": "capture", "pid": 1, "window_id": 2}
    )["ok"]
    assert call(computer, "type", text="draft", apply=True)["error"]["code"] == "capture_required"


def test_window_target_mismatch_rejects_tokens(computer):
    capture(computer)
    assert (
        call(computer, "click", window_id=3, element="s00000001:1", apply=True)["error"]["code"]
        == "capture_required"
    )


def test_failure_invalidates_capture_and_never_retries_input(computer):
    capture(computer)
    computer[2].fail = "type_text"
    assert not call(computer, "type", text="draft", apply=True)["ok"]
    assert call(computer, "type", text="draft", apply=True)["error"]["code"] == "capture_required"
    assert sum(name == "type_text" for name, _ in computer[2].calls) == 1


def test_post_action_capture_failure_preserves_applied_outcome(computer):
    capture(computer)
    computer[2].fail_capture_after_input = True
    result = call(computer, "type", text="draft", apply=True)
    assert result["ok"] and result["data"]["applied"]
    assert result["data"]["observation_error"]
    assert call(computer, "type", text="draft", apply=True)["error"]["code"] == "capture_required"


def test_sequence_saves_captures_and_stops_on_failure(computer):
    capture(computer)
    result = call(
        computer,
        "sequence",
        apply=True,
        steps=[
            {"action": "click", "element": "1"},
            {"action": "type", "text": "draft"},
            {"action": "key", "shortcut": "enter"},
        ],
    )
    assert result["ok"] and result["data"]["completed_steps"] == 3
    assert computer[2].snapshots == 2
    computer[2].fail = "type_text"
    result = call(
        computer,
        "sequence",
        apply=True,
        steps=[
            {"action": "key", "shortcut": "ctrl+a"},
            {"action": "type", "text": "draft"},
            {"action": "key", "shortcut": "enter"},
        ],
    )
    assert result["data"]["partial"] and result["data"]["completed_steps"] == 1
    assert sum(name == "press_key" for name, _ in computer[2].calls) == 1


def test_later_sequence_coordinates_validate_before_first_input(computer):
    data = capture(computer)["data"]
    result = call(
        computer,
        "sequence",
        apply=True,
        steps=[
            {"action": "type", "text": "must not be sent"},
            {"action": "click", "view_id": data["view_id"], "coordinate": [9000, 10]},
        ],
    )
    assert result["error"]["code"] == "invalid_coordinates"
    assert computer[2].inputs == 0


def test_desktop_defaults_to_fast_pixels_and_native_sequence(computer):
    service, context, client, _ = computer
    data = service.handle(context, {"action": "capture"})["data"]
    assert client.calls[-1][0] == "get_desktop_state"
    result = service.handle(
        context,
        {
            "action": "sequence",
            "apply": True,
            "steps": [
                {"action": "move", "view_id": data["view_id"], "coordinate": [10, 10]},
                {"action": "click", "view_id": data["view_id"], "coordinate": [20, 20]},
                {"action": "key", "shortcut": "alt+f4"},
            ],
        },
    )
    assert result["data"]["completed_steps"] == 3
    assert client.snapshots == 2
    assert all(
        args.get("delivery_mode") == "foreground"
        for name, args in client.calls
        if name in {"move_cursor", "click", "hotkey"}
    )


def test_windows_vision_does_not_request_element_tree(computer):
    result = capture(computer, mode="vision")
    assert result["ok"]
    assert computer[2].calls[-1][0] == "capture_pixels"


def test_sequence_rechecks_cancellation_between_steps(computer):
    service, context, client, _ = computer
    capture(computer)
    cancelled = False

    def hook(name):
        nonlocal cancelled
        if name == "hotkey":
            cancelled = True

    client.hook = hook
    context = replace(context, cancellation_hook=lambda: cancelled)
    result = service.handle(
        context,
        {
            "action": "sequence",
            "pid": 1,
            "window_id": 2,
            "apply": True,
            "steps": [{"action": "key", "shortcut": "ctrl+a"}, {"action": "type", "text": "draft"}],
        },
    )
    assert result["data"]["completed_steps"] == 1
    assert not any(name == "type_text" for name, _ in client.calls)


def test_revocation_rechecked_after_waiting_for_lock(computer):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    service, context, client, agent = computer
    entered = Event()

    def waiting():
        entered.set()
        return service.handle(context, {"action": "windows"})

    with ThreadPoolExecutor(max_workers=1) as pool:
        with service._lock:
            pending = pool.submit(waiting)
            assert entered.wait(5)
            agent.tool_access = ToolAccess()
        assert not pending.result(timeout=5)["ok"]
    assert not client.calls


def test_cancel_during_start_prevents_capture(computer):
    service, context, client, _ = computer
    cancelled = False

    def hook(name):
        nonlocal cancelled
        if name == "start_session":
            cancelled = True

    client.hook = hook
    result = service.handle(
        replace(context, cancellation_hook=lambda: cancelled),
        {"action": "capture", "pid": 1, "window_id": 2},
    )
    assert not result["ok"]
    assert [name for name, _ in client.calls] == ["start_session"]


@pytest.mark.parametrize(
    "args",
    [
        {"action": "capture", "pid": 1},
        {"action": "windows", "session": "foreign"},
        {"action": "capture", "pid": True, "window_id": 2},
        {"action": "capture", "pid": 1.0, "window_id": 2},
        {"action": "capture", "pid": 1, "window_id": 2, "apply": True},
        {"action": "click", "pid": 1, "window_id": 2, "coordinate": [1, 1]},
        {"action": "key", "pid": 1, "window_id": 2, "shortcut": "+"},
        {"action": "scroll", "pid": 1, "window_id": 2, "direction": "down", "amount": 101},
        {"action": "capture", "scope": "desktop", "pid": 1, "window_id": 2},
        {"action": "capture", "mode": "ax"},
        {
            "action": "sequence",
            "pid": 1,
            "window_id": 2,
            "steps": [{"action": "key", "shortcut": "enter"}, {"action": "click", "element": "1"}],
        },
        {
            "action": "sequence",
            "pid": 1,
            "window_id": 2,
            "steps": [{"action": "type", "text": "x", "session": "foreign"}],
        },
        {
            "action": "verify",
            "pid": 1,
            "window_id": 2,
            "expect": [{"element": {"selector": {}, "exists": True}}],
        },
        {
            "action": "verify",
            "pid": 1,
            "window_id": 2,
            "expect": [{"window": {"exists": True, "typo": 1}}],
        },
        {"action": "browser_prepare", "profile": "isolated", "pid": 1},
        {"action": "browser_capture", "target_id": "b"},
        {
            "action": "browser_navigate",
            "target_id": "b",
            "tab_id": "t",
            "url": "javascript:alert(1)",
        },
        {"action": "browser_dialog", "target_id": "b", "tab_id": "t", "dialog_action": "accept"},
    ],
)
def test_invalid_arguments_fail_before_any_driver_work(computer, args):
    service, context, client, _ = computer
    assert service.handle(context, args)["error"]["code"] == "invalid_arguments"
    assert not client.calls and client.connects == 0


@pytest.mark.parametrize(
    "action,fields,tool",
    [
        ("set_value", {"element": "1", "text": "new"}, "set_value"),
        ("menu", {"menu_path": ["File", "Save"]}, "invoke_menu"),
        ("resize", {"coordinate": [-100, 0], "size": [900, 700]}, "set_window_frame"),
        ("drag", {"coordinate": [1, 2], "to_coordinate": [20, 30]}, "drag"),
    ],
)
def test_desktop_operations_return_observations(computer, action, fields, tool):
    data = capture(computer)["data"]
    if action == "drag":
        fields = {**fields, "view_id": data["view_id"]}
    result = call(computer, action, apply=True, **fields)
    assert result["ok"] and result["data"]["observation"]
    assert any(name == tool for name, _ in computer[2].calls)


def test_desktop_scope_uses_own_coordinate_space(computer):
    service, context, client, _ = computer
    data = service.handle(context, {"action": "capture"})["data"]
    result = service.handle(
        context,
        {
            "action": "click",
            "view_id": data["view_id"],
            "coordinate": [10, 10],
            "apply": True,
        },
    )
    assert result["ok"]
    _, payload = next(item for item in client.calls if item[0] == "click")
    assert "pid" not in payload


def test_verify_reports_structured_result_and_fresh_observation(computer):
    result = call(computer, "verify", expect=[{"window": {"exists": True}}], timeout_ms=0)
    assert result["ok"] and result["data"]["verification"]["verified"]
    assert result["data"]["observation"]


@pytest.mark.parametrize("render_after", [0.4, 2.0])
def test_post_input_capture_delay_is_bounded_without_screen_polling(
    computer, monkeypatch, render_after
):
    service, _, client, _ = computer
    capture(computer)
    clock = [0.0]
    monkeypatch.setattr(computer_use, "_POST_INPUT_OBSERVATION_MS", 1000)
    monkeypatch.setattr(computer_use.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        service._wake, "wait", lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    )
    original_call = client.call
    observed_states = []

    def response(name, args):
        result = original_call(name, args)
        if name == "capture_pixels":
            observed_states.append(clock[0] >= render_after)
        return result

    client.call = response
    result = call(computer, "type", text="draft", apply=True)
    assert result["ok"]
    assert result["data"]["observation_delay_ms"] == 1000
    assert observed_states == [render_after <= 1.0]
    assert "verification" not in result["data"]
    assert clock[0] == pytest.approx(1.0)
    assert client.inputs == 1 and client.snapshots == 2


def test_stop_during_post_input_delay_preserves_dispatch_without_replay(computer, monkeypatch):
    service, _, client, _ = computer
    capture(computer)
    monkeypatch.setattr(computer_use, "_POST_INPUT_OBSERVATION_MS", 1000)
    waiting = threading.Event()
    original_wait = service._wake.wait

    def wait(seconds):
        waiting.set()
        return original_wait(seconds)

    monkeypatch.setattr(service._wake, "wait", wait)
    with ThreadPoolExecutor() as executor:
        future = executor.submit(call, computer, "type", text="draft", apply=True)
        assert waiting.wait(1)
        service.stop()
        result = future.result(timeout=0.5)
    assert result["data"]["applied"]
    assert result["data"]["observation_error"]["code"] == "computer_use_interrupted"
    assert client.inputs == 1 and client.snapshots == 1


def test_verify_waits_for_exact_postcondition_before_capturing(computer, monkeypatch):
    service, _, client, _ = computer
    clock = [0.0]
    monkeypatch.setattr(computer_use.time, "monotonic", lambda: clock[0])
    original_call = client.call
    expectation = [{"element": {"selector": {"role": "Edit"}, "value_equals": "done"}}]
    statuses = iter(["unknown", "unsatisfied", "satisfied"])

    def response(name, args):
        result = original_call(name, args)
        if name == "verify_state":
            assert args["expect"] == expectation
            assert args["timeout_ms"] <= 250
            clock[0] += 0.25
            return {"status": next(statuses)}
        if name == "get_window_state":
            assert clock[0] == 0.75
        return result

    client.call = response
    result = call(computer, "verify", expect=expectation, timeout_ms=1000)
    assert result["data"]["verification"]["status"] == "satisfied"
    assert client.inputs == 0 and client.snapshots == 1


def test_owned_session_cleanup_and_retirement(computer):
    service, context, client, _ = computer
    capture(computer)
    service.handle(replace(context, run_id="r2"), {"action": "apps"})
    owned = {session.name for session in service._sessions.values()}
    service.run_end(SimpleNamespace(run_id="r"))
    assert len(service._sessions) == 1
    service.close()
    assert not service._sessions
    assert {args["session"] for name, args in client.calls if name == "end_session"} == owned
    count = len(client.calls)
    assert not service.handle(context, {"action": "windows"})["ok"]
    assert len(client.calls) == count


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
                else {"structuredContent": {"max_image_dimension": 0, "apps": [], "windows": []}}
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


@pytest.mark.parametrize("finish", ["run_end", "cancel_between_tools", "close"])
def test_next_run_gets_fresh_connection_after_last_session_ends(
    computer, lifecycle_connection, finish
):
    service, context, _, _ = computer
    callbacks = []
    context = replace(context, cancel_registration_hook=callbacks.append)
    assert service.handle(context, {"action": "apps"})["ok"]
    previous = lifecycle_connection[0]
    # Simulate Cua's five-minute idle expiry without a wait or desktop access.
    previous.ended.add("implicit")
    if finish == "close":
        assert service.handle(context, {"action": "close"})["ok"]
    else:
        if finish == "cancel_between_tools":
            callbacks[0]()
        service.run_end(context)
    assert previous.closed and service._driver is None and not service._sessions

    following = replace(context, agent_id="b", session_id="s2", run_id="r2")
    assert service.handle(following, {"action": "apps"})["ok"]
    assert service.handle(following, {"action": "windows"})["ok"]
    assert len(lifecycle_connection) == 2


def test_implicit_session_expiry_and_run_cleanup_preserve_other_run(computer, lifecycle_connection):
    service, context, _, _ = computer
    assert service.handle(context, {"action": "apps"})["ok"]
    connection = lifecycle_connection[0]
    other = replace(context, agent_id="b", session_id="s2", run_id="r2")
    assert service.handle(other, {"action": "apps"})["ok"]
    service.run_end(context)
    assert not connection.closed and len(service._sessions) == 1

    connection.ended.add("implicit")
    before = len(connection.calls)
    assert service.handle(other, {"action": "windows"})["ok"]
    assert connection.calls[before:] == [("start_session", {}), ("list_windows", {})]
    service.run_end(context)  # A late completion must not close the surviving Run.
    assert not connection.closed and len(lifecycle_connection) == 1
    service.run_end(other)
    assert connection.closed and service._driver is None


def test_failed_implicit_start_prevents_dispatch(computer, lifecycle_connection):
    service, context, _, _ = computer
    assert service.handle(context, {"action": "apps"})["ok"]
    connection = lifecycle_connection[0]
    connection.fail_start = True
    before = len(connection.calls)
    assert not service.handle(context, {"action": "windows"})["ok"]
    assert connection.calls[before:] == [("start_session", {})]
    connection.fail_start = False
    assert service.handle(context, {"action": "windows"})["ok"]
    assert len(lifecycle_connection) == 1


def test_failed_session_start_is_not_cached(computer):
    service, context, client, _ = computer
    client.fail = "start_session"
    assert not service.handle(context, {"action": "apps"})["ok"]
    assert not service._sessions
    client.fail = None
    assert service.handle(context, {"action": "apps"})["ok"]
    assert [name for name, _ in client.calls].count("start_session") == 2


def test_broken_worker_replaces_cached_session_before_dispatch(computer, lifecycle_connection):
    service, context, _, _ = computer
    assert service.handle(context, {"action": "apps"})["ok"]
    previous = next(iter(service._sessions.values())).name
    service._driver.broken = True
    assert service.handle(context, {"action": "windows"})["ok"]
    current = next(iter(service._sessions.values())).name
    assert current != previous
    assert lifecycle_connection[0].closed and len(lifecycle_connection) == 2
    assert ("start_session", {"session": current}) in lifecycle_connection[1].calls


def test_cleanup_transport_loss_does_not_connect_to_close_remaining_sessions(
    computer, lifecycle_connection
):
    service, context, _, _ = computer
    assert service.handle(context, {"action": "apps"})["ok"]
    other = replace(context, session_id="s2", run_id="r2")
    assert service.handle(other, {"action": "apps"})["ok"]
    connection = lifecycle_connection[0]
    connection.fail_transport_on = "end_session"
    service._close_sessions()
    assert connection.closed and service._driver is None and not service._sessions
    assert len(lifecycle_connection) == 1


def test_failed_capture_retires_old_view(computer):
    capture(computer)
    computer[2].fail = "get_window_state"
    assert not capture(computer)["ok"]
    assert call(computer, "type", text="draft", apply=True)["error"]["code"] == "capture_required"


@pytest.mark.parametrize(
    "payload",
    [
        {"isError": True, "content": [{"type": "text", "text": "test refusal"}]},
        {"structuredContent": {"status": "refused", "refusal": {"code": "stale"}}},
        {"content": [{"type": "text", "text": 'error with example {"ok": true}'}]},
    ],
)
def test_protocol_errors_never_become_success(payload):
    with pytest.raises(ComputerUseError):
        unpack(payload)


def test_protocol_preserves_structured_state_and_png():
    assert unpack(
        {
            "structuredContent": {"elements": []},
            "content": [{"type": "image", "mimeType": "image/png", "data": "abc"}],
        }
    ) == {"elements": [], "screenshot_png_b64": "abc"}


def test_invalid_pixels_and_untrusted_file_paths_never_authorize_capture(computer, tmp_path):
    context = computer[1]
    private = tmp_path / "private.png"
    private.write_bytes(png())
    with pytest.raises(ComputerUseError):
        observations.capture(context, ("window", 1, 2), {"screenshot_file_path": str(private)})
    with pytest.raises(ComputerUseError):
        observations.capture(
            context,
            ("window", 1, 2),
            {"screenshot_png_b64": base64.b64encode(b"not an image").decode()},
        )
    assert not context.result_media


def test_missing_driver_is_not_ready(monkeypatch):
    monkeypatch.setattr(computer_use.shutil, "which", lambda _: None)
    service = computer_use.ComputerUseService(SimpleNamespace())
    assert not service.ready()
    with pytest.raises(ComputerUseError):
        service._client()


def test_desktop_mutation_invalidates_other_window_observations(computer):
    service, context, _, _ = computer
    capture(computer)
    data = service.handle(context, {"action": "capture"})["data"]
    assert service.handle(
        context,
        {
            "action": "click",
            "view_id": data["view_id"],
            "coordinate": [10, 20],
            "apply": True,
        },
    )["ok"]
    assert (
        call(computer, "key", shortcut="enter", apply=True)["error"]["code"] == "capture_required"
    )


def test_empty_set_value_clears_field(computer):
    capture(computer)
    assert call(computer, "set_value", element="1", text="", apply=True)["ok"]
    assert next(args for name, args in computer[2].calls if name == "set_value")["value"] == ""


def test_structured_effect_refusal_is_failure():
    with pytest.raises(ComputerUseError, match="denied"):
        unpack({"structuredContent": {"effect": "refused", "message": "denied"}})


@pytest.mark.parametrize(
    "version,dimension,expected",
    [
        ("0.23.2", 0, None),
        ("0.20.0", 0, "Update cua-driver"),
        ("0.24.0-beta", 0, "Update cua-driver"),
        ("0.23.2", 1568, "max_image_dimension"),
    ],
)
def test_persistent_mcp_handshake_version_config_and_cleanup(
    monkeypatch, version, dimension, expected
):
    from contextlib import asynccontextmanager

    from resources.extensions.computer_use import driver

    events = []

    @asynccontextmanager
    async def stdio(self, environment):
        assert environment["CUA_DRIVER_RS_TELEMETRY_ENABLED"] == "0"
        events.append("open")
        try:
            yield None, None
        finally:
            events.append("close")

    class Session:
        def __init__(self, *args, **kwargs):
            assert kwargs["read_timeout_seconds"] == 45

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def initialize(self):
            return SimpleNamespace(server_info=SimpleNamespace(version=version))

        async def list_tools(self):
            return SimpleNamespace(
                tools=[
                    SimpleNamespace(name="get_config", input_schema={}),
                    SimpleNamespace(name="click", input_schema={"properties": {"target": {}}}),
                ]
            )

        async def call_tool(self, name, arguments):
            events.append(name)
            return SimpleNamespace(
                model_dump=lambda **kwargs: {
                    "structuredContent": {"max_image_dimension": dimension}
                }
            )

    monkeypatch.setattr(driver.CuaDriver, "_stdio", stdio)
    monkeypatch.setattr(driver, "ClientSession", Session)
    client = driver.CuaDriver("test-owned-driver")
    client.desktop = None
    try:
        if expected:
            with pytest.raises(ComputerUseError, match=expected):
                client.connect()
        else:
            client.call("click", {"session": "test-owned"})
            client.call("click", {"session": "test-owned"})
            assert events == ["open", "get_config", "click", "click"]
    finally:
        client.close()
    assert events.count("open") == events.count("close") == 1
    assert "set_config" not in events


def test_transport_timeout_never_replays_uncertain_input():
    from resources.extensions.computer_use.driver import CuaDriver

    client = CuaDriver("test-owned-driver")
    client.desktop = None
    calls = []

    class Portal:
        def call(self, method, name, arguments):
            calls.append(name)
            raise TimeoutError()

    client._portal = Portal()
    client._session = SimpleNamespace(call_tool=None)
    client.schemas = {"click": {"properties": {"target": {}}}}
    with pytest.raises(ComputerUseError):
        client.call("click", {"session": "test-owned"})
    assert calls == ["click"] and client.broken and client._session is None


@pytest.mark.parametrize("source", ["tool_cancel", "control", "double_escape"])
def test_interrupt_only_affects_current_call_and_next_agents_can_use_tool(
    computer, monkeypatch, source
):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    service, context, client, _ = computer
    service.executable = "test-owned-driver"
    service._driver = client
    monkeypatch.setattr(
        service, "_client", computer_use.ComputerUseService._client.__get__(service)
    )
    replacements = []

    def new_client(executable):
        replacement = DesktopClient()
        replacements.append(replacement)
        return replacement

    monkeypatch.setattr(computer_use, "CuaDriver", new_client)
    callbacks = []
    context = replace(context, cancel_registration_hook=callbacks.append)
    entered = threading.Event()
    stopped = threading.Event()
    original = client.interrupt

    def interrupt():
        original()
        stopped.set()

    client.interrupt = interrupt

    def block(name):
        if name == "list_apps":
            entered.set()
            assert stopped.wait(3)
            raise ComputerUseError("test-owned interrupted input")

    client.hook = block
    with ThreadPoolExecutor() as pool:
        future = pool.submit(service.handle, context, {"action": "apps"})
        assert entered.wait(3)
        if source == "tool_cancel":
            callbacks[0]()
        elif source == "control":
            status = asyncio.run(service.control({}))
            asyncio.run(service.control({"action": "stop", "call_id": status["call_id"]}))
        else:
            service._hotkey._key_event(0x1B, True, 0)
            service._hotkey._key_event(0x1B, False, 0)
            service._hotkey._key_event(0x1B, True, 0)
            owner = service._hotkey.pending_owner
            assert owner is service._active
            service._hotkey.callback(owner)
        result = future.result(timeout=2)
        assert result["error"]["code"] == "computer_use_interrupted"
    assert client.broken and client.closed and service._driver is None
    assert not (context.data_root / "computer-use-stopped").exists()
    assert not asyncio.run(service.control({}))["active"]
    assert service.handle(context, {"action": "apps"})["ok"]
    other = replace(context, agent_id="other", session_id="other-session", run_id="other-run")
    assert service.handle(other, {"action": "apps"})["ok"]
    assert len(replacements) == 1
    assert not replacements[0].broken


def test_late_cancel_does_not_stop_next_call(computer):
    service, context, client, _ = computer
    callbacks = []
    context = replace(context, cancel_registration_hook=callbacks.append)
    assert service.handle(context, {"action": "apps"})["ok"]
    client.hook = lambda name: callbacks[0]()
    assert service.handle(context, {"action": "apps"})["ok"]
    assert not asyncio.run(service.control({}))["stopping"]
    assert not client.broken


def test_old_stop_file_has_no_runtime_effect_after_reload(computer, monkeypatch):
    service, context, _, _ = computer
    marker = context.data_root / "computer-use-stopped"
    marker.touch()
    replacement = computer_use.ComputerUseService(service.api)
    replacement.executable = "test-owned-driver"
    monkeypatch.setattr(computer_use, "CuaDriver", lambda executable: DesktopClient())
    asyncio.run(replacement.start(service.host))
    try:
        assert replacement.handle(context, {"action": "apps"})["ok"]
        assert set(asyncio.run(replacement.control({}))) == {
            "available",
            "active",
            "stopping",
            "hotkey_available",
        }
    finally:
        replacement.close()


def test_control_rejects_release_and_untargeted_stop(computer):
    service, context, client, _ = computer
    for arguments in ({"action": "resume"}, {"action": "stop"}):
        with pytest.raises(ValueError):
            asyncio.run(service.api.operations.invoke("control", arguments))
    # A stop after the targeted call completed is a no-op, not an idle lock.
    asyncio.run(service.control({"action": "stop", "call_id": "test-owned-expired-call"}))
    assert not client.broken
    assert service.handle(context, {"action": "apps"})["ok"]


def test_stale_control_request_does_not_cancel_next_call(computer):
    service, context, client, _ = computer
    status = []
    client.hook = lambda name: status.append(asyncio.run(service.control({})))
    assert service.handle(context, {"action": "apps"})["ok"]
    old = status[-1]
    client.hook = lambda name: asyncio.run(
        service.control({"action": "stop", "call_id": old["call_id"]})
    )
    # Provider call ids can be reused; the control reference still changes.
    assert service.handle(context, {"action": "apps"})["ok"]
    assert not client.broken


def test_os_interrupt_failure_stops_remaining_work_without_latching(computer):
    service, context, client, _ = computer

    def denied():
        raise PermissionError("test-owned termination failure")

    client.interrupt = denied
    service._driver = client
    client.hook = lambda name: service.stop()
    assert (
        service.handle(context, {"action": "apps"})["error"]["code"] == "computer_use_interrupted"
    )
    assert client.broken and client.closed
    client.hook = None
    assert service.handle(context, {"action": "apps"})["ok"]
    assert not (context.data_root / "computer-use-stopped").exists()


def test_stop_during_connection_admission_cannot_mark_driver_healthy(monkeypatch):
    from contextlib import asynccontextmanager

    from resources.extensions.computer_use.driver import CuaDriver

    closed = []

    @asynccontextmanager
    async def connection(self):
        self.interrupt()
        try:
            yield SimpleNamespace()
        finally:
            closed.append(True)

    monkeypatch.setattr(CuaDriver, "_connection", connection)
    client = CuaDriver("test-owned-driver")
    client.desktop = None
    with pytest.raises(ComputerUseError) as failure:
        client.connect()
    assert failure.value.code == "computer_use_interrupted"
    assert client.broken and client._session is None and closed == [True]


def test_real_owned_process_is_killed_without_waiting_for_rpc(tmp_path, monkeypatch):
    import sys
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    from resources.extensions.computer_use import driver

    entered = threading.Event()
    real_open = driver.anyio.open_process
    processes = []
    script = """
import json, sys, time
for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    method = request["method"]
    if method == "initialize":
        result = {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}},
                  "serverInfo": {"name": "test-owned", "version": "0.23.2"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "click", "inputSchema": {
            "type": "object", "properties": {"target": {}}}}]}
    elif request["params"]["name"] == "get_config":
        result = {"content": [], "structuredContent": {"max_image_dimension": 0}}
    else:
        time.sleep(120)
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
"""

    async def launch(command, **kwargs):
        assert command == ["test-owned-driver", "mcp", "--direct"]
        process = await real_open([sys.executable, "-u", "-c", script], **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(driver.anyio, "open_process", launch)
    client = driver.CuaDriver("test-owned-driver")
    client.desktop = None
    client.connect()

    def call():
        entered.set()
        with pytest.raises(ComputerUseError):
            client.call("click", {})

    try:
        with ThreadPoolExecutor() as pool:
            result = pool.submit(call)
            assert entered.wait(3)
            start = time.monotonic()
            client.interrupt()
            result.result(timeout=3)
            assert time.monotonic() - start < 3
        assert processes[0].returncode is not None
        assert len(processes) == 1
        with pytest.raises(ComputerUseError):
            client.connect()
    finally:
        client.interrupt()
        client.close()


def test_complete_provider_matrix_runs_through_real_handler(computer):
    from scripts.probe_provider_tool_call import COMPUTER_CASE_ARGUMENTS

    service, context, client, _ = computer
    for case, arguments in COMPUTER_CASE_ARGUMENTS.items():
        args = dict(arguments)
        if case.startswith("invalid_"):
            before = len(client.calls)
            result = service.handle(context, args)
            assert result["error"]["code"] == "invalid_arguments", case
            assert len(client.calls) == before, case
            continue
        target = {key: args[key] for key in ("pid", "window_id", "monitor") if key in args}
        observed = service.handle(
            context,
            {
                "action": "capture",
                **target,
                "mode": "som" if "pid" in target else "vision",
                "foreground": args.get("foreground", "pid" not in target),
            },
        )
        assert observed["ok"], case
        if "view_id" in args:
            args["view_id"] = observed["data"]["view_id"]
        if ":" in args.get("element", ""):
            args["element"] = observed["data"]["elements"][0]["element"]
        result = service.handle(context, args)
        assert result["ok"], (case, result)


def test_skip_capture_preserves_outcome_and_retires_view(computer, monkeypatch):
    capture(computer)
    with monkeypatch.context() as scoped:

        def unexpected_wait(*args):
            pytest.fail("Skipping capture must also skip the observation delay")

        scoped.setattr(computer[0], "_wait", unexpected_wait)
        result = call(computer, "type", text="draft", apply=True, capture_after=False)
    assert result["ok"] and result["data"]["applied"]
    assert "observation" not in result["data"]
    assert "Capture" in result["data"]["next_action"]
    assert computer[2].snapshots == 1
    assert (
        call(computer, "key", shortcut="enter", apply=True)["error"]["code"] == "capture_required"
    )
    capture(computer)
    assert call(computer, "key", shortcut="enter", apply=True)["ok"]


def test_sequence_without_capture_and_wait_only_captures_at_end(computer):
    capture(computer)
    result = call(
        computer,
        "sequence",
        apply=True,
        capture_after=False,
        steps=[
            {"action": "type", "text": "draft"},
            {"action": "wait", "duration_ms": 0},
            {"action": "key", "shortcut": "enter"},
        ],
    )
    assert result["ok"] and result["data"]["completed_steps"] == 3
    assert computer[2].snapshots == 1
    assert call(computer, "wait", duration_ms=0)["ok"]
    assert computer[2].snapshots == 2


def test_failed_sequence_captures_even_if_capture_after_false(computer):
    capture(computer)
    computer[2].fail = "press_key"
    result = call(
        computer,
        "sequence",
        apply=True,
        capture_after=False,
        steps=[
            {"action": "type", "text": "draft"},
            {"action": "key", "shortcut": "enter"},
        ],
    )
    assert result["data"]["partial"] and result["data"]["completed_steps"] == 1
    assert "observation" in result["data"]


def test_wait_interrupts_immediately_without_recapture(computer, monkeypatch):
    service, _, client, _ = computer
    waiting = threading.Event()
    original = service._wake.wait

    def wait(seconds):
        waiting.set()
        return original(seconds)

    monkeypatch.setattr(service._wake, "wait", wait)
    with ThreadPoolExecutor() as executor:
        future = executor.submit(call, computer, "wait", duration_ms=10_000)
        assert waiting.wait(1)
        service.stop()
        assert future.result(timeout=0.5)["error"]["code"] == "computer_use_interrupted"
    assert client.snapshots == 0


@pytest.mark.parametrize(
    "arguments",
    [
        {"action": "move", "view_id": "v", "coordinate": [1]},
        {"action": "move", "view_id": "v", "coordinate": [1, 2, 3]},
        {"action": "move", "view_id": "v", "coordinate": [1.0, 2]},
        {"action": "move", "view_id": "v", "coordinate": [True, 2]},
        {"action": "move", "view_id": "v", "coordinate": [-1, 2]},
        {"action": "click", "view_id": "v", "coordinate": [1, 2], "modifiers": ["ctrl", "ctrl"]},
        {
            "action": "click",
            "view_id": "v",
            "coordinate": [1, 2],
            "modifiers": ["ctrl"],
            "foreground": False,
        },
        {"action": "click", "pid": 1, "window_id": 2, "element": "1", "modifiers": ["ctrl"]},
        {"action": "resize", "pid": 1, "window_id": 2, "coordinate": [0, 0], "size": [0, 5]},
        {"action": "wait", "duration_ms": 10_001},
        {"action": "capture", "capture_after": False},
    ],
)
def test_compact_contract_rejects_bad_values_before_connect(computer, arguments):
    service, context, client, _ = computer
    assert service.handle(context, arguments)["error"]["code"] == "invalid_arguments"
    assert client.calls == []


def test_pointer_modifiers_are_forwarded_and_prevalidated_for_every_step(computer):
    data = capture(computer, foreground=True)["data"]
    result = call(
        computer,
        "click",
        view_id=data["view_id"],
        coordinate=[10, 20],
        modifiers=["ctrl", "shift"],
        apply=True,
    )
    assert result["ok"]
    assert next(args for name, args in computer[2].calls if name == "click")["modifiers"] == [
        "ctrl",
        "shift",
    ]
    before = len(computer[2].calls)
    result = call(
        computer,
        "sequence",
        apply=True,
        steps=[
            {"action": "type", "text": "never"},
            {"action": "click", "view_id": "v", "coordinate": [10, 20], "modifiers": ["invalid"]},
        ],
    )
    assert result["error"]["code"] == "invalid_arguments"
    assert len(computer[2].calls) == before


def test_sequence_zero_completed_steps_uses_failure_envelope(computer):
    from core.tools.tools import is_tool_result_envelope

    capture(computer)
    computer[2].fail = "type_text"
    result = call(computer, "sequence", apply=True, steps=[{"action": "type", "text": "draft"}])
    assert is_tool_result_envelope(result)
    assert not result["ok"] and result["data"] is None
    assert "No sequence step completed successfully" in result["error"]["message"]
    assert computer[2].snapshots == 2


def test_observation_disk_failure_keeps_dispatched_outcome(computer, monkeypatch):
    capture(computer)

    def fail(*args, **kwargs):
        raise OSError("test-owned disk failure")

    monkeypatch.setattr(observations, "capture", fail)
    result = call(computer, "type", text="draft", apply=True)
    assert result["ok"] and result["data"]["applied"]
    assert result["data"]["observation_error"]["code"] == "observation_failed"


def test_transport_loss_during_recapture_retires_all_sessions(computer):
    service, context, client, _ = computer
    capture(computer)
    service._driver = client
    original_call = client.call

    def response(name, args):
        if name in {"get_window_state", "capture_pixels"}:
            client.broken = True
            raise ComputerUseError("test-owned transport failure")
        return original_call(name, args)

    client.call = response
    result = call(computer, "type", text="draft", apply=True)
    assert result["ok"] and result["data"]["applied"]
    assert not service._sessions


def test_failed_session_cleanup_is_retained_for_shutdown_retry(computer):
    service, context, client, _ = computer
    capture(computer)
    client.fail = "end_session"
    service.run_end(context)
    assert len(service._sessions) == 1
    client.fail = None
    service.close()
    assert not service._sessions


def test_cleanup_after_stop_does_not_start_a_replacement_worker(computer):
    service, _, client, _ = computer
    capture(computer)
    service._driver = client
    client.broken = True
    previous = list(client.calls)
    service._close_sessions()
    assert not service._sessions
    assert client.calls == previous


def test_verified_window_disappearance_survives_capture_failure(computer):
    computer[2].fail = "capture_pixels"
    result = call(computer, "verify", expect=[{"window": {"exists": False}}], timeout_ms=0)
    assert result["ok"] and result["data"]["verification"]["verified"]
    assert result["data"]["observation_error"]


def test_idle_stop_and_escape_do_not_affect_later_calls(computer):
    service, context, client, _ = computer
    capture(computer)
    assert not asyncio.run(service.control({}))["active"]
    service.stop()
    for _ in range(2):
        service._hotkey._key_event(0x1B, True, 0)
        service._hotkey._key_event(0x1B, False, 0)
    assert service._hotkey.pending_owner is None
    assert not client.broken
    service.run_end(context)
    assert service.handle(replace(context, run_id="new-run"), {"action": "apps"})["ok"]


@pytest.mark.parametrize("capture_after", [False, True])
def test_pending_escape_stops_sequence_but_not_next_call(computer, capture_after):
    service, context, client, _ = computer
    capture(computer)

    def escape(name):
        if name == "type_text":
            service._hotkey._key_event(0x1B, True, 0)
            service._hotkey._key_event(0x1B, False, 0)
            service._hotkey._key_event(0x1B, True, 0)

    client.hook = escape
    result = call(
        computer,
        "sequence",
        capture_after=capture_after,
        steps=[
            {"action": "type", "text": "first"},
            {"action": "type", "text": "must not reach application"},
        ],
    )
    data = result["data"]
    assert data["completed_steps"] == 1 and data["stopped_step"] == 2
    assert data["partial"] and data["error"]["code"] == "computer_use_interrupted"
    assert data["observation_error"]["code"] == "computer_use_interrupted"
    assert sum(name == "type_text" for name, _ in client.calls) == 1
    # Even an undrained hotkey notification belongs only to the previous call.
    owner = service._hotkey.pending_owner
    assert owner is not None
    client.hook = lambda name: service._hotkey.callback(owner)
    assert service.handle(context, {"action": "apps"})["ok"]
    assert not client.broken


@pytest.mark.parametrize("capture_after", [False, True])
def test_interrupted_single_input_keeps_effect_and_notifies_agent(computer, capture_after):
    service, _, client, _ = computer
    capture(computer)
    client.hook = lambda name: service.stop() if name == "type_text" else None
    result = call(computer, "type", text="draft", capture_after=capture_after)
    assert result["ok"] and result["data"]["applied"]
    error = result["data"]["observation_error" if capture_after else "error"]
    assert error["code"] == "computer_use_interrupted"
    assert sum(name == "type_text" for name, _ in client.calls) == 1
    client.hook = None
    assert call(computer, "capture")["ok"]


def test_run_completion_and_shutdown_never_persist_a_stop(computer):
    service, context, _, _ = computer
    capture(computer)
    service.run_end(context)
    assert not service._sessions
    service.close()
    assert not (context.data_root / "computer-use-stopped").exists()


def test_short_view_ids_keep_previous_images_and_stale_view_rejection(computer, monkeypatch):
    from core.utils import ids

    first = capture(computer)["data"]
    assert first["view_id"].startswith("view_")
    assert len(first["view_id"]) == 17
    alphabet = "0123456789abcdefghjkmnpqrstvwxyz"
    value = 0
    for char in first["view_id"].removeprefix("view_"):
        value = value * 32 + alphabet.index(char)
    original_path = Path(computer[1].presentation_images[-1]["path"])
    original = original_path.read_bytes()
    # Force the displayed-image allocation to collide with the previous view.
    sequence = iter((value, value, (value + 1) % (1 << 60)))
    monkeypatch.setattr(ids.secrets, "randbits", lambda bits: 1 if bits == 80 else next(sequence))
    second = capture(computer)["data"]
    assert second["view_id"] != first["view_id"]
    assert original_path.read_bytes() == original
    result = call(computer, "click", view_id=first["view_id"], coordinate=[1, 1], apply=True)
    assert result["error"]["code"] == "stale_view"


def test_background_capture_input_and_zoom_keep_one_coordinate_space(computer):
    data = capture(computer, foreground=False)["data"]
    assert data["foreground"] is False
    client = computer[2]
    assert client.calls[-1][1]["_background_capture"] is True
    zoomed = call(
        computer, "zoom", view_id=data["view_id"], coordinate=[0, 0], to_coordinate=[100, 100]
    )
    result = call(
        computer,
        "click",
        view_id=zoomed["data"]["view_id"],
        coordinate=[10, 20],
        foreground=False,
        apply=True,
    )
    assert result["ok"]
    args = next(args for name, args in client.calls if name == "click")
    assert args["delivery_mode"] == "background" and args["x"] == 10 and args["y"] == 20
    assert result["data"]["observation"]["foreground"] is False


@pytest.mark.parametrize("sequence", [False, True])
def test_switching_capture_delivery_refuses_before_any_input(computer, sequence):
    data = capture(computer, foreground=False)["data"]
    pointer = {"action": "click", "view_id": data["view_id"], "coordinate": [10, 20]}
    arguments = (
        {"action": "sequence", "steps": [{"action": "type", "text": "never"}, pointer]}
        if sequence
        else pointer
    )
    result = call(computer, **arguments, apply=True, foreground=True)
    assert result["error"]["code"] == "capture_required"
    assert computer[2].inputs == 0


def test_background_timed_sequence_rejects_before_any_input(computer):
    capture(computer, foreground=False)
    result = call(
        computer,
        "sequence",
        foreground=False,
        apply=True,
        steps=[
            {"action": "type", "text": "never"},
            {"action": "key", "shortcut": "enter", "duration_ms": 100},
        ],
    )
    assert result["error"]["code"] == "invalid_arguments"
    assert computer[2].inputs == 0


def test_background_focus_change_stops_sequence_after_dispatched_step(computer):
    data = capture(computer, foreground=False)["data"]
    client = computer[2]
    original = client.call

    def response(name, args):
        result = original(name, args)
        if name == "click":
            result["target_became_foreground"] = True
        return result

    client.call = response
    result = call(
        computer,
        "sequence",
        foreground=False,
        apply=True,
        steps=[
            {"action": "click", "view_id": data["view_id"], "coordinate": [10, 20]},
            {"action": "type", "text": "never"},
        ],
    )
    assert result["data"]["completed_steps"] == 1
    assert result["data"]["partial"]
    assert result["data"]["error"]["code"] == "background_focus_changed"
    assert client.inputs == 1


def test_window_defaults_execute_once_in_background_with_a_compact_image(computer):
    service, context, client, _ = computer
    original = client.call

    def shallow_capture(name, args):
        payload = original(name, args)
        if name == "capture_pixels":
            payload.update(elements_complete=False, degraded=True)
        return payload

    client.call = shallow_capture
    first = service.handle(context, {"action": "capture", "pid": 1, "window_id": 2})
    assert first["data"]["foreground"] is False
    assert first["data"]["mode"] == "vision"
    assert "elements" not in first["data"]
    assert "degraded" not in first["data"] and "elements_complete" not in first["data"]
    result = service.handle(
        context,
        {
            "action": "click",
            "view_id": first["data"]["view_id"],
            "coordinate": [10, 20],
        },
    )
    assert result["ok"] and result["data"]["applied"]
    assert client.inputs == 1
    inputs = [args for name, args in client.calls if name == "click"]
    assert inputs[0]["delivery_mode"] == "background"
    assert inputs[0]["pid"] == 1 and inputs[0]["window_id"] == 2
    assert len(context.result_media) == 2
    assert len(json.dumps(result)) < 1000
    assert result["data"]["observation"]["target"] == {"pid": 1, "window_id": 2}


def test_launch_without_apply_is_not_a_silent_preview(computer):
    service, context, client, _ = computer
    result = service.handle(context, {"action": "launch", "app": "test-owned-app"})
    assert result["ok"] and result["data"]["applied"]
    assert client.inputs == 1


@pytest.mark.parametrize("duration", [None, 1800])
@pytest.mark.parametrize("sequence", [False, True])
def test_background_drawing_preserves_the_requested_duration(computer, duration, sequence):
    data = capture(computer)["data"]
    step = {"action": "drag", "coordinate": [10, 20], "to_coordinate": [50, 60]}
    if duration is not None:
        step["duration_ms"] = duration
    args = {"action": "sequence", "steps": [step]} if sequence else step
    result = call(computer, **args, view_id=data["view_id"])
    assert result["ok"] and result["data"]["applied"]
    inputs = [args for name, args in computer[2].calls if name == "drag"]
    assert len(inputs) == 1
    assert inputs[0]["delivery_mode"] == "background"
    assert inputs[0]["duration_ms"] == (250 if duration is None else duration)


def test_zoom_infers_window_keeps_parent_view_and_maps_nested_crops(computer):
    service, context, client, _ = computer
    client.size = (3840, 2160)
    initial = capture(computer)["data"]
    first = service.handle(
        context,
        {
            "action": "zoom",
            "view_id": initial["view_id"],
            "coordinate": [100, 100],
            "to_coordinate": [200, 200],
        },
    )
    assert first["ok"]
    assert first["data"]["target"] == initial["target"]
    second = service.handle(
        context,
        {
            "action": "zoom",
            "view_id": first["data"]["view_id"],
            "coordinate": [10, 20],
            "to_coordinate": [100, 110],
        },
    )
    assert second["ok"] and second["data"]["image_width"] == 90
    parent_again = service.handle(
        context,
        {
            "action": "zoom",
            "view_id": initial["view_id"],
            "coordinate": [200, 100],
            "to_coordinate": [300, 200],
        },
    )
    assert parent_again["ok"] and client.inputs == 0
    result = service.handle(
        context,
        {
            "action": "click",
            "view_id": second["data"]["view_id"],
            "coordinate": [5, 6],
        },
    )
    assert result["ok"]
    sent = next(args for name, args in client.calls if name == "click")
    assert (sent["x"], sent["y"]) == (255, 266)
    assert sent["delivery_mode"] == "background"
    assert (
        service.handle(
            context,
            {
                "action": "click",
                "view_id": initial["view_id"],
                "coordinate": [1, 1],
            },
        )["error"]["code"]
        == "stale_view"
    )


def test_sequence_inherits_one_view_and_reports_each_outcome(computer):
    service, context, client, _ = computer
    data = capture(computer)["data"]
    result = service.handle(
        context,
        {
            "action": "sequence",
            "view_id": data["view_id"],
            "steps": [
                {"action": "click", "coordinate": [10, 20]},
                {"action": "drag", "coordinate": [20, 30], "to_coordinate": [40, 50]},
            ],
        },
    )
    assert result["ok"] and result["data"]["completed_steps"] == 2
    assert result["data"]["total_steps"] == 2
    assert [step["step"] for step in result["data"]["step_results"]] == [1, 2]
    assert all(step["effect"] == "unverifiable" for step in result["data"]["step_results"])
    assert client.inputs == 2 and client.snapshots == 2
    assert len(json.dumps(result)) < 1400


def test_sequence_infers_target_from_first_step_view_before_element_validation(computer):
    service, context, client, _ = computer
    data = capture(computer)["data"]
    result = service.handle(
        context,
        {
            "action": "sequence",
            "steps": [
                {"action": "click", "element": data["elements"][0]["element"]},
                {
                    "action": "drag",
                    "view_id": data["view_id"],
                    "coordinate": [20, 30],
                    "to_coordinate": [40, 50],
                },
            ],
        },
    )
    assert result["ok"] and client.inputs == 2


def test_foreign_view_and_mixed_target_sequence_fail_before_input(computer):
    service, context, client, _ = computer
    first = capture(computer)["data"]
    second = capture(computer, window_id=3)["data"]
    mismatch = call(computer, "click", window_id=3, view_id=first["view_id"], coordinate=[1, 1])
    assert mismatch["error"]["code"] == "invalid_arguments"
    result = service.handle(
        context,
        {
            "action": "sequence",
            "view_id": first["view_id"],
            "steps": [
                {"action": "click", "coordinate": [1, 1]},
                {"action": "click", "view_id": second["view_id"], "coordinate": [1, 1]},
            ],
        },
    )
    assert result["error"]["code"] == "invalid_arguments" and client.inputs == 0


def test_bad_coordinates_do_not_destroy_the_valid_observation(computer):
    data = capture(computer)["data"]
    bad = call(computer, "click", view_id=data["view_id"], coordinate=[9999, 10])
    assert bad["error"]["code"] == "invalid_coordinates"
    assert call(computer, "click", view_id=data["view_id"], coordinate=[10, 10])["ok"]
    assert computer[2].inputs == 1


def test_next_elements_can_be_requested_with_input_and_keep_control_state(computer):
    capture(computer)
    result = call(computer, "key", shortcut="tab", mode="som", query="Draft", limit=10)
    assert result["ok"]
    element = result["data"]["observation"]["elements"][0]
    assert element["role"] == "Edit" and element["label"] == "Draft"
    assert "element_token" not in element and "element_index" not in element
    assert call(computer, "set_value", element=element["element"], text="test-owned")["ok"]


@pytest.mark.parametrize("effect", ["suspected_noop", "partial"])
def test_uncertain_effect_stops_later_steps_and_preserves_verification(computer, effect):
    capture(computer)
    client = computer[2]
    original = client.call

    def respond(name, args):
        payload = original(name, args)
        if name == "click":
            payload.update(effect=effect, verified=False, escalation={"rung": "px"})
        return payload

    client.call = respond
    result = call(
        computer,
        "sequence",
        steps=[
            {"action": "click", "element": "1"},
            {"action": "type", "text": "must not type"},
        ],
    )
    data = result["data"]
    assert data["partial"] and data["completed_steps"] == 1 and data["stopped_step"] == 1
    assert data["step_results"][0]["verified"] is False
    assert data["step_results"][0]["escalation"] == {"rung": "px"}
    assert data["error"]["code"] == "effect_uncertain" and client.inputs == 1


@pytest.mark.parametrize("sequence", [False, True])
def test_blocked_input_returns_a_usable_dialog_observation_without_extra_capture(
    computer, sequence
):
    service, context, client, _ = computer
    capture(computer)
    original = client.call
    blocked = [True]

    def respond(name, args):
        if name == "type_text" and args["window_id"] == 2:
            raise ComputerUseError("test-owned blocked window", "target_blocked")
        if name == "resolve_window" and blocked[0]:
            return {"pid": 1, "window_id": 3}
        return original(name, args)

    client.call = respond
    result = (
        call(computer, "sequence", steps=[{"action": "type", "text": "draft"}])
        if sequence
        else call(computer, "type", text="draft")
    )
    assert not result["ok"] and result["error"]["code"] == "target_blocked"
    recovery = result["artifacts"][0]
    assert recovery["applied"] is False and client.inputs == 0
    observed = recovery["observation"]
    assert observed["target"] == {"pid": 1, "window_id": 3}
    assert observed["requested_target"] == {"pid": 1, "window_id": 2}
    blocked[0] = False
    assert service.handle(
        context,
        {
            "action": "click",
            "view_id": observed["view_id"],
            "coordinate": [10, 10],
        },
    )["ok"]
    assert client.inputs == 1


def test_computer_skill_is_discoverable_from_the_loaded_extension():
    from core.skills import SkillRegistry

    root = Path(computer_use.__file__).parent / "skills"
    registry = SkillRegistry.load(root)
    skill = registry.get("computer-use")
    assert skill is not None and skill.name == "computer-use"


def test_query_requests_elements_and_partial_target_stays_invalid(computer):
    service, context, client, _ = computer
    data = service.handle(
        context,
        {
            "action": "capture",
            "pid": 1,
            "window_id": 2,
            "query": "Draft",
        },
    )["data"]
    assert data["mode"] == "som" and data["elements"]
    result = service.handle(
        context,
        {
            "action": "zoom",
            "pid": 1,
            "view_id": data["view_id"],
            "coordinate": [1, 1],
            "to_coordinate": [50, 50],
        },
    )
    assert result["error"]["code"] == "invalid_arguments" and client.inputs == 0


def test_stale_sequence_root_view_cannot_fall_back_to_the_desktop(computer):
    service, context, client, _ = computer
    service.handle(context, {"action": "capture"})
    result = service.handle(
        context,
        {
            "action": "sequence",
            "view_id": "view_missing",
            "steps": [{"action": "type", "text": "must not type"}],
        },
    )
    assert result["error"]["code"] == "stale_view" and client.inputs == 0
