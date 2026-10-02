"""The computer Tool through production dispatch: permission, readiness, input and images."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import cast

import pytest

from core.extensions.extensions import ExtensionAPI, ExtensionDeclarations
from core.extensions.operations import ExtensionHost
from core.tools.availability import ToolAccess
from resources.extensions.computer_use import extension as computer_use
from resources.extensions.computer_use.extension import _new_target as platform_target
from tests.resources.extensions.computer_use.computer_use_test_support import (
    PAINT,
    TOOLS,
    Harness,
    images,
    model_text,
    registered_service,
)

pytestmark = pytest.mark.asyncio


async def test_tools_register_as_one_opt_in_family_with_stop_control(computer: Harness) -> None:
    for name in TOOLS:
        tool = computer.registry.get(name)
        assert tool.requires_opt_in and not tool.parallel_safe
        assert tool.family == "computer_use" and tool.extension == "computer_use"
    assert computer.registry.get("computer").display.summary(
        {"action": "left_click", "action_summary": "Opens the File menu"}
    ) == ("left_click · Opens the File menu")
    status = await computer.api.operations.invoke("control", {"action": "status"})
    assert status == {
        "available": True,
        "active": False,
        "stopping": False,
        "hotkey_available": False,
    }
    # The global keyboard hook starts with the first call that takes the desktop.
    await computer.call("computer_apps", {"action": "list"})
    assert computer.hotkeys == []
    await computer.computer(action="screenshot")
    assert computer.hotkey.started and computer.hotkey.armed is None
    status = await computer.api.operations.invoke("control", {"action": "status"})
    assert status["hotkey_available"] is True


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("computer", {"action": "screenshot"}),
        ("computer_batch", {"actions": [{"action": "screenshot"}]}),
        ("computer_apps", {"action": "list"}),
    ],
)
async def test_agent_without_the_permission_never_reaches_the_desktop(
    computer: Harness, tool: str, arguments: dict
) -> None:
    computer.agent.tool_access = ToolAccess(granted=tuple(name for name in TOOLS if name != tool))
    context = computer.context_for(tool)
    result = await computer.call(tool, arguments, context)
    assert result["error"]["code"] == "tool_not_allowed"
    assert f"allow {tool}" in result["error"]["message"]
    assert context.result_media == [] and computer.target.inputs == []


async def test_call_outside_a_session_does_nothing(computer: Harness) -> None:
    context = computer.context_for("computer", session_id="")
    result = await computer.call("computer", {"action": "screenshot"}, context)
    assert result["error"]["code"] == "computer_use_unavailable"
    assert "Session" in result["error"]["message"] and context.result_media == []


async def test_other_hosts_hide_the_tools_and_explain_why(
    computer: Harness, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(computer_use, "_new_target", platform_target)
    monkeypatch.setattr(computer_use.sys, "platform", "linux")
    declarations = ExtensionDeclarations()
    api = ExtensionAPI("computer_use", declarations, config={}, logger=logging.getLogger("test"))
    computer_use.register(api)
    service = registered_service(declarations)
    host = SimpleNamespace(resolve_tool_agent=None, publish_change=None)
    await service.start(cast(ExtensionHost, host))
    monkeypatch.undo()
    try:
        assert not service.ready()
        assert all(
            item.readiness_hint == computer_use.READINESS_HINT for item in declarations.tools
        )
        result = await service.computer(computer.context, {"action": "screenshot"})
        assert result["error"] == {
            "code": "computer_use_unavailable",
            "message": computer_use.UNSUPPORTED,
            "retryable": False,
        }
    finally:
        await service.close()


async def test_a_target_that_becomes_unusable_refuses_calls(computer: Harness) -> None:
    computer.target.reason = "The desktop is locked."
    result = await computer.computer(action="screenshot")
    assert result["error"] == {
        "code": "computer_use_unavailable",
        "message": "The desktop is locked.",
        "retryable": False,
    }
    assert not computer.service.ready()


async def test_screenshot_states_the_frame_and_keeps_the_chosen_display(computer: Harness) -> None:
    context = computer.context_for("computer")
    result = await computer.call("computer", {"action": "screenshot"}, context)
    assert model_text(result) == 'Screenshot of display 1 of 2 "Main": 1280x720 frame.' + (
        " Hidden apps (gray, not granted): Notepad, Google Chrome, Windows Terminal, Slack, "
        "vBot, File Explorer."
    )
    assert [image.size for image in images(context)] == [(1280, 720)]

    context = computer.context_for("computer")
    result = await computer.call(
        "computer", {"action": "screenshot", "display": "2", "scale": 0.5}, context
    )
    assert model_text(result).startswith(
        'Screenshot of display 2 of 2 "Wide": 1568x500 frame, image scaled to 784x250; '
        "coordinates use the 1568x500 frame."
    )
    assert [image.size for image in images(context)] == [(784, 250)]
    result = await computer.computer(action="screenshot")
    assert '"Wide"' in model_text(result)
    result = await computer.computer(action="screenshot", display="auto")
    assert '"Main"' in model_text(result)

    result = await computer.computer(action="screenshot", display="Projector")
    assert result["error"]["code"] == "invalid_arguments"
    assert '1. "Main" 1280x720 (primary, current)\n2. "Wide" 3136x1000' in model_text(result)


async def test_zoom_shows_a_region_at_physical_resolution_in_the_same_frame(
    computer: Harness,
) -> None:
    await computer.computer(action="screenshot", display="Wide")
    context = computer.context_for("computer")
    result = await computer.call(
        "computer", {"action": "zoom", "region": [0, 0, 200, 100]}, context
    )
    assert model_text(result).startswith(
        "Zoom of [0, 0, 200, 100]: image 400x200, 2.0x the screenshot. Coordinates still use "
        'the 1568x500 frame of display "Wide".'
    )
    assert [image.size for image in images(context)] == [(400, 200)]
    result = await computer.computer(action="zoom", region=[1500, 0, 1600, 100])
    assert result["error"]["code"] == "invalid_arguments"
    assert "1568x500" in result["error"]["message"]


async def grant_paint_on_the_wide_display(computer: Harness) -> None:
    await computer.grant("Paint", "Notepad")
    computer.target.front(PAINT)
    await computer.computer(action="screenshot")
    computer.target.pointer = (-2000, 500)
    computer.target.inputs.clear()
    computer.sleeps.clear()


# Wide display: frame 1568x500 at x=-3136, so frame [x, y] is physical [-3136 + 2x, 2y].
@pytest.mark.parametrize(
    ("arguments", "inputs"),
    [
        (
            {"action": "left_click", "coordinate": [100, 200]},
            [("click", -2936, 400, "left", 1, [])],
        ),
        (
            {"action": "right_click", "coordinate": [100, 200], "text": "shift"},
            [("click", -2936, 400, "right", 1, ["shift"])],
        ),
        ({"action": "middle_click"}, [("click", -2000, 500, "middle", 1, [])]),
        (
            {"action": "double_click", "coordinate": [100, 200]},
            [("click", -2936, 400, "left", 2, [])],
        ),
        (
            {"action": "triple_click", "coordinate": [100, 200]},
            [("click", -2936, 400, "left", 3, [])],
        ),
        ({"action": "mouse_move", "coordinate": [500, 250]}, [("move", -2136, 500)]),
        (
            {
                "action": "left_click_drag",
                "start_coordinate": [100, 100],
                "coordinate": [200, 150],
                "text": "alt",
            },
            [("drag", (-2936, 200), (-2736, 300), ["alt"])],
        ),
        (
            {"action": "left_click_drag", "coordinate": [200, 150]},
            [("drag", (-2000, 500), (-2736, 300), [])],
        ),
        (
            {"action": "left_mouse_up", "coordinate": [300, 300]},
            [("move", -2536, 600), ("button", "left", False)],
        ),
        (
            {
                "action": "scroll",
                "coordinate": [400, 200],
                "scroll_direction": "down",
                "scroll_amount": 5,
                "text": "ctrl",
            },
            [("scroll", -2336, 400, "down", 5, ["ctrl"])],
        ),
        ({"action": "scroll", "scroll_direction": "left"}, [("scroll", -2000, 500, "left", 3, [])]),
        ({"action": "type", "text": "héllo\nwörld"}, [("type", "héllo\nwörld")]),
        (
            {"action": "key", "text": "ctrl+shift+t Return", "repeat": 2},
            [("keys", ["ctrl", "shift", "t"], 1), ("keys", ["enter"], 1)] * 2,
        ),
        ({"action": "key", "text": "Page_Down", "repeat": 3}, [("keys", ["pagedown"], 3)]),
        ({"action": "hold_key", "text": "shift", "duration": 2}, [("hold", ["shift"], 2.0)]),
    ],
)
async def test_input_actions_map_frame_coordinates_and_return_a_screenshot(
    computer: Harness, arguments: dict, inputs: list
) -> None:
    await grant_paint_on_the_wide_display(computer)
    context = computer.context_for("computer")
    result = await computer.call("computer", arguments, context)
    assert result["ok"], result
    assert computer.target.inputs == inputs
    assert computer.sleeps == [computer_use.SETTLE_SECONDS]
    assert len(images(context)) == 1
    assert model_text(result).startswith("Done: ")


async def test_mouse_down_and_cursor_position_return_no_screenshot(computer: Harness) -> None:
    await grant_paint_on_the_wide_display(computer)
    context = computer.context_for("computer")
    result = await computer.call("computer", {"action": "left_mouse_down"}, context)
    assert model_text(result) == "Done: left_mouse_down at the pointer."
    assert computer.target.inputs == [("button", "left", True)] and context.result_media == []
    result = await computer.computer(action="cursor_position")
    assert (
        model_text(result)
        == 'The pointer is at [568, 250] in the 1568x500 frame of display "Wide".'
    )
    computer.target.pointer = (300, 300)
    result = await computer.computer(action="cursor_position")
    assert model_text(result).startswith(
        'The pointer is outside the current display on display "Main"'
    )


async def test_wait_pauses_then_shows_the_screen(computer: Harness) -> None:
    context = computer.context_for("computer")
    result = await computer.call("computer", {"action": "wait", "duration": 2}, context)
    assert computer.sleeps == [2] and len(images(context)) == 1
    assert model_text(result).startswith("Waited 2 s.\nScreenshot")
    result = await computer.computer(action="wait", duration=1500)
    assert computer.sleeps[-1] == 1.5
    assert model_text(result).endswith(
        "duration 1500 was read as milliseconds (1.5 s); duration is in seconds."
    )


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"action": "left_click", "coordinate": [1300, 100]}, "outside the latest screenshot"),
        ({"action": "type", "text": "x", "coordinate": [200, 150]}, "computer_batch"),
        ({"action": "mouse_move"}, '"coordinate": [x, y]'),
        ({"action": "scroll", "coordinate": [200, 150]}, '"scroll_direction"'),
        ({"action": "hold_key", "text": "shift"}, '"duration"'),
        ({"action": "hold_key", "text": "a b", "duration": 1}, "one key or chord"),
        ({"action": "left_click", "coordinate": [200, 150], "text": "a"}, "not a modifier"),
        ({"action": "key", "text": "cmd+s"}, "Mac key"),
        ({"action": "key", "text": "hyper"}, 'Unknown key "hyper"'),
    ],
)
async def test_unclear_actions_are_refused_before_input(
    computer: Harness, arguments: dict, message: str
) -> None:
    await computer.grant("Notepad")
    await computer.computer(action="screenshot")
    computer.target.inputs.clear()
    result = await computer.computer(**arguments)
    assert result["error"]["code"] == "invalid_arguments"
    assert message in result["error"]["message"]
    assert computer.target.inputs == []


@pytest.mark.parametrize(
    ("text", "chords"),
    [
        ("Return", [["enter"]]),
        ("KP_Enter", [["enter"]]),
        ("super+e", [["win", "e"]]),
        ("ctrl-shift-t", [["ctrl", "shift", "t"]]),
        ("Control_L+ArrowUp", [["ctrl", "up"]]),
        ("ctrl+plus", [["ctrl", "+"]]),
        ("ctrl++", [["ctrl", "+"]]),
        ("page down", [["pagedown"]]),
        ("ctrl+a Delete", [["ctrl", "a"], ["delete"]]),
        ("Shift + Tab", [["shift", "tab"]]),
        ("F12", [["f12"]]),
        ("ä", [["ä"]]),
    ],
)
async def test_key_names_from_other_harnesses_press_canonical_keys(
    computer: Harness, text: str, chords: list[list[str]]
) -> None:
    await computer.grant("Notepad")
    computer.target.inputs.clear()
    result = await computer.computer(action="key", text=text)
    assert result["ok"], result
    assert computer.target.inputs == [("keys", chord, 1) for chord in chords]


async def test_unexpected_target_failure_says_whether_input_was_sent(
    computer: Harness,
) -> None:
    await computer.grant("Notepad")
    computer.target.fail["type"] = computer_use.TargetError("The keyboard layout is missing.")
    result = await computer.computer(action="type", text="x")
    assert result["error"]["code"] == "computer_use_failed"
    assert result["error"]["message"] == (
        "The keyboard layout is missing. Input may have been sent; take a screenshot before "
        "repeating it."
    )
    computer.target.fail["move"] = computer_use.TargetError("The pointer is blocked.")
    result = await computer.computer(action="mouse_move", coordinate=[200, 150])
    assert "Input may have been sent" in result["error"]["message"]
