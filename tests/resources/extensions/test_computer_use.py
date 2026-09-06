"""Computer Use integration regressions with real PNGs and controlled driver effects."""

from __future__ import annotations

import asyncio
import base64
import io
import logging
from dataclasses import replace
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
    args = {"action": "capture", "pid": 1, "window_id": 2, **kwargs}
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
    assert result["data"]["backend"]["effect"] == "unverifiable"
    assert "backend-echo" not in str(result) and "private draft" not in str(result)
    assert call(computer, "key", shortcut="enter", apply=True)["ok"]
    assert len(computer[1].result_media) == 3


def test_preview_sends_no_input_and_does_not_consume_capture(computer):
    assert capture(computer)["ok"]
    before = len(computer[2].calls)
    assert call(computer, "type", text="private draft")["data"]["preview"]
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
    assert Image.open(result["original"]).size == (3840, 2160)
    zoom = call(computer, "zoom", view_id=result["view_id"], x=100, y=100, x2=200, y2=200)["data"]
    assert (zoom["image_width"], zoom["image_height"]) == (240, 240)
    clicked = call(computer, "click", view_id=zoom["view_id"], x=50, y=60, apply=True)
    assert clicked["ok"]
    _, args = next(item for item in computer[2].calls if item[0] == "click")
    assert (args["x"], args["y"]) == (290, 300)
    assert (
        call(computer, "click", view_id=result["view_id"], x=1, y=1, apply=True)["error"]["code"]
        == "stale_view"
    )


def test_original_resolution_and_image_edges(computer):
    computer[2].size = (1800, 1000)
    data = capture(computer, resolution="original")["data"]
    assert data["image_width"] == 1800
    result = call(computer, "click", view_id=data["view_id"], x=1800, y=2, apply=True)
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
            "x": 1,
            "y": 1,
            "apply": True,
        },
    )
    assert result["error"]["code"] == "capture_required"
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
            {"action": "click", "view_id": data["view_id"], "x": 9000, "y": 10},
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
                {"action": "move", "view_id": data["view_id"], "x": 10, "y": 10},
                {"action": "click", "view_id": data["view_id"], "x": 20, "y": 20},
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
        {"action": "click", "pid": 1, "window_id": 2, "x": 1, "y": 1},
        {"action": "key", "pid": 1, "window_id": 2, "shortcut": "+"},
        {"action": "scroll", "pid": 1, "window_id": 2, "direction": "down", "amount": 101},
        {"action": "capture", "scope": "desktop", "pid": 1, "window_id": 2},
        {"action": "capture", "scope": "desktop", "mode": "ax"},
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
        ("resize", {"x": -100, "y": 0, "width": 900, "height": 700}, "set_window_frame"),
        ("drag", {"x": 1, "y": 2, "x2": 20, "y2": 30}, "drag"),
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
    data = service.handle(context, {"action": "capture", "scope": "desktop"})["data"]
    result = service.handle(
        context,
        {
            "action": "click",
            "scope": "desktop",
            "view_id": data["view_id"],
            "x": 10,
            "y": 10,
            "apply": True,
        },
    )
    assert result["ok"]
    _, payload = next(item for item in client.calls if item[0] == "click")
    assert payload["scope"] == "desktop" and "pid" not in payload


def test_verify_reports_structured_result_and_fresh_observation(computer):
    result = call(computer, "verify", expect=[{"window": {"exists": True}}], timeout_ms=0)
    assert result["ok"] and result["data"]["verification"]["verified"]
    assert result["data"]["observation"]


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
    data = service.handle(context, {"action": "capture", "scope": "desktop"})["data"]
    assert service.handle(
        context,
        {
            "action": "click",
            "scope": "desktop",
            "view_id": data["view_id"],
            "x": 10,
            "y": 20,
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
                    SimpleNamespace(name="click", input_schema={}),
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
            client.call("click", {})
            client.call("click", {})
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
    client.schemas = {"click": {}}
    with pytest.raises(ComputerUseError):
        client.call("click", {})
    assert calls == ["click"] and client.broken and client._session is None


def test_cancel_interrupts_inflight_driver_and_latches_until_operator_resume(computer):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    service, context, client, _ = computer
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
        callbacks[0]()
        assert not future.result(timeout=2)["ok"]
    assert service._stop_file.exists()
    calls = len(client.calls)
    result = service.handle(context, {"action": "apps"})
    assert result["error"]["code"] == "computer_use_stopped"
    assert len(client.calls) == calls
    client.hook = None
    status = asyncio.run(service.control({"action": "resume", "stop_token": service._stop_token}))
    assert not status["paused"] and not service._stop_file.exists()
    assert service.handle(context, {"action": "apps"})["ok"]


def test_late_cancel_does_not_stop_next_call(computer):
    service, context, client, _ = computer
    callbacks = []
    context = replace(context, cancel_registration_hook=callbacks.append)
    assert service.handle(context, {"action": "apps"})["ok"]
    client.hook = lambda name: callbacks[0]()
    assert service.handle(context, {"action": "apps"})["ok"]
    assert not asyncio.run(service.control({}))["paused"]
    assert not client.broken


def test_global_stop_persists_across_reload_and_resume_waits_for_active_call(computer):
    service, _, _, _ = computer
    service._active = object()
    asyncio.run(service.control({"action": "stop"}))
    assert asyncio.run(service.control({"action": "resume", "stop_token": service._stop_token}))[
        "paused"
    ]
    service._active = None
    replacement = computer_use.ComputerUseService(service.api)
    asyncio.run(replacement.start(service.host))
    assert asyncio.run(replacement.control({}))["paused"]
    asyncio.run(replacement.control({"action": "resume", "stop_token": replacement._stop_token}))
    replacement.close()


def test_stale_operator_resume_cannot_undo_a_newer_stop(computer):
    service, _, _, _ = computer
    first = asyncio.run(service.control({"action": "stop"}))
    second = asyncio.run(service.control({"action": "stop"}))
    assert first["stop_token"] != second["stop_token"]
    stale = asyncio.run(service.control({"action": "resume", "stop_token": first["stop_token"]}))
    assert stale["paused"]
    current = asyncio.run(service.control({"action": "resume", "stop_token": second["stop_token"]}))
    assert not current["paused"]


def test_os_interrupt_failure_still_persists_stop(computer):
    service, context, client, _ = computer

    def denied():
        raise PermissionError("test-owned termination failure")

    client.interrupt = denied
    service._driver = client
    service.stop()
    assert service._stop_file.exists()
    assert service.handle(context, {"action": "apps"})["error"]["code"] == "computer_use_stopped"
    assert not client.calls


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
    assert failure.value.code == "computer_use_stopped"
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
        result = {"tools": [{"name": "click", "inputSchema": {"type": "object"}}]}
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
        target = {key: args[key] for key in ("pid", "window_id", "scope", "monitor") if key in args}
        observed = service.handle(context, {"action": "capture", **target})
        assert observed["ok"], case
        if "view_id" in args:
            args["view_id"] = observed["data"]["view_id"]
        if ":" in args.get("element", ""):
            args["element"] = observed["data"]["elements"][0]["element_token"]
        result = service.handle(context, args)
        assert result["ok"], (case, result)


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
        if name == "get_window_state":
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
    computer[2].fail = "get_window_state"
    result = call(computer, "verify", expect=[{"window": {"exists": False}}], timeout_ms=0)
    assert result["ok"] and result["data"]["verification"]["verified"]
    assert result["data"]["observation_error"]
