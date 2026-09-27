"""Computer use: captures produce owned screenshots that input consumes and retires."""

from __future__ import annotations

import base64
from dataclasses import replace

import pytest

from tests.resources.extensions.computer_use.computer_use_test_support import call, capture, png


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


def test_driver_file_paths_and_invalid_pixels_never_become_screenshots(computer, tmp_path):
    _, context, client, _ = computer
    private = tmp_path / "private.png"
    private.write_bytes(png())
    original = client.call
    for screenshot in (
        {"screenshot_file_path": str(private)},
        {"screenshot_png_b64": base64.b64encode(b"not an image").decode()},
    ):

        def respond(name, args, screenshot=screenshot):
            payload = original(name, args)
            if name == "capture_pixels":
                payload.pop("screenshot_png_b64", None)
                payload.update(screenshot)
            return payload

        client.call = respond
        assert not capture(computer, mode="vision")["ok"]
    assert not context.result_media


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
def test_views_and_element_refs_never_cross_owners(computer, change):
    service, context, client, _ = computer
    data = capture(computer)["data"]
    other = replace(context, **change)
    by_view = service.handle(
        other,
        {
            "action": "click",
            "pid": 1,
            "window_id": 2,
            "view_id": data["view_id"],
            "coordinate": [1, 1],
            "apply": True,
        },
    )
    assert by_view["error"]["code"] == "stale_view"
    # An element ref never lets another owner infer the window it came from.
    by_element = service.handle(
        other, {"action": "click", "element": data["elements"][0]["element"]}
    )
    assert not by_element["ok"] and not by_element["artifacts"]
    assert client.inputs == 0


def test_other_run_capture_invalidates_old_view(computer):
    service, context, _, _ = computer
    capture(computer)
    assert service.handle(
        replace(context, run_id="r2"), {"action": "capture", "pid": 1, "window_id": 2}
    )["ok"]
    assert call(computer, "type", text="draft", apply=True)["error"]["code"] == "capture_required"


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


def test_window_target_mismatch_rejects_tokens(computer):
    capture(computer)
    assert (
        call(computer, "click", window_id=3, element="s00000001:1", apply=True)["error"]["code"]
        == "invalid_arguments"
    )
