"""Computer use: the Cua Driver protocol, handshake and request routing."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from jsonschema import validate

from resources.extensions.computer_use import driver
from resources.extensions.computer_use import extension as computer_use
from resources.extensions.computer_use.driver import ComputerUseError, CuaDriver, unpack


@pytest.mark.parametrize(
    "payload,reported",
    [
        (
            {"isError": True, "content": [{"type": "text", "text": "test refusal"}]},
            "test refusal",
        ),
        ({"structuredContent": {"status": "refused", "refusal": {"code": "stale"}}}, "stale"),
        (
            {"content": [{"type": "text", "text": 'error with example {"ok": true}'}]},
            "error with example",
        ),
        ({"structuredContent": {"effect": "refused", "message": "denied"}}, "denied"),
    ],
)
def test_protocol_errors_never_become_success(payload, reported):
    with pytest.raises(ComputerUseError, match=reported):
        unpack(payload)


def test_protocol_preserves_structured_state_and_png():
    assert unpack(
        {
            "structuredContent": {"elements": []},
            "content": [{"type": "image", "mimeType": "image/png", "data": "abc"}],
        }
    ) == {"elements": [], "screenshot_png_b64": "abc"}


def test_missing_driver_is_not_ready_and_refuses_calls(computer, monkeypatch):
    service, context, client, _ = computer
    monkeypatch.setattr(computer_use.shutil, "which", lambda _: None)
    missing = computer_use.ComputerUseService(service.api)
    asyncio.run(missing.start(service.host))
    assert not missing.ready()
    result = missing.handle(context, {"action": "apps"})
    assert result["error"]["code"] == "tool_not_ready" and not client.calls


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
            assert events == [
                "open",
                "get_config",
                "start_session",
                "click",
                "start_session",
                "click",
            ]
    finally:
        client.close()
    assert events.count("open") == events.count("close") == 1
    assert "set_config" not in events


def test_window_pixels_are_captured_after_accessibility_query(monkeypatch):
    client = CuaDriver.__new__(CuaDriver)
    order = []

    def capture(args):
        order.append("pixels")
        return {"screen_origin": [0, 0]}

    def query(function, name, args):
        if name == "start_session":
            assert args == {}
            order.append("session")
            return SimpleNamespace(model_dump=lambda **kwargs: {"structuredContent": {}})
        assert name == "get_window_state" and args["include_screenshot"] is False
        order.append("elements")
        return SimpleNamespace(model_dump=lambda **kwargs: {"structuredContent": {"elements": []}})

    client.desktop = SimpleNamespace(capture=capture)
    client._portal = SimpleNamespace(call=query)
    client._session = SimpleNamespace(call_tool=object())
    client.schemas = {"get_window_state": {}}
    monkeypatch.setattr(client, "connect", lambda: None)
    result = client.call("get_window_state", {"pid": 1, "window_id": 2})
    assert order == ["session", "elements", "pixels"]
    assert result["screen_origin"] == [0, 0] and result["elements"] == []


@pytest.mark.parametrize("action,refuse", [("click", False), ("drag", True)])
def test_background_pixels_use_cua_without_foreground_fallback(monkeypatch, action, refuse):
    client = CuaDriver.__new__(CuaDriver)
    calls = []
    geometry = [1]
    client._background_frames = {}
    client._original_images = True
    client.desktop = SimpleNamespace(
        window_geometry=lambda args: tuple(geometry),
        foreground_window=lambda: 99,
        resolve_window=lambda args: {key: args[key] for key in ("pid", "window_id")},
    )
    client.schemas = {"get_window_state": {}, action: {"properties": {"target": {}}}}
    client._session = SimpleNamespace(call_tool=object())

    def query(function, name, args):
        calls.append((name, args))
        assert "_background_capture" not in args
        result = {"elements": []}
        if name == action:
            assert args["delivery_mode"] == "background"
            assert args["target"] == {"kind": "window", "pid": 1, "window_id": 2}
            assert "pid" not in args and "window_id" not in args
            if action == "drag":
                assert args["duration_ms"] == 1800
            result = {
                "effect": "refused" if refuse else "unverifiable",
                "route": "synthetic_events",
            }
        return SimpleNamespace(model_dump=lambda **kwargs: {"structuredContent": result})

    client._portal = SimpleNamespace(call=query)
    monkeypatch.setattr(client, "connect", lambda: None)
    target = {"pid": 1, "window_id": 2, "session": "s"}
    client.call("get_window_state", {**target, "_background_capture": True})
    coordinates = (
        {"x": 10, "y": 20}
        if action == "click"
        else {"from_x": 10, "from_y": 20, "to_x": 30, "to_y": 40, "duration_ms": 1800}
    )
    request = {**target, **coordinates, "delivery_mode": "background"}
    if refuse:
        with pytest.raises(ComputerUseError):
            client.call(action, request)
    else:
        client.call(action, request)
    assert [name for name, _ in calls] == [
        "start_session",
        "get_window_state",
        "start_session",
        action,
    ]
    previous_calls = len(calls)
    geometry[0] = 2
    with pytest.raises(ComputerUseError) as caught:
        client.call(action, request)
    assert caught.value.code == "capture_required" and len(calls) == previous_calls


def test_agent_cursor_uses_the_cua_target_contract_without_shared_pointer_fields(monkeypatch):
    client = CuaDriver.__new__(CuaDriver)
    client.desktop = None
    client.schemas = {
        "move_cursor": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "target": {"type": "object", "required": ["kind", "pid", "window_id"]},
                "x": {"type": "number"},
                "y": {"type": "number"},
                "session": {"type": "string"},
            },
            "required": ["target", "x", "y"],
        }
    }
    client._session = SimpleNamespace(call_tool=object())
    sent = []

    def query(function, name, args):
        validate(args, {"type": "object"} if name == "start_session" else client.schemas[name])
        sent.append(args)
        return SimpleNamespace(
            model_dump=lambda **kwargs: {"structuredContent": {"effect": "unverifiable"}}
        )

    client._portal = SimpleNamespace(call=query)
    monkeypatch.setattr(client, "connect", lambda: None)
    client.call(
        "move_cursor",
        {
            "pid": 1,
            "window_id": 2,
            "x": 10,
            "y": 20,
            "delivery_mode": "background",
            "session": "owned",
        },
    )
    assert sent == [
        {"session": "owned"},
        {
            "target": {"kind": "window", "pid": 1, "window_id": 2},
            "x": 10,
            "y": 20,
            "session": "owned",
        },
    ]
