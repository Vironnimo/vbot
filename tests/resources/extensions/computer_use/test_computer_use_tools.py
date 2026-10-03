"""The computer Tool through production dispatch: permission, readiness, input and images."""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from core.extensions.extensions import ExtensionAPI, ExtensionDeclarations
from core.extensions.operations import ExtensionHost
from core.tools import ToolContext
from core.tools.availability import ToolAccess
from resources.extensions.computer_use import extension as computer_use
from resources.extensions.computer_use.extension import _new_target as platform_target
from tests.resources.extensions.computer_use.computer_use_test_support import (
    CHROME,
    PAINT,
    TOOLS,
    Harness,
    images,
    model_text,
    registered_service,
)


def screenshot_id(context: ToolContext) -> str:
    return str(context.result_media[-1]["filename"]).removesuffix(".png")


pytestmark = pytest.mark.asyncio


async def test_tools_register_as_one_opt_in_family_with_stop_control(computer: Harness) -> None:
    for name in TOOLS:
        tool = computer.registry.get(name)
        assert tool.requires_opt_in and not tool.parallel_safe
        assert tool.open_input_schema and "additionalProperties" not in tool.parameters
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
    assert computer.hotkey.started and computer.hotkey.armed is not None  # armed while in control
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


async def test_screenshot_coordinates_use_returned_pixels_and_keep_the_display(
    computer: Harness,
) -> None:
    context = computer.context_for("computer")
    result = await computer.call("computer", {"action": "screenshot", "view": "display"}, context)
    assert "1280x720 pixels" in model_text(result)
    assert f'screenshot_id="{screenshot_id(context)}"' in model_text(result)
    assert "Coordinates are this image's pixels, 1:1 with the screen." in model_text(result)
    assert [image.size for image in images(context)] == [(1280, 720)]
    # Images are temporary files that the storage retention sweep removes.
    saved = Path(context.result_media[0]["path"])
    assert saved.parent == computer.context.data_root / "artifacts" / "temp" / "computer_use"
    assert saved.is_file()

    context = computer.context_for("computer")
    result = await computer.call(
        "computer", {"action": "screenshot", "display": "2", "scale": 0.5}, context
    )
    assert model_text(result).startswith('Screenshot of display 2 of 2 "Wide": 784x250 pixels')
    assert "one covers 4.0 screen pixels, so zoom in where an exact pixel" in model_text(result)
    assert [image.size for image in images(context)] == [(784, 250)]
    scaled_id = screenshot_id(context)
    # One pixel of the 784x250 image covers 4x4 physical pixels; input hits the centre.
    computer.target.pointer = (-2736, 400)
    result = await computer.computer(action="cursor_position")
    assert model_text(result).startswith("The pointer is at [100, 100]")
    result = await computer.computer(action="left_click", coordinate=[100, 100])
    assert result["ok"], result
    assert computer.target.inputs[-1] == ("click", -2734, 402, "left", 1, [])
    # Input returned a full-sized image, but the explicit scaled-image reference still works.
    for alias in ["screenshotId", "image_id"]:
        result = await computer.computer(
            action="mouse_move", coordinate=[100, 100], **{alias: scaled_id}
        )
        assert result["ok"]
        assert computer.target.inputs[-1] == ("move", -2734, 402)
    before = list(computer.target.inputs)
    result = await computer.computer(
        action="left_click", coordinate=[100, 100], screenshot_id=scaled_id, image_id="shot_unknown"
    )
    assert result["error"]["code"] == "invalid_arguments" and computer.target.inputs == before
    result = await computer.computer(
        action="left_click", screenshot_id=scaled_id, coordinate=[784, 10]
    )
    assert result["error"]["code"] == "invalid_arguments"
    result = await computer.computer(action="screenshot")
    assert '"Wide"' in model_text(result)
    result = await computer.computer(action="screenshot", display="auto")
    assert '"Main"' in model_text(result)

    result = await computer.computer(action="screenshot", display="Projector")
    assert result["error"]["code"] == "invalid_arguments"
    assert '1. "Main" 1280x720 (primary, current)\n2. "Wide" 3136x1000' in model_text(result)


async def test_zoom_coordinates_are_local_to_the_returned_crop(
    computer: Harness,
) -> None:
    await computer.computer(action="screenshot", display="Wide")
    context = computer.context_for("computer")
    result = await computer.call(
        "computer", {"action": "zoom", "region": [100, 100, 300, 200]}, context
    )
    assert "400x200 pixels" in model_text(result)
    assert [image.size for image in images(context)] == [(400, 200)]
    crop_id = screenshot_id(context)
    result = await computer.computer(
        action="left_click_drag", start_coordinate=[50, 50], coordinate=[150, 100]
    )
    assert result["ok"], result
    assert computer.target.inputs[-1] == ("drag", (-2886, 250), (-2786, 300), [])
    context = computer.context_for("computer")
    result = await computer.call(
        "computer",
        {"action": "zoom", "screenshot_id": crop_id, "region": [50, 50, 150, 100], "scale": 0.5},
        context,
    )
    assert result["ok"] and images(context)[0].size == (50, 25)
    # The half-size zoom shows physical [-2886, 250] onward at 2x2 pixels per image pixel.
    result = await computer.computer(action="left_click", coordinate=[25, 10])
    assert result["ok"]
    assert computer.target.inputs[-1] == ("click", -2835, 271, "left", 1, [])
    result = await computer.computer(
        action="zoom", screenshot_id=crop_id, region=[350, 0, 450, 100]
    )
    assert result["error"]["code"] == "invalid_arguments"
    assert "400x200" in result["error"]["message"]


