"""computer_batch order and failure reporting, and the stop control around every call."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest

from resources.extensions.computer_use import _desktop
from resources.extensions.computer_use import extension as computer_use
from tests.resources.extensions.computer_use.computer_use_test_support import (
    EXPLORER,
    PAINT,
    VBOT,
    Harness,
    images,
    model_text,
)

pytestmark = pytest.mark.asyncio

# A per-action summary is dropped without a note.
CLICK = {"action": "left_click", "coordinate": [200, 150], "action_summary": "Focuses the field"}


async def test_batch_runs_in_order_and_returns_images_in_order(computer: Harness) -> None:
    await computer.computer(action="screenshot", view="display")
    computer.target.inputs.clear()
    context = computer.context_for("computer_batch")
    actions = [
        CLICK,
        {"action": "type", "text": "invoice"},
        {"action": "screenshot"},
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
    assert [image.size for image in images(context)] == [(1280, 720), (800, 400), (1280, 720)]
    step, settle = computer_use.STEP_SETTLE_SECONDS, computer_use.SETTLE_SECONDS
    assert computer.sleeps == [step, step, settle]
    text = model_text(result)
    assert text.startswith(
        "Ran 5 actions:\n1. left_click at [200, 150]\n2. type (7 characters)\n3. Screenshot"
    )
    assert "\n4. key enter\n5. Zoom of [100, 100, 300, 200]" in text
    assert computer.registry.get("computer_batch").display.summary({"actions": actions}) == (
        "5 actions: left_click; type; screenshot; key; zoom"
    )


@pytest.mark.parametrize(
    ("actions", "count"),
    [([CLICK, {"action": "screenshot"}], 1), ([{"action": "zoom", "region": [0, 0, 10, 10]}], 1)],
)
async def test_batch_adds_no_final_screenshot_after_one_or_without_input(
    computer: Harness, actions: list, count: int
) -> None:
    await computer.computer(action="screenshot", view="display")
    context = computer.context_for("computer_batch")
    result = await computer.call("computer_batch", {"actions": actions}, context)
    assert result["ok"] and len(images(context)) == count


async def test_batch_coordinates_keep_the_frame_from_before_the_call(computer: Harness) -> None:
    context = computer.context_for("computer")
    await computer.call("computer", {"action": "screenshot", "view": "display"}, context)
    full_id = context.result_media[-1]["filename"].removesuffix(".png")
    computer.target.inputs.clear()

    def paint_to_front(record: tuple) -> None:
        if record[0] == "click" and computer.target.foreground().app != PAINT:
            computer.target.front(PAINT)

    computer.target.on_input = paint_to_front
    actions = [
        {**CLICK, "screenshot_id": full_id},
        {"action": "screenshot"},
        {"action": "left_click", "coordinate": [150, 100]},
    ]
    result = await computer.call("computer_batch", {"actions": actions})
    assert result["ok"], result
    # The inner screenshot followed Paint to the wide display; the click still used Main.
    assert '2. Screenshot of display 2 of 2 "Wide"' in model_text(result)
    assert computer.target.inputs[-1] == ("click", 150, 100, "left", 1, [])


async def test_batch_stops_at_the_first_failure_and_names_what_ran(computer: Harness) -> None:
    await computer.computer(action="screenshot", view="display")
    slack = computer.target.windows_[3]
    computer.target.windows_[3] = replace(slack, elevated=True)
    computer.target.inputs.clear()
    actions = [
        CLICK,
        {"action": "left_click", "coordinate": [800, 600]},
        {"action": "type", "text": "x"},
    ]
    context = computer.context_for("computer_batch")
    result = await computer.call("computer_batch", {"actions": actions}, context)
    assert result["error"]["code"] == "target_elevated"
    message = result["error"]["message"]
    assert message.startswith("Action 2 of 3 (left_click) failed: Slack runs as administrator")
    assert "The remaining 1 did not run.\nActions that ran:\n1. left_click at [200, 150]\n" in (
        message
    )
    # Input was sent, so the failure shows the screen it left.
    shot = context.result_media[-1]["filename"].removesuffix(".png")
    assert message.endswith(
        f'\nThe screen now: Screenshot of display 1 of 2 "Main": 1280x720 pixels, '
        f'screenshot_id="{shot}". Use positions in this image as coordinates.'
    )
    assert len(images(context)) == 1
    assert computer.target.inputs == [("click", 200, 150, "left", 1, [])]


async def test_batch_refuses_keys_when_another_app_came_to_the_front_after_its_click(
    computer: Harness,
) -> None:
    await computer.computer(action="screenshot", view="display")
    computer.target.inputs.clear()

    def user_brings_vbot_to_the_front(record: tuple) -> None:
        if record[0] == "click":
            computer.target.front(VBOT)

    computer.target.on_input = user_brings_vbot_to_the_front
    actions = [
        CLICK,
        {"action": "type", "text": "execfile('x')"},
        {"action": "key", "text": "enter"},
    ]
    context = computer.context_for("computer_batch")
    result = await computer.call("computer_batch", {"actions": actions}, context)
    assert result["error"]["code"] == "focus_changed"
    message = result["error"]["message"]
    assert message.startswith(
        "Action 2 of 3 (type) failed: Your last click went into Notepad, but the foreground "
        "window now belongs to vBot"
    )
    assert "The remaining 1 did not run.\nActions that ran:\n1. left_click at [200, 150]" in message
    assert "\nThe screen now: Screenshot of display" in message and len(images(context)) == 1
    assert computer.target.inputs == [("click", 200, 150, "left", 1, [])]


async def test_batch_keys_follow_an_app_its_own_keys_brought_up(computer: Harness) -> None:
    # The Start menu after win stands for any app the call's own keys bring up.
    await computer.computer(action="screenshot", view="display")
    computer.target.inputs.clear()

    def win_opens_another_app(record: tuple) -> None:
        if record[0] == "keys":
            computer.target.front(EXPLORER)

    computer.target.on_input = win_opens_another_app
    actions = [
        {"action": "key", "text": "win"},
        {"action": "type", "text": "paint"},
        {"action": "key", "text": "enter"},
    ]
    result = await computer.call(
        "computer_batch", {"actions": actions}, computer.context_for("computer_batch")
    )
    assert result["ok"]
    assert [record[0] for record in computer.target.inputs] == ["keys", "type", "keys"]


@pytest.mark.parametrize("failure", ["target", "artifact", "unexpected"])
async def test_batch_reports_completed_input_when_its_final_screenshot_fails(
    computer: Harness, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    await computer.computer(action="screenshot", view="display")

    def fail_capture(*args):
        error = {
            "target": computer_use.TargetError,
            "artifact": OSError,
            "unexpected": RuntimeError,
        }[failure]
        raise error("test-owned-capture-unavailable")

    if failure == "artifact":
        monkeypatch.setattr(_desktop, "publish_image", fail_capture)
    else:
        monkeypatch.setattr(computer.target, "capture", fail_capture)
    context = computer.context_for("computer_batch")
    result = await computer.call(
        "computer_batch", {"actions": [CLICK, {"action": "key", "text": "enter"}]}, context
    )
    assert result["error"]["code"] == "computer_use_failed"
    assert "test-owned-capture-unavailable" in result["error"]["message"]
    # Each successful action keeps its evidence even though no final image exists.
    assert "1. left_click at [200, 150]" in model_text(result)
    assert "2. key enter" in model_text(result)
    assert computer.target.inputs == [("click", 200, 150, "left", 1, []), ("keys", ["enter"], 1)]
    assert context.result_media == []


async def test_batch_rechecks_image_geometry_after_earlier_input(computer: Harness) -> None:
    await computer.computer(action="screenshot")

    def move_window(record: tuple) -> None:
        computer.target.windows_[0] = replace(computer.target.windows_[0], left=101)

    computer.target.on_input = move_window
    result = await computer.call("computer_batch", {"actions": [CLICK, CLICK]})
    assert result["error"]["code"] == "invalid_arguments"
    assert "Action 2 of 2" in result["error"]["message"]
    assert "moved, resized or closed" in result["error"]["message"]
    assert "Actions that ran" in result["error"]["message"]
    assert computer.target.inputs == [("click", 300, 250, "left", 1, [])]


@pytest.mark.parametrize(
    ("bad", "message"),
    [
        ({"action": "type", "text": "x", "coordinate": [1, 1]}, "Action 2: type acts on the"),
        ({"action": "left_click", "coordinate": [5000, 10]}, "Action 2: coordinate [5000, 10]"),
        (
            {"action": "left_click", "coordinate": [200, 150], "screenshot_id": "shot_unknown"},
            "Action 2: Image",
        ),
    ],
)
async def test_batch_with_an_invalid_action_runs_nothing(
    computer: Harness, bad: dict, message: str
) -> None:
    await computer.computer(action="screenshot", view="display")
    computer.target.inputs.clear()
    result = await computer.call("computer_batch", {"actions": [CLICK, bad]})
    assert result["error"]["code"] == "invalid_arguments"
    assert message in result["error"]["message"]
    assert computer.target.inputs == []


async def test_double_escape_interrupts_a_batch_between_input_events(computer: Harness) -> None:
    await computer.computer(action="screenshot", view="display")
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
    assert computer.target.activity == [True, False]  # a stop takes the sign down


async def test_control_reports_and_stops_only_the_current_control(computer: Harness) -> None:
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
        seen.append(control({"action": "stop", "control_id": "ctl_other"}))
        seen.append(control({"action": "stop", "control_id": status["control_id"]}))

    computer.target.on_input = inspect
    computer.published.clear()
    result = await computer.computer(action="type", text="x")
    status, ignored, stopped = seen
    assert computer.hotkey.armed is None
    assert status == {
        "available": True,
        "active": True,
        "stopping": False,
        "hotkey_available": True,
        "control_id": status["control_id"],
    }
    assert ignored == status
    # The frame is gone at once; the interrupted call still drains.
    assert stopped == {
        "available": True,
        "active": False,
        "stopping": True,
        "hotkey_available": True,
    }
    assert result["error"]["code"] == "computer_use_interrupted"
    assert result["error"]["message"].startswith(
        "The user stopped Computer Use during type (1 characters); it may have been partly sent."
    )
    controls = [change for change in computer.published if change[0] == "control"]
    assert controls and all(ids == ["control"] for _, ids, _ in controls)
    assert [revision for *_, revision in controls] == sorted(revision for *_, revision in controls)
    assert (await computer.api.operations.invoke("control", {}))["active"] is False


@pytest.mark.parametrize("cancellation", ["run", "task", "both"])
async def test_run_cancellation_stops_the_call(computer: Harness, cancellation: str) -> None:
    cancel_task = cancellation != "run"
    callbacks: list = []
    context = computer.context_for("computer", cancel_registration_hook=callbacks.append)
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    def cancel(record: tuple) -> None:
        if cancellation != "task":
            for callback in callbacks:
                callback()
        if cancel_task:
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(5), "cancelled desktop worker was not released"

    computer.target.on_input = cancel
    call = asyncio.create_task(
        computer.call("computer", {"action": "key", "text": "enter"}, context)
    )
    if cancel_task:
        try:
            await entered.wait()
            owner = computer.service._active
            call.cancel()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            # Cancellation cannot release whole-call serialization while native
            # input still runs, or lose the owner of its stop event.
            assert not call.done()
            assert computer.service._lock.locked()
            assert computer.service._active == owner
            assert computer.target.stop_event is not None and computer.target.stop_event.is_set()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await call
        assert not computer.service._lock.locked()
        assert computer.service._active is None
        return
    result = await call
    assert result["error"]["code"] == "computer_use_interrupted"
    assert result["error"]["message"].startswith("The Run was cancelled during key enter")


async def test_cancelled_queued_call_does_not_stop_another_runs_input(
    computer: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered = asyncio.Event()
    queued = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    readiness_calls = []
    target_readiness = computer.target.readiness

    def readiness() -> str | None:
        readiness_calls.append(True)
        return target_readiness()

    def block(record: tuple) -> None:
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5), "desktop worker was not released"

    monkeypatch.setattr(computer.target, "readiness", readiness)
    computer.target.on_input = block
    first = asyncio.create_task(computer.computer(action="key", text="enter"))
    second = None
    try:
        await entered.wait()
        executor = computer.service._executor
        assert executor is not None
        submit = executor.submit

        def submitted(*args, **kwargs):
            future = submit(*args, **kwargs)
            queued.set()
            return future

        monkeypatch.setattr(executor, "submit", submitted)
        second = asyncio.create_task(
            computer.call(
                "computer_apps", {"action": "list"}, computer.context_for("computer_apps")
            )
        )
        await queued.wait()
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second
        assert computer.service._lock.locked()
        assert computer.target.stop_event is not None and not computer.target.stop_event.is_set()
    finally:
        release.set()
        await asyncio.gather(
            first, *([second] if second is not None else []), return_exceptions=True
        )
    assert (await first)["ok"]
    assert len(readiness_calls) == 1
    assert computer.target.inputs == [("keys", ["enter"], 1)]


async def test_cancellation_after_mouse_down_completes_releases_the_button(
    computer: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    loop = asyncio.get_running_loop()
    button = computer.target.button

    def cancel_after_input(*args):
        button(*args)
        loop.call_soon_threadsafe(call.cancel)

    monkeypatch.setattr(computer.target, "button", cancel_after_input)
    call = asyncio.create_task(computer.computer(action="left_mouse_down"))
    with pytest.raises(asyncio.CancelledError):
        await call
    assert computer.target.inputs == [("button", "left", True)]
    # _act never received the result to set _held_run, so Run-end bookkeeping
    # cannot be responsible for releasing this completed native input.
    assert computer.service._held_run is None
    assert computer.target.released >= 1


async def test_run_end_releases_a_mouse_button_the_run_left_pressed(computer: Harness) -> None:
    computer.target.pointer = (200, 150)
    assert (await computer.computer(action="left_mouse_down"))["ok"]
    await computer.service.run_end(SimpleNamespace(run_id="other"))
    assert computer.target.released == 0
    await computer.service.run_end(SimpleNamespace(run_id="run"), outcome="completed")
    assert computer.target.released == 1


async def test_desktop_control_lasts_from_the_first_call_until_release_run_end_or_idle(
    computer: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    await computer.call("computer_apps", {"action": "list"})
    assert computer.target.activity == []
    await computer.computer(action="screenshot")
    await computer.computer(action="type", text="x")
    assert computer.target.activity == [True]
    control_id = computer.hotkey.armed
    assert control_id is not None  # double Esc works between calls too
    assert (await computer.api.operations.invoke("control", {}))["control_id"] == control_id

    released = await computer.call("computer_apps", {"action": "release"})
    assert model_text(released).startswith("Released the computer")
    assert computer.target.activity == [True, False]
    assert computer.hotkey.armed is None
    again = await computer.call("computer_apps", {"action": "done"})
    assert model_text(again).startswith("You were not controlling the computer")

    # Taking the computer again in the same Run starts a new control.
    await computer.computer(action="screenshot")
    assert computer.hotkey.armed not in (None, control_id)
    await computer.service.run_end(SimpleNamespace(run_id="other"))
    assert computer.target.activity == [True, False, True]
    await computer.service.run_end(SimpleNamespace(run_id="run"))
    assert computer.target.activity == [True, False, True, False]

    # A Run that stops calling loses control after the idle time.
    monkeypatch.setattr(computer_use, "CONTROL_IDLE_SECONDS", 0)
    await computer.computer(action="screenshot")
    await asyncio.sleep(0.01)
    assert computer.target.activity == [True, False, True, False, True, False]


async def test_a_user_stop_between_calls_refuses_the_run_until_it_ends(
    computer: Harness,
) -> None:
    computer.target.pointer = (200, 150)
    assert (await computer.computer(action="left_mouse_down"))["ok"]
    computer.target.inputs.clear()
    computer.hotkey.press_double_escape()
    assert computer.target.activity == [True, False]
    assert computer.hotkey.armed is None

    refused = await computer.computer(action="left_click", coordinate=[10, 10])
    assert refused["error"]["code"] == "computer_use_interrupted"
    assert refused["error"]["message"].startswith(
        "The user stopped Computer Use by pressing Esc twice. Nothing was done. Computer Use "
        "stays off until the user's next message"
    )
    opened = await computer.call("computer_apps", {"action": "open", "app": "Notepad"})
    assert opened["error"]["code"] == "computer_use_interrupted"
    assert computer.target.inputs == []
    assert computer.target.activity == [True, False]

    # The Run's end releases the held button, and the next Run may take control.
    await computer.service.run_end(SimpleNamespace(run_id="run"))
    assert computer.target.released == 1
    assert (await computer.computer(action="screenshot"))["ok"]
    assert computer.target.activity == [True, False, True]
