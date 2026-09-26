"""Computer use: calls that leave the screenshot implicit, and refusals naming the next call."""

from __future__ import annotations

import json

import pytest

from resources.extensions.computer_use.driver import ComputerUseError
from tests.resources.extensions.computer_use_helpers import (
    capture,
    dispatch,
    model_text,
)
from tests.resources.extensions.computer_use_helpers import (
    computer as computer,
)


def _sent(client, name):
    return [args for called, args in client.calls if called == name]


def test_the_only_current_screenshot_names_target_and_view(computer):
    _, _, client, _ = computer
    view = capture(computer)["data"]["view_id"]
    clicked = dispatch(computer, {"action": "click", "coordinate": [10, 20]})
    assert clicked["ok"] and _sent(client, "click")[0]["pid"] == 1
    # The input returned a new screenshot, which the next call continues with.
    assert clicked["data"]["observation"]["view_id"] != view
    assert dispatch(computer, {"action": "type", "text": "draft"})["ok"]
    assert _sent(client, "type_text")[0]["window_id"] == 2 and client.inputs == 2


def test_coordinates_without_any_screenshot_name_the_capture(computer):
    _, _, client, _ = computer
    result = dispatch(computer, {"action": "click", "coordinate": [10, 20]})
    message = result["error"]["message"]
    assert result["error"]["code"] == "capture_required" and not result["artifacts"]
    assert '{"action":"capture"}' in message and '"pid":<pid>' in message
    assert "No input was sent" in message and not client.calls
    assert model_text(result).startswith("Error (capture_required): Coordinates need")


def test_input_in_a_fresh_run_needs_a_capture_even_without_coordinates(computer):
    service, context, client, _ = computer
    result = dispatch(computer, {"action": "key", "text": "enter"})
    assert result["error"]["code"] == "capture_required" and client.inputs == 0
    assert 'Capture it with {"action":"capture"}' in result["error"]["message"]
    service.handle(context, {"action": "capture"})
    assert dispatch(computer, {"action": "key", "text": "enter"})["ok"]
    sent = _sent(client, "press_key")[0]
    assert "pid" not in sent and sent["delivery_mode"] == "foreground"


def test_replaced_window_screenshot_keeps_targetless_input_off_the_desktop(computer):
    _, _, client, _ = computer
    capture(computer)
    typed = dispatch(
        computer,
        {"action": "type", "pid": 1, "window_id": 2, "text": "draft", "capture_after": False},
    )
    assert typed["ok"] and client.inputs == 1
    refused = dispatch(computer, {"action": "key", "text": "enter"})
    message = refused["error"]["message"]
    assert refused["error"]["code"] == "capture_required"
    assert 'Add "pid":1,"window_id":2 to continue' in message
    assert "No input was sent" in message and client.inputs == 1
    clicked = dispatch(computer, {"action": "click", "coordinate": [1, 1]})
    assert (
        '{"action":"capture","pid":1,"window_id":2,"foreground":false}'
        in (clicked["error"]["message"])
    )
    # A read continues with that window and makes it current again.
    waited = dispatch(computer, {"action": "wait", "duration_ms": 0})
    assert waited["ok"] and waited["data"]["target"] == {"pid": 1, "window_id": 2}
    assert dispatch(computer, {"action": "key", "text": "enter"})["ok"]
    assert _sent(client, "press_key")[0]["window_id"] == 2 and client.inputs == 2


def test_several_current_screenshots_need_a_choice_for_input(computer):
    service, context, client, _ = computer
    window = capture(computer)["data"]["view_id"]
    desktop = service.handle(context, {"action": "capture"})["data"]["view_id"]
    result = dispatch(computer, {"action": "click", "coordinate": [10, 20]})
    message = result["error"]["message"]
    assert result["error"]["code"] == "invalid_arguments" and client.inputs == 0
    assert f"view {window} of window pid 1 window_id 2" in message
    assert f"view {desktop} of the desktop" in message
    # A read follows the newest capture.
    waited = dispatch(computer, {"action": "wait", "duration_ms": 0})
    assert waited["ok"] and "target" not in waited["data"]