async def test_window_view_includes_owned_popups_and_spans_displays(
    computer: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    # This window spans the display at a negative origin and the primary display.
    computer.target.windows_[0] = replace(
        computer.target.windows_[0], left=-200, top=100, right=400, bottom=500
    )
    context = computer.context_for("computer")
    result = await computer.call("computer", {"action": "screenshot"}, context)
    assert result["ok"] and images(context)[0].size == (600, 400)
    assert 'foreground window "Notepad"' in model_text(result)
    image = images(context)[0]
    assert image.getpixel((0, 0)) == image.getpixel((599, 399)) == (60, 185, 70)
    result = await computer.computer(action="left_click", coordinate=[300, 50])
    assert result["ok"]
    assert computer.target.inputs[-1] == ("click", 100, 150, "left", 1, [])
    # A deliberate display capture remains available; window view can be restored.
    await computer.computer(action="screenshot", display="1")
    context = computer.context_for("computer")
    await computer.call("computer", {"action": "screenshot", "view": "window"}, context)
    assert images(context)[0].size == (600, 400)
    root = computer.target.foreground()
    popup = replace(root, handle=8, left=400, right=600, bottom=300, owner=root.handle)
    computer.target.windows_.insert(0, popup)
    monkeypatch.setattr(computer.target, "foreground", lambda: root)
    context = computer.context_for("computer")
    result = await computer.call("computer", {"action": "screenshot"}, context)
    assert result["ok"] and images(context)[0].size == (800, 400)
    result = await computer.computer(action="left_click", coordinate=[650, 50])
    assert result["ok"], result
    assert computer.target.inputs[-1] == ("click", 450, 150, "left", 1, [])


@pytest.mark.parametrize(
    "reason", ["missing", "other_session", "expired", "layout", "window", "covered"]
)
async def test_image_references_refuse_missing_or_changed_targets_before_input(
    computer: Harness, reason: str
) -> None:
    context = computer.context_for("computer")
    await computer.call("computer", {"action": "screenshot"}, context)
    reference = screenshot_id(context)
    request_context = computer.context_for("computer")
    if reason == "missing":
        reference = "shot_unknown"
    elif reason == "other_session":
        request_context = computer.context_for("computer", session_id="other")
    elif reason == "expired":
        now = computer.service.sessions.clock()
        computer.service.sessions.clock = lambda: now + 1801
    elif reason == "layout":
        computer.target.displays_[0] = replace(computer.target.displays_[0], left=1)
    elif reason == "window":
        computer.target.windows_[0] = replace(computer.target.windows_[0], left=101)
    else:
        chrome = computer.target.windows_[1]
        computer.target.windows_[1] = replace(chrome, left=100, top=100, right=600, bottom=500)
        computer.target.front(chrome.app)
    result = await computer.call(
        "computer",
        {"action": "left_click", "screenshot_id": reference, "coordinate": [100, 50]},
        request_context,
    )
    assert result["error"]["code"] == "invalid_arguments"
    assert "screenshot" in result["error"]["message"]
    assert computer.target.inputs == []


@pytest.mark.parametrize("tool", ["computer", "computer_batch"])
async def test_coordinates_require_an_observation(computer: Harness, tool: str) -> None:
    arguments = {"action": "left_click", "coordinate": [200, 150]}
    if tool == "computer_batch":
        arguments = {"actions": [{"action": "screenshot"}, arguments]}
    context = computer.context_for(tool)
    result = await computer.call(tool, arguments, context)
    assert result["error"]["code"] == "invalid_arguments"
    assert 'Take a new screenshot with {"action":"screenshot"}' in result["error"]["message"]
    assert computer.target.inputs == []
    assert context.result_media == []


async def paint_on_the_wide_display(computer: Harness) -> None:
    computer.target.front(PAINT)
    await computer.computer(action="screenshot", view="display")
    computer.target.pointer = (-2000, 500)
    computer.target.inputs.clear()
    computer.sleeps.clear()


# Wide display: frame 1568x500 at x=-3136. Image pixel [x, y] covers physical
# [-3136 + 2x, 2y] to [-3135 + 2x, 2y + 1]; input lands on [-3135 + 2x, 2y + 1].
@pytest.mark.parametrize(
    ("arguments", "inputs"),
    [
        (
            {"action": "left_click", "coordinate": [100, 200]},
            [("click", -2935, 401, "left", 1, [])],
        ),
        (
            {"action": "right_click", "coordinate": [100, 200], "text": "shift"},
            [("click", -2935, 401, "right", 1, ["shift"])],
        ),
        ({"action": "middle_click"}, [("click", -2000, 500, "middle", 1, [])]),
        (
            {"action": "double_click", "coordinate": [100, 200]},
            [("click", -2935, 401, "left", 2, [])],
        ),
        (
            {"action": "triple_click", "coordinate": [100, 200]},
            [("click", -2935, 401, "left", 3, [])],
        ),
        ({"action": "mouse_move", "coordinate": [500, 250]}, [("move", -2135, 501)]),
        (
            {
                "action": "left_click_drag",
                "start_coordinate": [100, 100],
                "coordinate": [200, 150],
                "text": "alt",
            },
            [("drag", (-2935, 201), (-2735, 301), ["alt"])],
        ),
        (
            {"action": "left_click_drag", "coordinate": [200, 150]},
            [("drag", (-2000, 500), (-2735, 301), [])],
        ),
        (
            {"action": "left_mouse_up", "coordinate": [300, 300]},
            [("move", -2535, 601), ("button", "left", False)],
        ),
        (
            {
                "action": "scroll",
                "coordinate": [400, 200],
                "scroll_direction": "down",
                "scroll_amount": 5,
                "text": "ctrl",
            },
            [("scroll", -2335, 401, "down", 5, ["ctrl"])],
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
    await paint_on_the_wide_display(computer)
    context = computer.context_for("computer")
    result = await computer.call("computer", arguments, context)
    assert result["ok"], result
    assert computer.target.inputs == inputs
    assert computer.sleeps == [computer_use.SETTLE_SECONDS]
    assert len(images(context)) == 1
    assert model_text(result).startswith("Done: ")


async def test_mouse_down_and_cursor_position_return_no_screenshot(computer: Harness) -> None:
    await paint_on_the_wide_display(computer)
    context = computer.context_for("computer")
    result = await computer.call("computer", {"action": "left_mouse_down"}, context)
    assert model_text(result) == "Done: left_mouse_down at the pointer."
    assert computer.target.inputs == [("button", "left", True)] and context.result_media == []
    result = await computer.computer(action="cursor_position")
    assert model_text(result).startswith('The pointer is at [568, 250] in image "shot_')
    assert "1568x500 pixels" in model_text(result)
    computer.target.pointer = (300, 300)
    result = await computer.computer(action="cursor_position")
    assert 'outside image "shot_' in model_text(result)
    assert 'on display "Main"' in model_text(result)


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
    result = await computer.computer(action="wait", duration=1, screenshot_id="shot_unknown")
    assert result["ok"]
    assert model_text(result).endswith("Ignored screenshot_id: wait does not use it.")


async def test_keys_are_refused_when_another_app_came_to_the_front(computer: Harness) -> None:
    context = computer.context_for("computer")
    await computer.call("computer", {"action": "screenshot"}, context)
    notepad = screenshot_id(context)
    computer.target.front(CHROME)  # for example, the user clicked into another app
    computer.target.inputs.clear()
    for tool, arguments in [
        ("computer", {"action": "type", "text": "x"}),
        ("computer_batch", {"actions": [{"action": "key", "text": "enter"}]}),
    ]:
        result = await computer.call(tool, arguments)
        message = result["error"]["message"]
        assert result["error"]["code"] == "focus_changed"
        assert f'Notepad was in front in image "{notepad}", but the foreground window' in message
        assert "now belongs to Google Chrome, so" in message
    assert computer.target.inputs == []
    # A click before the keys in the same call decides where they go.
    result = await computer.call(
        "computer_batch",
        {
            "actions": [
                {"action": "left_click", "coordinate": [50, 50]},
                {"action": "type", "text": "x"},
            ]
        },
    )
    assert result["ok"], result
    assert computer.target.inputs == [("click", 150, 150, "left", 1, []), ("type", "x")]
    # After a screenshot of Chrome, keys go through, unless they name the Notepad image.
    await computer.computer(action="screenshot")
    result = await computer.computer(action="key", text="enter")
    assert result["ok"] and computer.target.inputs[-1] == ("keys", ["enter"], 1)
    result = await computer.computer(action="key", text="enter", screenshot_id=notepad)
    assert result["error"]["code"] == "focus_changed"


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"action": "left_click", "coordinate": [1300, 100]}, "outside image"),
        ({"action": "screenshot", "view": "window", "display": "1"}, "Omit display"),
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
    computer.target.inputs.clear()
    result = await computer.computer(action="key", text=text)
    assert result["ok"], result
    assert computer.target.inputs == [("keys", chord, 1) for chord in chords]


async def test_unexpected_target_failure_says_whether_input_was_sent(
    computer: Harness,
) -> None:
    computer.target.fail["type"] = computer_use.TargetError("The keyboard layout is missing.")
    result = await computer.computer(action="type", text="x")
    assert result["error"]["code"] == "computer_use_failed"
    assert result["error"]["message"] == (
        "The keyboard layout is missing. Input may have been sent; take a screenshot before "
        "repeating it."
    )
    computer.target.fail["move"] = computer_use.TargetError("The pointer is blocked.")
    await computer.computer(action="screenshot", view="display")
    result = await computer.computer(action="mouse_move", coordinate=[200, 150])
    assert "Input may have been sent" in result["error"]["message"]
