"""computer_batch order and failure reporting, and the stop control around every call."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from resources.extensions.computer_use import extension as computer_use
from tests.resources.extensions.computer_use.computer_use_test_support import (
    PAINT,
    Harness,
    images,
    model_text,
)

pytestmark = pytest.mark.asyncio

CLICK = {"action": "left_click", "coordinate": [200, 150], "action_summary": "Focuses the field"}


async def test_batch_runs_in_order_and_returns_images_in_order(computer: Harness) -> None:
    computer.target.inputs.clear()
    context = computer.context_for("computer_batch")
    actions = [
        CLICK,
        {"action": "type", "text": "invoice"},
        {"action": "screenshot", "scale": 0.5},
        {"action": "key", "text": "enter"},
        {"action": "zoom", "region": [100, 100, 300, 200]},
    ]
    result = await computer.call("computer_batch", {"actions": actions}, context)
    assert computer.target.inputs == [
        ("click", 200, 150, "left", 1, []),
        ("type", "invoice"),
        ("keys", ["enter"], 1),
    ]
    # Screenshot, zoom, then the final screenshot because input was sent after the last one.
    assert [image.size for image in images(context)] == [(640, 360), (200, 100), (1280, 720)]
    step, settle = computer_use.STEP_SETTLE_SECONDS, computer_use.SETTLE_SECONDS
    assert computer.sleeps == [step, step, settle]
    text = model_text(result)
    assert text.startswith(
        "Ran 5 actions:\n1. left_click at [200, 150]\n2. type (7 characters)\n3. Screenshot"
    )
    assert "\n4. key enter\n5. Zoom of [100, 100, 300, 200]" in text
    assert computer.registry.get("computer_batch").display.summary({"actions": actions}) == (
        "5 actions: Focuses the field; type; screenshot; key; zoom"
    )


@pytest.mark.parametrize(
    ("actions", "count"),
    [([CLICK, {"action": "screenshot"}], 1), ([{"action": "zoom", "region": [0, 0, 10, 10]}], 1)],
)
async def test_batch_adds_no_final_screenshot_after_one_or_without_input(
    computer: Harness, actions: list, count: int
) -> None:
    context = computer.context_for("computer_batch")
    result = await computer.call("computer_batch", {"actions": actions}, context)
    assert result["ok"] and len(images(context)) == count


async def test_batch_coordinates_keep_the_frame_from_before_the_call(computer: Harness) -> None:
    await computer.computer(action="screenshot")
    computer.target.inputs.clear()

    def paint_to_front(record: tuple) -> None:
        if record[0] == "click" and computer.target.foreground().app != PAINT:
            computer.target.front(PAINT)

    computer.target.on_input = paint_to_front
    actions = [CLICK, {"action": "screenshot"}, {"action": "left_click", "coordinate": [300, 200]}]
    result = await computer.call("computer_batch", {"actions": actions})
    assert result["ok"], result
    # The inner screenshot followed Paint to the wide display; the click still used Main.
    assert '2. Screenshot of display 2 of 2 "Wide"' in model_text(result)
    assert computer.target.inputs[-1] == ("click", 300, 200, "left", 1, [])


async def test_batch_stops_at_the_first_failure_and_names_what_ran(computer: Harness) -> None:
    slack = computer.target.windows_[3]
    computer.target.windows_[3] = replace(slack, elevated=True)
    computer.target.inputs.clear()
    actions = [
        CLICK,
        {"action": "left_click", "coordinate": [800, 600]},
        {"action": "type", "text": "x"},
    ]
    result = await computer.call("computer_batch", {"actions": actions})
    assert result["error"]["code"] == "target_elevated"
    message = result["error"]["message"]
    assert message.startswith("Action 2 of 3 (left_click) failed: Slack runs as administrator")
    assert "The remaining 1 did not run.\nActions that ran" in message
    assert message.endswith("\n1. left_click at [200, 150]")
    assert computer.target.inputs == [("click", 200, 150, "left", 1, [])]


@pytest.mark.parametrize(
    ("bad", "message"),
    [
        ({"action": "type", "text": "x", "coordinate": [1, 1]}, "Action 2: type acts on the"),
        ({"action": "left_click", "coordinate": [5000, 10]}, "Action 2: coordinate [5000, 10]"),
    ],
)
async def test_batch_with_an_invalid_action_runs_nothing(
    computer: Harness, bad: dict, message: str
) -> None:
    computer.target.inputs.clear()
    result = await computer.call("computer_batch", {"actions": [CLICK, bad]})
    assert result["error"]["code"] == "invalid_arguments"
    assert message in result["error"]["message"]
    assert computer.target.inputs == []


async def test_double_escape_interrupts_a_batch_between_input_events(computer: Harness) -> None:
    computer.target.inputs.clear()

    def escape_on_typing(record: tuple) -> None:
        if record[0] == "type":
            computer.hotkey.press_double_escape()

    computer.target.on_input = escape_on_typing
    actions = [CLICK, {"action": "type", "text": "x"}, {"action": "key", "text": "enter"}]
    result = await computer.call("computer_batch", {"actions": actions})
    assert result["error"]["code"] == "computer_use_interrupted"
    message = result["error"]["message"]
    assert message.startswith(
        "The user stopped Computer Use by pressing Esc twice during action 2 of 3 (type (1 "
        "characters)). The remaining 1 did not run."
    )
    assert message.endswith("\n1. left_click at [200, 150]")
    assert computer.target.inputs == [("click", 200, 150, "left", 1, [])]
    assert computer.hotkey.armed is None


async def test_control_reports_and_stops_only_the_active_call(computer: Harness) -> None:
    loop = asyncio.get_running_loop()
    seen: list[dict] = []

    def control(arguments: dict) -> dict:
        future = asyncio.run_coroutine_threadsafe(
            computer.api.operations.invoke("control", arguments), loop
        )
        return future.result(5)

    def inspect(record: tuple) -> None:
        if record[0] != "type":
            return
        status = control({"action": "status"})
        seen.append(status)
        seen.append(control({"action": "stop", "call_id": "ctl_other"}))
        seen.append(control({"action": "stop", "call_id": status["call_id"]}))

    computer.target.on_input = inspect
    computer.published.clear()
    result = await computer.computer(action="type", text="x")
    status, ignored, stopped = seen
    call_id = status["call_id"]
    assert computer.hotkey.armed is None
    assert status == {
        "available": True,
        "active": True,
        "stopping": False,
        "hotkey_available": True,
        "call_id": call_id,
    }
    assert ignored == status
    assert stopped == {**status, "stopping": True}
    assert result["error"]["code"] == "computer_use_interrupted"
    assert result["error"]["message"].startswith(
        "The user stopped Computer Use during type (1 characters); it may have been partly sent."
    )
    controls = [change for change in computer.published if change[0] == "control"]
    assert [ids for _, ids, _ in controls] == [[call_id]] * 3
    assert [revision for *_, revision in controls] == sorted(revision for *_, revision in controls)
    assert (await computer.api.operations.invoke("control", {}))["active"] is False


async def test_run_cancellation_stops_the_call(computer: Harness) -> None:
    callbacks: list = []
    context = computer.context_for("computer", cancel_registration_hook=callbacks.append)

    def cancel(record: tuple) -> None:
        for callback in callbacks:
            callback()

    computer.target.on_input = cancel
    result = await computer.call("computer", {"action": "key", "text": "enter"}, context)
    assert result["error"]["code"] == "computer_use_interrupted"
    assert result["error"]["message"].startswith("The Run was cancelled during key enter")


async def test_run_end_releases_a_mouse_button_the_run_left_pressed(computer: Harness) -> None:
    computer.target.pointer = (200, 150)
    assert (await computer.computer(action="left_mouse_down"))["ok"]
    await computer.service.run_end(SimpleNamespace(run_id="other"))
    assert computer.target.released == 0
    await computer.service.run_end(SimpleNamespace(run_id="run"), outcome="completed")
    assert computer.target.released == 1
