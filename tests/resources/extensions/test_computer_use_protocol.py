"""Computer use: protocol behavior."""

from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest

from resources.extensions.computer_use import extension as computer_use
from resources.extensions.computer_use import observations
from resources.extensions.computer_use.driver import ComputerUseError, unpack
from tests.resources.extensions.computer_use_helpers import (
    call,
    capture,
    png,
)
from tests.resources.extensions.computer_use_helpers import (
    computer as computer,
)


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
