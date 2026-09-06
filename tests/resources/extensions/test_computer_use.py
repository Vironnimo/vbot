"""Computer Use Extension driver and authorization regressions."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from resources.extensions.computer_use import extension as computer_use


class FakeClient(computer_use.CuaDriverCli):
    def __init__(self, responses: list[Any] | None = None) -> None:
        self.responses = iter(responses or [])
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def version(self) -> str:
        return "cua-driver 1.2.3"

    def call(self, tool: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((tool, arguments))
        return next(self.responses, {})


def test_capture_normalizes_screenshot_and_accessibility_data(tmp_path: Path) -> None:
    screenshot = base64.b64encode(b"fake-png").decode("ascii")
    client = FakeClient(
        [
            {
                "structuredContent": {
                    "screenshot_png_b64": screenshot,
                    "tree_markdown": "- button Save",
                    "elements": [{"index": 14, "role": "button", "name": "Save"}],
                }
            }
        ]
    )

    result = computer_use._execute(
        {
            "session": "desktop-test",
            "timeout": 45,
            "action": "capture",
            "pid": 1234,
            "window_id": 5678,
            "mode": "som",
        },
        client=client,
        cwd=tmp_path,
    )

    screenshot_path = Path(result["screenshot"])
    assert screenshot_path.read_bytes() == b"fake-png"
    assert screenshot_path.is_relative_to(tmp_path / "tmp" / "computer-use" / "desktop-test")
    assert result["tree"] == "- button Save"
    assert result["elements"][0]["index"] == 14
    tool, arguments = client.calls[0]
    assert tool == "get_window_state"
    assert arguments["pid"] == 1234
    assert arguments["window_id"] == 5678


def test_click_is_dry_run_without_apply(tmp_path: Path) -> None:
    client = FakeClient()

    result = computer_use._execute(
        {
            "session": "desktop-test",
            "timeout": 45,
            "action": "click",
            "pid": 1234,
            "window_id": 5678,
            "element": "14",
            "x": None,
            "y": None,
            "button": "left",
            "count": 1,
            "apply": False,
            "foreground": False,
        },
        client=client,
        cwd=tmp_path,
    )

    assert result["applied"] is False
    assert result["dry_run"]["element"] == "14"
    assert "element_token" not in result["dry_run"]
    assert client.calls == []


def test_applied_click_resolves_element_index_to_token(tmp_path: Path) -> None:
    screenshot = base64.b64encode(b"fake-png").decode("ascii")
    client = FakeClient(
        [
            {
                "structuredContent": {
                    "screenshot_png_b64": screenshot,
                    "snapshot_id": "s00000042",
                    "elements": [
                        {
                            "element_index": 14,
                            "element_token": "s00000042:14",
                            "role": "Button",
                            "label": "Debug",
                        }
                    ],
                }
            },
            {
                "delivery": {"mode": "background"},
                "effect": "unverifiable",
                "route": "accessibility",
            },
        ]
    )

    computer_use._execute(
        {
            "session": "desktop-test",
            "timeout": 45,
            "action": "capture",
            "pid": 1234,
            "window_id": 5678,
            "mode": "som",
        },
        client=client,
        cwd=tmp_path,
    )
    result = computer_use._execute(
        {
            "session": "desktop-test",
            "timeout": 45,
            "action": "click",
            "pid": 1234,
            "window_id": 5678,
            "element": "14",
            "x": None,
            "y": None,
            "button": "left",
            "count": 1,
            "apply": True,
            "foreground": False,
        },
        client=client,
        cwd=tmp_path,
    )

    assert result["applied"] is True
    assert result["backend"] == {
        "delivery": {"mode": "background"},
        "effect": "unverifiable",
        "route": "accessibility",
    }
    tool, arguments = client.calls[1]
    assert tool == "click"
    assert arguments["element_token"] == "s00000042:14"
    assert "element_index" not in arguments
    assert arguments["pid"] == 1234
    assert arguments["window_id"] == 5678


def test_applied_click_accepts_element_token_directly(tmp_path: Path) -> None:
    directory = computer_use._output_directory(tmp_path, "desktop-test")
    computer_use._write_element_token_map(
        directory,
        {"pid": 1234, "window_id": 5678},
        "s00000042",
        [{"element_index": 14, "element_token": "s00000042:14"}],
    )
    client = FakeClient([{"delivery": {"mode": "background"}, "effect": "unverifiable"}])

    result = computer_use._execute(
        {
            "session": "desktop-test",
            "timeout": 45,
            "action": "click",
            "pid": 1234,
            "window_id": 5678,
            "element": "s00000042:14",
            "x": None,
            "y": None,
            "button": "left",
            "count": 1,
            "apply": True,
            "foreground": False,
        },
        client=client,
        cwd=tmp_path,
    )

    assert result["applied"] is True
    tool, arguments = client.calls[0]
    assert tool == "click"
    assert arguments["element_token"] == "s00000042:14"


def test_applied_click_element_index_requires_prior_capture(tmp_path: Path) -> None:
    client = FakeClient()

    with pytest.raises(computer_use.ComputerUseError) as excinfo:
        computer_use._execute(
            {
                "session": "desktop-test",
                "timeout": 45,
                "action": "click",
                "pid": 1234,
                "window_id": 5678,
                "element": "14",
                "x": None,
                "y": None,
                "button": "left",
                "count": 1,
                "apply": True,
                "foreground": False,
            },
            client=client,
            cwd=tmp_path,
        )

    assert "no matching capture" in str(excinfo.value)
    assert client.calls == []


def test_applied_click_rejects_element_from_different_window(tmp_path: Path) -> None:
    screenshot = base64.b64encode(b"fake-png").decode("ascii")
    client = FakeClient(
        [
            {
                "structuredContent": {
                    "screenshot_png_b64": screenshot,
                    "elements": [
                        {"element_index": 14, "element_token": "s00000042:14", "role": "Button"}
                    ],
                }
            }
        ]
    )

    computer_use._execute(
        {
            "session": "desktop-test",
            "timeout": 45,
            "action": "capture",
            "pid": 1234,
            "window_id": 5678,
            "mode": "som",
        },
        client=client,
        cwd=tmp_path,
    )
    with pytest.raises(computer_use.ComputerUseError) as excinfo:
        computer_use._execute(
            {
                "session": "desktop-test",
                "timeout": 45,
                "action": "click",
                "pid": 1234,
                "window_id": 9999,
                "element": "14",
                "x": None,
                "y": None,
                "button": "left",
                "count": 1,
                "apply": True,
                "foreground": False,
            },
            client=client,
            cwd=tmp_path,
        )

    assert "different window" in str(excinfo.value)
    assert len(client.calls) == 1


def test_applied_scroll_resolves_element_index_to_token(tmp_path: Path) -> None:
    screenshot = base64.b64encode(b"fake-png").decode("ascii")
    client = FakeClient(
        [
            {
                "structuredContent": {
                    "screenshot_png_b64": screenshot,
                    "elements": [
                        {"element_index": 3, "element_token": "s00000042:3", "role": "Pane"}
                    ],
                }
            },
            {"delivery": {"mode": "background"}, "effect": "unverifiable"},
        ]
    )

    computer_use._execute(
        {
            "session": "desktop-test",
            "timeout": 45,
            "action": "capture",
            "pid": 1234,
            "window_id": 5678,
            "mode": "som",
        },
        client=client,
        cwd=tmp_path,
    )
    result = computer_use._execute(
        {
            "session": "desktop-test",
            "timeout": 45,
            "action": "scroll",
            "pid": 1234,
            "window_id": 5678,
            "direction": "down",
            "amount": 3,
            "element": "3",
            "apply": True,
            "foreground": False,
        },
        client=client,
        cwd=tmp_path,
    )

    assert result["applied"] is True
    tool, arguments = client.calls[1]
    assert tool == "scroll"
    assert arguments["element_token"] == "s00000042:3"
    assert "element_index" not in arguments


def test_refused_backend_call_raises_instead_of_reporting_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = computer_use.CuaDriverCli("cua-driver", 5)
    refusal = {
        "status": "refused",
        "refusal": {
            "code": "snapshot_id_required",
            "message": "click: bare element_index is not accepted",
        },
    }
    completed = subprocess.CompletedProcess(
        args=[], returncode=0, stdout=json.dumps(refusal), stderr=""
    )
    monkeypatch.setattr(cli, "_invoke", lambda _arguments: completed)

    with pytest.raises(computer_use.ComputerUseError) as excinfo:
        cli.call("click", {"element_index": 14})

    assert "snapshot_id_required" in str(excinfo.value)


def test_type_does_not_echo_text_in_result(tmp_path: Path) -> None:
    client = FakeClient([{"typed": True, "text": "backend echo"}])

    result = computer_use._execute(
        {
            "session": "desktop-test",
            "timeout": 45,
            "action": "type",
            "pid": 1234,
            "window_id": 5678,
            "text": "private draft",
            "apply": True,
            "foreground": False,
        },
        client=client,
        cwd=tmp_path,
    )

    serialized = json.dumps(result)
    assert "private draft" not in serialized
    assert "backend echo" not in serialized
    assert result["applied"] is True


def test_dangerous_system_shortcut_is_blocked(tmp_path: Path) -> None:
    client = FakeClient()

    with pytest.raises(computer_use.ComputerUseError):
        computer_use._execute(
            {
                "session": "desktop-test",
                "timeout": 45,
                "action": "key",
                "pid": 1234,
                "window_id": 5678,
                "shortcut": "ctrl+alt+delete",
                "apply": True,
                "foreground": False,
            },
            client=client,
            cwd=tmp_path,
        )

    assert client.calls == []


def test_click_requires_one_exact_target_kind(tmp_path: Path) -> None:
    client = FakeClient()

    with pytest.raises(computer_use.ComputerUseError):
        computer_use._execute(
            {
                "session": "desktop-test",
                "timeout": 45,
                "action": "click",
                "pid": 1234,
                "window_id": 5678,
                "element": None,
                "x": None,
                "y": None,
                "button": "left",
                "count": 1,
                "apply": False,
                "foreground": False,
            },
            client=client,
            cwd=tmp_path,
        )

    assert client.calls == []


class DesktopClient:
    def __init__(self):
        self.calls = []
        self.fail_input = False

    def version(self):
        return "test-driver"

    def call(self, name, arguments):
        self.calls.append((name, arguments))
        if name == "get_window_state":
            return {
                "structuredContent": {
                    "screenshot_png_b64": base64.b64encode(b"test-pixels").decode(),
                    "elements": [{"element_index": 1, "element_token": "s00000001:1"}],
                }
            }
        if name in {"click", "type_text", "scroll", "hotkey", "press_key"}:
            if self.fail_input:
                raise computer_use.ComputerUseError("test-owned failure")
            return {"effect": "unverifiable"}
        return {}


@pytest.fixture
def computer(tmp_path, monkeypatch):
    from core.extensions.extensions import ExtensionAPI, ExtensionDeclarations
    from core.tools import ToolContext, ToolRegistry
    from core.tools.availability import ToolAccess

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
    asyncio.run(service.start(SimpleNamespace(resolve_agent=lambda project, name: agent)))
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
    return service, context, client, agent


def _capture(service, context):
    return service.handle(context, {"action": "capture", "pid": 1, "window_id": 2})


def test_tool_captures_pixels_and_applies_only_after_owned_capture(computer):
    service, context, client, agent = computer
    click = {"action": "click", "pid": 1, "window_id": 2, "element": "1", "apply": True}
    assert service.handle(context, click)["error"]["code"] == "capture_required"
    assert _capture(service, context)["ok"]
    assert base64.b64decode(context.result_media[0]["base64"]) == b"test-pixels"
    assert len(context.presentation_images) == 1
    result = service.handle(context, click)
    assert result["ok"] and result["data"]["applied"]
    assert result["data"]["backend"]["effect"] == "unverifiable"
    assert "session" not in result["data"]
    assert client.calls[-1][1]["element_token"] == "s00000001:1"
    assert service.handle(context, click)["error"]["code"] == "capture_required"


def test_revocation_and_cancel_are_rechecked_before_input(computer):
    from core.tools.availability import ToolAccess

    service, context, client, agent = computer
    assert _capture(service, context)["ok"]
    count = len(client.calls)
    agent.tool_access = ToolAccess()
    assert not service.handle(context, {"action": "windows"})["ok"]
    assert len(client.calls) == count
    agent.tool_access = ToolAccess(granted=("computer",))
    context = replace(context, cancellation_hook=lambda: True)
    assert not service.handle(context, {"action": "windows"})["ok"]
    assert len(client.calls) == count


@pytest.mark.parametrize(
    "arguments",
    [
        {"action": "capture", "pid": 1},
        {"action": "windows", "session": "foreign"},
        {"action": "type", "pid": 1, "window_id": 2},
        {"action": "key", "pid": 1, "window_id": 2, "shortcut": "win+l"},
        {"action": "click", "pid": True, "window_id": 2, "x": 2, "y": 3},
        {"action": "capture", "pid": 1, "window_id": 2, "apply": True},
        {"action": "click", "pid": 1, "window_id": 2, "x": 1},
        {"action": "scroll", "pid": 1, "window_id": 2, "direction": "down", "amount": 101},
    ],
)
def test_invalid_tool_arguments_never_start_desktop_work(computer, arguments):
    service, context, client, _ = computer
    assert service.handle(context, arguments)["error"]["code"] == "invalid_arguments"
    assert client.calls == []


@pytest.mark.parametrize(
    "change",
    [{"session_id": "other"}, {"agent_id": "other"}, {"project_id": "other"}, {"run_id": "other"}],
)
def test_capture_authority_does_not_cross_contexts(computer, change):
    service, context, client, _ = computer
    assert _capture(service, context)["ok"]
    foreign = replace(context, **change)
    result = service.handle(
        foreign,
        {"action": "click", "pid": 1, "window_id": 2, "element": "s00000001:1", "apply": True},
    )
    assert result["error"]["code"] == "capture_required"
    assert not any(name == "click" for name, _ in client.calls)
    assert len(set(service._sessions.values())) == 2


def test_failed_input_is_not_retried_and_invalidates_capture(computer):
    service, context, client, _ = computer
    assert _capture(service, context)["ok"]
    client.fail_input = True
    arguments = {"action": "type", "pid": 1, "window_id": 2, "text": "test", "apply": True}
    result = service.handle(context, arguments)
    assert not result["ok"] and result["error"]["retryable"] is False
    assert sum(name == "type_text" for name, _ in client.calls) == 1
    assert service.handle(context, arguments)["error"]["code"] == "capture_required"


def test_cleanup_closes_only_owned_sessions_and_retires_handler(computer):
    service, context, client, _ = computer
    assert _capture(service, context)["ok"]
    assert _capture(service, replace(context, run_id="r2"))["ok"]
    owned = set(service._sessions.values())
    service.run_end(SimpleNamespace(run_id="r"))
    assert len(service._sessions) == 1
    service.close()
    assert not service._sessions
    assert {args["session"] for name, args in client.calls if name == "end_session"} == owned
    count = len(client.calls)
    assert not service.handle(context, {"action": "windows"})["ok"]
    assert len(client.calls) == count


def test_empty_capture_invalidates_previous_element_tokens(computer, monkeypatch):
    service, context, client, _ = computer
    assert _capture(service, context)["ok"]
    monkeypatch.setattr(client, "call", lambda name, args: {"structuredContent": {"elements": []}})
    assert service.handle(context, {"action": "capture", "pid": 1, "window_id": 2, "mode": "ax"})[
        "ok"
    ]
    result = service.handle(
        context,
        {"action": "click", "pid": 1, "window_id": 2, "element": "s00000001:1", "apply": True},
    )
    assert not result["ok"]


def test_missing_driver_stays_configurable_but_not_ready(monkeypatch):
    monkeypatch.setattr(computer_use.shutil, "which", lambda name: None)
    service = computer_use.ComputerUseService(SimpleNamespace())
    assert not service.ready()
    with pytest.raises(computer_use.ComputerUseError):
        service._client()


@pytest.mark.parametrize("output", ["", 'No window exists. Call list_windows({"pid": 42}).'])
def test_plain_driver_errors_are_not_parsed_as_success(output):
    with pytest.raises(computer_use.ComputerUseError, match="non-JSON"):
        computer_use._parse_json_output(output)


def test_driver_json_after_leading_log_is_supported():
    assert computer_use._parse_json_output('driver ready\n{"windows": []}') == {"windows": []}


@pytest.mark.parametrize("mode", ["som", "vision", "ax"])
def test_failed_capture_never_authorizes_input(computer, monkeypatch, mode):
    service, context, client, _ = computer
    assert _capture(service, context)["ok"]
    monkeypatch.setattr(client, "call", lambda name, args: {})
    assert not service.handle(
        context, {"action": "capture", "pid": 1, "window_id": 2, "mode": mode}
    )["ok"]
    result = service.handle(
        context, {"action": "click", "pid": 1, "window_id": 2, "x": 1, "y": 1, "apply": True}
    )
    assert result["error"]["code"] == "capture_required"


def test_cancel_during_session_start_prevents_capture(computer, monkeypatch):
    service, context, client, _ = computer
    cancelled = False

    def start(name, arguments):
        nonlocal cancelled
        client.calls.append((name, arguments))
        cancelled = True
        return {}

    monkeypatch.setattr(client, "call", start)
    context = replace(context, cancellation_hook=lambda: cancelled)
    assert not _capture(service, context)["ok"]
    assert [name for name, _ in client.calls] == ["start_session"]


def test_waiting_call_rechecks_revoked_permission_after_lock(computer):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from core.tools.availability import ToolAccess

    service, context, client, agent = computer
    entered = Event()

    def waiting_call():
        entered.set()
        return service.handle(context, {"action": "windows"})

    with ThreadPoolExecutor(max_workers=1) as executor:
        with service._lock:
            pending = executor.submit(waiting_call)
            assert entered.wait(5)
            agent.tool_access = ToolAccess()
        assert not pending.result(timeout=5)["ok"]
    assert client.calls == []


@pytest.mark.parametrize("element", ["1", "s00000001:1"])
def test_type_targets_only_a_current_captured_element(computer, element):
    service, context, client, _ = computer
    assert _capture(service, context)["ok"]
    arguments = {
        "action": "type",
        "pid": 1,
        "window_id": 2,
        "element": element,
        "text": "test-owned draft",
        "apply": True,
    }
    assert service.handle(context, arguments)["ok"]
    assert client.calls[-1][0] == "type_text"
    assert client.calls[-1][1]["element_token"] == "s00000001:1"
    assert service.handle(context, arguments)["error"]["code"] == "capture_required"
