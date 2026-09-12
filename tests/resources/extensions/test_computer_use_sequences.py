"""Computer use: sequences behavior."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from core.tools.availability import ToolAccess
from resources.extensions.computer_use import extension as computer_use
from tests.resources.extensions.computer_use_helpers import (
    call,
    capture,
)
from tests.resources.extensions.computer_use_helpers import (
    computer as computer,
)


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


def test_skip_capture_preserves_outcome_and_retires_view(computer, monkeypatch):
    capture(computer)
    with monkeypatch.context() as scoped:

        def unexpected_wait(*args):
            pytest.fail("Skipping capture must also skip the observation delay")

        scoped.setattr(computer[0], "_wait", unexpected_wait)
        result = call(computer, "type", text="draft", apply=True, capture_after=False)
    assert result["ok"] and result["data"]["applied"]
    assert "observation" not in result["data"]
    assert result["data"]["recovery"]["action"] == "capture"
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


@pytest.mark.parametrize("first", ["click", "key"])
def test_root_view_does_not_turn_keyboard_steps_into_coordinate_input(computer, first):
    service, context, client, _ = computer
    data = capture(computer, foreground=True)["data"]
    steps = [
        {"action": "click", "coordinate": [10, 20]},
        {"action": "key", "shortcut": "x"},
        {"action": "type", "text": "0.45", "text_mode": "keyboard"},
        {"action": "key", "shortcut": "enter"},
    ]
    if first == "key":
        steps[0], steps[1] = steps[1], steps[0]
    result = service.handle(
        context, {"action": "sequence", "view_id": data["view_id"], "steps": steps}
    )
    assert result["ok"] and result["data"]["completed_steps"] == 4
    assert client.inputs == 4 and client.snapshots == 2
    typed = next(args for name, args in client.calls if name == "type_text")
    assert typed["text_mode"] == "keyboard" and "x" not in typed and "y" not in typed


@pytest.mark.parametrize(
    "action, fields", [("key", {"shortcut": "enter"}), ("type", {"text": "draft"})]
)
def test_focused_input_uses_explicit_view_target_and_delivery(computer, action, fields):
    service, context, client, _ = computer
    data = capture(computer, foreground=True)["data"]
    result = service.handle(context, {"action": action, "view_id": data["view_id"], **fields})
    assert result["ok"]
    name = "press_key" if action == "key" else "type_text"
    sent = next(args for called, args in client.calls if called == name)
    assert sent["pid"] == 1 and sent["window_id"] == 2
    assert sent["delivery_mode"] == "foreground" and "x" not in sent
    result = service.handle(context, {"action": action, "view_id": data["view_id"], **fields})
    assert result["error"]["code"] == "stale_view" and client.inputs == 1


@pytest.mark.parametrize("completed", [0, 1])
def test_unexpected_sequence_failure_retains_progress_and_recovery_image(computer, completed):
    service, context, client, _ = computer
    data = capture(computer)["data"]

    def fail(name):
        if name == "press_key":
            raise RuntimeError("test-owned dispatch failure")

    client.hook = fail
    steps = [{"action": "click", "coordinate": [10, 20]}] if completed else []
    steps += [{"action": "key", "shortcut": "enter"}, {"action": "type", "text": "never"}]
    result = service.handle(
        context, {"action": "sequence", "view_id": data["view_id"], "steps": steps}
    )
    outcome = result["data"] if completed else result["artifacts"][0]
    assert result["ok"] == bool(completed)
    assert outcome["completed_steps"] == completed
    assert outcome["stopped_step"] == completed + 1 and outcome["partial"]
    assert outcome["observation"]["view_id"] != data["view_id"]
    assert client.inputs == completed and client.snapshots == 2
    assert not any(name == "type_text" for name, _ in client.calls)


def test_unexpected_single_input_failure_keeps_recovery_image(computer):
    capture(computer)

    def fail(name):
        if name == "type_text":
            raise RuntimeError("test-owned failure")

    computer[2].hook = fail
    result = call(computer, "type", text="draft")
    assert result["error"]["code"] == "computer_use_failed"
    assert result["artifacts"][0]["observation"]["view_id"]
    assert result["artifacts"][0]["partial"]
