"""Computer use: coordinates behavior."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.resources.extensions.computer_use_helpers import (
    call,
    capture,
)
from tests.resources.extensions.computer_use_helpers import (
    computer as computer,
)


def test_short_view_ids_keep_previous_images_and_stale_view_rejection(computer, monkeypatch):
    from core.utils import ids

    first = capture(computer)["data"]
    assert first["view_id"].startswith("view_")
    assert len(first["view_id"]) == 17
    alphabet = "0123456789abcdefghjkmnpqrstvwxyz"
    value = 0
    for char in first["view_id"].removeprefix("view_"):
        value = value * 32 + alphabet.index(char)
    original_path = Path(computer[1].presentation_images[-1]["path"])
    original = original_path.read_bytes()
    # Force the displayed-image allocation to collide with the previous view.
    sequence = iter((value, value, (value + 1) % (1 << 60)))
    monkeypatch.setattr(ids.secrets, "randbits", lambda bits: 1 if bits == 80 else next(sequence))
    second = capture(computer)["data"]
    assert second["view_id"] != first["view_id"]
    assert original_path.read_bytes() == original
    result = call(computer, "click", view_id=first["view_id"], coordinate=[1, 1], apply=True)
    assert result["error"]["code"] == "stale_view"


def test_background_capture_input_and_zoom_keep_one_coordinate_space(computer):
    data = capture(computer, foreground=False)["data"]
    assert data["foreground"] is False
    client = computer[2]
    assert client.calls[-1][1]["_background_capture"] is True
    zoomed = call(
        computer, "zoom", view_id=data["view_id"], coordinate=[0, 0], to_coordinate=[100, 100]
    )
    result = call(
        computer,
        "click",
        view_id=zoomed["data"]["view_id"],
        coordinate=[10, 20],
        foreground=False,
        apply=True,
    )
    assert result["ok"]
    args = next(args for name, args in client.calls if name == "click")
    assert args["delivery_mode"] == "background" and args["x"] == 10 and args["y"] == 20
    assert result["data"]["observation"]["foreground"] is False


@pytest.mark.parametrize("sequence", [False, True])
def test_switching_capture_delivery_refuses_before_any_input(computer, sequence):
    data = capture(computer, foreground=False)["data"]
    pointer = {"action": "click", "view_id": data["view_id"], "coordinate": [10, 20]}
    arguments = (
        {"action": "sequence", "steps": [{"action": "type", "text": "never"}, pointer]}
        if sequence
        else pointer
    )
    result = call(computer, **arguments, apply=True, foreground=True)
    assert result["error"]["code"] == "capture_required"
    assert computer[2].inputs == 0


def test_background_timed_sequence_rejects_before_any_input(computer):
    capture(computer, foreground=False)
    result = call(
        computer,
        "sequence",
        foreground=False,
        apply=True,
        steps=[
            {"action": "type", "text": "never"},
            {"action": "key", "shortcut": "enter", "duration_ms": 100},
        ],
    )
    assert result["error"]["code"] == "invalid_arguments"
    assert computer[2].inputs == 0


def test_background_focus_change_stops_sequence_after_dispatched_step(computer):
    data = capture(computer, foreground=False)["data"]
    client = computer[2]
    original = client.call

    def response(name, args):
        result = original(name, args)
        if name == "click":
            result["target_became_foreground"] = True
        return result

    client.call = response
    result = call(
        computer,
        "sequence",
        foreground=False,
        apply=True,
        steps=[
            {"action": "click", "view_id": data["view_id"], "coordinate": [10, 20]},
            {"action": "type", "text": "never"},
        ],
    )
    assert result["data"]["completed_steps"] == 1
    assert result["data"]["partial"]
    assert result["data"]["error"]["code"] == "background_focus_changed"
    assert client.inputs == 1


def test_window_defaults_execute_once_in_background_with_a_compact_image(computer):
    service, context, client, _ = computer
    original = client.call

    def shallow_capture(name, args):
        payload = original(name, args)
        if name == "capture_pixels":
            payload.update(elements_complete=False, degraded=True)
        return payload

    client.call = shallow_capture
    first = service.handle(context, {"action": "capture", "pid": 1, "window_id": 2})
    assert first["data"]["foreground"] is False
    assert first["data"]["mode"] == "vision"
    assert "elements" not in first["data"]
    assert "degraded" not in first["data"] and "elements_complete" not in first["data"]
    result = service.handle(
        context,
        {
            "action": "click",
            "view_id": first["data"]["view_id"],
            "coordinate": [10, 20],
        },
    )
    assert result["ok"] and result["data"]["applied"]
    assert client.inputs == 1
    inputs = [args for name, args in client.calls if name == "click"]
    assert inputs[0]["delivery_mode"] == "background"
    assert inputs[0]["pid"] == 1 and inputs[0]["window_id"] == 2
    assert len(context.result_media) == 2
    assert len(json.dumps(result)) < 1000
    assert result["data"]["observation"]["target"] == {"pid": 1, "window_id": 2}


def test_launch_without_apply_is_not_a_silent_preview(computer):
    service, context, client, _ = computer
    result = service.handle(context, {"action": "launch", "app": "test-owned-app"})
    assert result["ok"] and result["data"]["applied"]
    assert client.inputs == 1


@pytest.mark.parametrize("duration", [None, 1800])
@pytest.mark.parametrize("sequence", [False, True])
def test_background_drawing_preserves_the_requested_duration(computer, duration, sequence):
    data = capture(computer)["data"]
    step = {"action": "drag", "coordinate": [10, 20], "to_coordinate": [50, 60]}
    if duration is not None:
        step["duration_ms"] = duration
    args = {"action": "sequence", "steps": [step]} if sequence else step
    result = call(computer, **args, view_id=data["view_id"])
    assert result["ok"] and result["data"]["applied"]
    inputs = [args for name, args in computer[2].calls if name == "drag"]
    assert len(inputs) == 1
    assert inputs[0]["delivery_mode"] == "background"
    assert inputs[0]["duration_ms"] == (250 if duration is None else duration)


def test_zoom_infers_window_keeps_parent_view_and_maps_nested_crops(computer):
    service, context, client, _ = computer
    client.size = (3840, 2160)
    initial = capture(computer)["data"]
    first = service.handle(
        context,
        {
            "action": "zoom",
            "view_id": initial["view_id"],
            "coordinate": [100, 100],
            "to_coordinate": [200, 200],
        },
    )
    assert first["ok"]
    assert first["data"]["target"] == initial["target"]
    second = service.handle(
        context,
        {
            "action": "zoom",
            "view_id": first["data"]["view_id"],
            "coordinate": [10, 20],
            "to_coordinate": [100, 110],
        },
    )
    assert second["ok"] and second["data"]["image_width"] == 90
    parent_again = service.handle(
        context,
        {
            "action": "zoom",
            "view_id": initial["view_id"],
            "coordinate": [200, 100],
            "to_coordinate": [300, 200],
        },
    )
    assert parent_again["ok"] and client.inputs == 0
    result = service.handle(
        context,
        {
            "action": "click",
            "view_id": second["data"]["view_id"],
            "coordinate": [5, 6],
        },
    )
    assert result["ok"]
    sent = next(args for name, args in client.calls if name == "click")
    assert (sent["x"], sent["y"]) == (255, 266)
    assert sent["delivery_mode"] == "background"
    assert (
        service.handle(
            context,
            {
                "action": "click",
                "view_id": initial["view_id"],
                "coordinate": [1, 1],
            },
        )["error"]["code"]
        == "stale_view"
    )


def test_original_resolution_survives_input_zoom_and_explicit_target(computer):
    service, context, client, _ = computer
    client.size = (2578, 1398)
    original = capture(computer, resolution="original")["data"]
    crop = service.handle(
        context,
        {
            "action": "zoom",
            "view_id": original["view_id"],
            "coordinate": [100, 100],
            "to_coordinate": [300, 300],
        },
    )["data"]
    assert crop["parent_view_id"] == original["view_id"]
    assert crop["coordinate_space"] == "image_pixels"
    result = service.handle(
        context, {"action": "click", "view_id": crop["view_id"], "coordinate": [10, 10]}
    )["data"]
    assert (
        result["observation"]["image_width"],
        result["observation"]["image_height"],
    ) == client.size
    result = call(computer, "key", shortcut="enter")["data"]
    assert result["observation"]["image_width"] == 2578
    assert capture(computer, resolution="auto")["data"]["image_width"] == 1600


def test_zoom_foreground_is_a_consistency_assertion_not_a_delivery_switch(computer):
    data = capture(computer, foreground=True)["data"]
    fields = {"view_id": data["view_id"], "coordinate": [0, 0], "to_coordinate": [100, 100]}
    result = call(computer, "zoom", foreground=False, **fields)
    assert result["error"]["code"] == "invalid_arguments"
    assert call(computer, "zoom", foreground=True, **fields)["ok"]
    assert computer[2].inputs == 0


def test_resolution_preference_outlives_views_after_skipped_observation(computer):
    computer[2].size = (2578, 1398)
    capture(computer, resolution="original")
    assert call(computer, "key", shortcut="enter", capture_after=False)["ok"]
    assert capture(computer)["data"]["image_width"] == 2578
    assert call(computer, "key", shortcut="enter", capture_after=False, resolution="auto")["ok"]
    assert capture(computer)["data"]["image_width"] == 1600


def test_unexpected_observation_failure_preserves_applied_input(computer):
    capture(computer)
    client = computer[2]

    def fail(name):
        if name == "capture_pixels" and client.inputs:
            raise RuntimeError("test-owned private diagnostic")

    client.hook = fail
    result = call(computer, "type", text="draft")
    assert result["ok"] and result["data"]["applied"]
    assert result["data"]["observation_error"]["code"] == "observation_failed"
    assert "test-owned private diagnostic" not in json.dumps(result)
    assert client.inputs == 1