def test_a_newer_crop_makes_bare_coordinates_ambiguous(computer):
    _, _, client, _ = computer
    view = capture(computer)["data"]["view_id"]
    crop = dispatch(computer, {"action": "zoom", "region": [0, 0, 100, 50]})["data"]
    assert crop["parent_view_id"] == view
    result = dispatch(computer, {"action": "click", "coordinate": [10, 20]})
    message = result["error"]["message"]
    assert f'"view_id":"{crop["view_id"]}"' in message and f'"view_id":"{view}"' in message
    assert client.inputs == 0
    assert dispatch(
        computer, {"action": "click", "view_id": crop["view_id"], "coordinate": [10, 20]}
    )["ok"]
    assert client.inputs == 1


def test_foreground_mismatch_names_both_corrected_calls(computer):
    _, _, client, _ = computer
    view = capture(computer)["data"]["view_id"]
    result = dispatch(computer, {"action": "click", "coordinate": [10, 20], "foreground": True})
    message = result["error"]["message"]
    assert result["error"]["code"] == "capture_required" and client.inputs == 0
    assert f"View {view} was captured for background input" in message
    assert "Omit foreground to send background input with this view" in message
    assert '{"action":"capture","pid":1,"window_id":2,"foreground":true}' in message
    # Keyboard input has no coordinates to misplace, so it follows the requested delivery.
    assert dispatch(computer, {"action": "key", "text": "enter", "foreground": True})["ok"]


@pytest.mark.parametrize(
    "code,failing,arguments,next_call",
    [
        (
            "target_not_foreground",
            "capture_pixels",
            {"action": "capture", "pid": 1, "window_id": 2, "foreground": True, "mode": "vision"},
            '{"action":"capture","pid":1,"window_id":2,"foreground":false}',
        ),
        (
            "focus_refused",
            "type_text",
            {"action": "type", "text": "draft", "foreground": True},
            '{"action":"capture"}, click the window or its taskbar entry',
        ),
        (
            "window_not_visible",
            "type_text",
            {"action": "type", "text": "draft"},
            "restore the window from its taskbar entry",
        ),
        ("stale_window", "type_text", {"action": "type", "text": "draft"}, '{"action":"windows"}'),
        (
            "computer_session_expired",
            "type_text",
            {"action": "type", "text": "draft"},
            '{"action":"capture","pid":1,"window_id":2,"foreground":false}',
        ),
    ],
)
def test_runtime_refusals_name_the_next_call(computer, code, failing, arguments, next_call):
    _, _, client, _ = computer
    capture(computer)

    def refuse(name):
        if name == failing:
            raise ComputerUseError("test-owned refusal", code)

    client.hook = refuse
    result = dispatch(computer, arguments)
    assert result["error"]["code"] == code and not result["artifacts"]
    assert next_call in result["error"]["message"]
    assert "No input was sent" in result["error"]["message"]
    assert "test-owned refusal" not in result["error"]["message"] and client.inputs == 0


def test_unexpected_input_failure_says_input_may_have_been_sent(computer):
    _, _, client, _ = computer
    capture(computer)

    def fail(name):
        if name == "type_text":
            raise RuntimeError("test-owned private detail")

    client.hook = fail
    result = dispatch(computer, {"action": "type", "text": "draft"})
    message = result["error"]["message"]
    assert result["error"]["code"] == "computer_use_failed"
    assert "RuntimeError" in message and "may have partial effects" in message
    assert "vBot problem" in message and "test-owned private detail" not in json.dumps(result)


def test_capture_result_reads_as_short_text(computer):
    result = dispatch(computer, {"action": "screenshot", "pid": 1, "window_id": 2})
    text = model_text(result)
    assert text.splitlines()[0] == "action: capture"
    assert "view_id: " in text and "image_width: 320" in text and "mode:" not in text
    assert 'target: {"pid":1,"window_id":2}' in text and "foreground: false" in text
