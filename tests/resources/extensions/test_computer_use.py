"""Computer use: observations behavior."""

from __future__ import annotations

from dataclasses import replace

import pytest
from PIL import Image

from tests.resources.extensions.computer_use_helpers import (
    call,
    capture,
)
from tests.resources.extensions.computer_use_helpers import (
    computer as computer,
)


def test_input_returns_fresh_capture_and_does_not_echo_text(computer):
    assert (
        call(computer, "type", text="private draft", apply=True)["error"]["code"]
        == "capture_required"
    )
    assert capture(computer)["ok"]
    result = call(computer, "type", text="private draft", element="1", apply=True)
    assert result["ok"] and result["data"]["applied"]
    assert result["data"]["observation"]["view_id"]
    assert result["data"]["effect"] == "unverifiable"
    assert "backend-echo" not in str(result) and "private draft" not in str(result)
    assert call(computer, "key", shortcut="enter", apply=True)["ok"]
    assert len(computer[1].result_media) == 3


def test_preview_sends_no_input_and_does_not_consume_capture(computer):
    assert capture(computer)["ok"]
    before = len(computer[2].calls)
    assert call(computer, "type", text="private draft", apply=False)["data"]["preview"]
    assert len(computer[2].calls) == before
    assert call(computer, "type", text="private draft", apply=True)["ok"]


def test_ax_is_driver_tree_only_and_som_removes_duplicate_tree(computer):
    result = capture(computer, mode="ax", query="Draft", limit=25)
    assert result["ok"]
    assert not computer[1].result_media
    assert "tree_markdown" not in result["data"]
    name, args = computer[2].calls[-1]
    assert name == "get_window_state"
    assert (
        args["include_screenshot"] is False
        and args["query"] == "Draft"
        and args["max_elements"] == 25
    )


def test_scaled_image_coordinates_and_native_crop_round_trip(computer):
    computer[2].size = (3840, 2160)
    result = capture(computer)["data"]
    assert (result["image_width"], result["image_height"]) == (1600, 900)
    assert Image.open(computer[1].presentation_images[-1]["path"]).size == (3840, 2160)
    zoom = call(
        computer, "zoom", view_id=result["view_id"], coordinate=[100, 100], to_coordinate=[200, 200]
    )["data"]
    assert (zoom["image_width"], zoom["image_height"]) == (240, 240)
    clicked = call(computer, "click", view_id=zoom["view_id"], coordinate=[50, 60], apply=True)
    assert clicked["ok"]
    _, args = next(item for item in computer[2].calls if item[0] == "click")
    assert (args["x"], args["y"]) == (290, 300)
    assert (
        call(computer, "click", view_id=result["view_id"], coordinate=[1, 1], apply=True)["error"][
            "code"
        ]
        == "stale_view"
    )


def test_original_resolution_and_image_edges(computer):
    computer[2].size = (1800, 1000)
    data = capture(computer, resolution="original")["data"]
    assert data["image_width"] == 1800
    result = call(computer, "click", view_id=data["view_id"], coordinate=[1800, 2], apply=True)
    assert result["error"]["code"] == "invalid_coordinates"
    assert not any(name == "click" for name, _ in computer[2].calls)


@pytest.mark.parametrize(
    "change",
    [{"session_id": "other"}, {"agent_id": "other"}, {"project_id": "other"}, {"run_id": "other"}],
)
def test_capture_authority_never_crosses_context(computer, change):
    service, context, client, _ = computer
    data = capture(computer)["data"]
    result = service.handle(
        replace(context, **change),
        {
            "action": "click",
            "pid": 1,
            "window_id": 2,
            "view_id": data["view_id"],
            "coordinate": [1, 1],
            "apply": True,
        },
    )
    assert result["error"]["code"] == "stale_view"
    assert not any(name == "click" for name, _ in client.calls)


def test_other_run_capture_invalidates_old_view(computer):
    service, context, _, _ = computer
    capture(computer)
    assert service.handle(
        replace(context, run_id="r2"), {"action": "capture", "pid": 1, "window_id": 2}
    )["ok"]
    assert call(computer, "type", text="draft", apply=True)["error"]["code"] == "capture_required"


def test_window_target_mismatch_rejects_tokens(computer):
    capture(computer)
    assert (
        call(computer, "click", window_id=3, element="s00000001:1", apply=True)["error"]["code"]
        == "invalid_arguments"
    )
