"""Request image budget: images stay until a Provider limit retires the oldest delivered ones."""

from __future__ import annotations

from copy import deepcopy

import pytest

from core.chat.errors import ImageBudgetExceededError
from core.chat.wire_shaping import RequestImageBudget, limit_request_images
from core.providers.adapter import TOOL_RESULT_CONTENT_BLOCKS_FIELD


def _resolved_image_result(index: int, size: int = 4) -> dict:
    return {
        "role": "tool",
        "tool_call_id": f"call-{index}",
        "content": f"result-{index}",
        TOOL_RESULT_CONTENT_BLOCKS_FIELD: [
            {"type": "media", "media_type": "image/png", "base64": "A" * size},
            {"type": "text", "text": f"path-{index}"},
        ],
    }


def _as_user_message(message: dict) -> dict:
    message["role"] = "user"
    message["content"] = message.pop(TOOL_RESULT_CONTENT_BLOCKS_FIELD)
    return message


def _native_image_calls(messages: list[dict]) -> list[str]:
    return [
        message["tool_call_id"]
        for message in messages
        if any(
            block.get("type") == "media" and block.get("media_type") == "image/png"
            for block in message.get(TOOL_RESULT_CONTENT_BLOCKS_FIELD, [])
        )
    ]


def test_without_a_limit_every_image_stays_and_the_request_prefix_is_stable() -> None:
    source = [_resolved_image_result(index, 1024 * 1024) for index in range(200)]
    source.insert(2, {"role": "assistant", "content": "inspection findings"})
    before = deepcopy(source)

    bounded = limit_request_images(source)

    assert bounded == before
    assert source == before
    # A later text-only Tool step and a re-read append without rewriting the prefix.
    grown = limit_request_images(
        [
            *bounded,
            {"role": "assistant", "tool_calls": [{"id": "text", "name": "read", "arguments": {}}]},
            {"role": "tool", "tool_call_id": "text", "content": "notes"},
            _resolved_image_result(0),
        ]
    )
    assert grown[: len(bounded)] == bounded
    assert _native_image_calls(grown) == [*[f"call-{index}" for index in range(200)], "call-0"]


def test_count_limit_retires_the_oldest_down_to_half_then_preserves_the_prefix() -> None:
    budget = RequestImageBudget()
    original = [_resolved_image_result(i) for i in range(10)]
    original[0] = _as_user_message({**original[0], "id": "reference"})
    budget.record_delivered(original)
    expanded = [*original, _resolved_image_result(10)]

    bounded = budget.project(expanded, image_limit=10, remember=True)

    # One image over the limit retires down to five, across user and Tool roles.
    assert _native_image_calls(bounded) == [f"call-{i}" for i in range(6, 11)]
    assert all(block["type"] != "media" for block in bounded[0]["content"])
    assert _native_image_calls(original) == [f"call-{i}" for i in range(1, 10)]
    budget.record_delivered(bounded)
    continued = budget.project([*bounded, _resolved_image_result(11)], image_limit=10)
    assert continued[: len(bounded)] == bounded
    # Attachment resolution restores retired pixels in every rebuild; the budget
    # keeps their notes even without pressure or a limit.
    assert budget.project(expanded) == bounded


@pytest.mark.parametrize("role", ["user", "tool"])
def test_fresh_images_reach_the_model_or_fail_explicitly(role: str) -> None:
    delivered = [_resolved_image_result(i) for i in range(3)]
    budget = RequestImageBudget()
    budget.record_delivered(delivered)
    fresh = _resolved_image_result(3)
    fresh[TOOL_RESULT_CONTENT_BLOCKS_FIELD] *= 3
    if role == "user":
        fresh = _as_user_message({**fresh, "id": "user-current"})

    # Delivered images give way, however far below half the limit that leaves.
    bounded = budget.project([*delivered, fresh], image_limit=4)
    assert _native_image_calls(bounded) == ([] if role == "user" else ["call-3"])
    assert bounded[-1] == fresh

    before = deepcopy(fresh)
    with pytest.raises(ImageBudgetExceededError) as failure:
        budget.project([*delivered, fresh], image_limit=2)
    assert (failure.value.count, failure.value.max_count) == (3, 2)
    assert fresh == before


def test_projection_commits_nothing_until_asked() -> None:
    budget = RequestImageBudget()
    original = [_resolved_image_result(i) for i in range(4)]
    # Projecting a request does not acknowledge delivery: a failed request stays fresh.
    assert budget.project(original, image_limit=4, remember=True) == original
    with pytest.raises(ImageBudgetExceededError):
        budget.project([*original, _resolved_image_result(4)], image_limit=4)
    # A projection without remember (e.g. for Compaction) does not commit retirement.
    budget.record_delivered(original)
    projected = budget.project([*original, _resolved_image_result(4)], image_limit=4)
    assert _native_image_calls(projected) == ["call-3", "call-4"]
    assert budget.project(original) == original
    assert budget.take_unsaved_pin() is None


@pytest.mark.parametrize(
    ("max_bytes", "kept"),
    [(40, ["call-7", "call-8"]), (None, ["call-5", "call-6", "call-7", "call-8"])],
    ids=["known-limit", "unknown-limit"],
)
def test_body_rejection_retires_image_data_down_to_half(
    max_bytes: int | None, kept: list[str]
) -> None:
    budget = RequestImageBudget()
    messages = [_resolved_image_result(i, 10) for i in range(9)]
    budget.record_delivered(messages[:-1])

    smaller = budget.shrink(messages, max_bytes=max_bytes)

    assert _native_image_calls(smaller) == kept
    # Every further rejection retires at least one more image until only fresh ones
    # remain; then nothing changes, and Chat surfaces the error.
    while _native_image_calls(smaller) != ["call-8"]:
        fewer = budget.shrink(smaller, max_bytes=max_bytes)
        assert len(_native_image_calls(fewer)) < len(_native_image_calls(smaller))
        smaller = fewer
    assert budget.shrink(smaller, max_bytes=max_bytes) == smaller


def test_retirements_persist_and_earlier_images_count_as_delivered() -> None:
    history = [_resolved_image_result(i) for i in range(6)]
    current = _as_user_message({**_resolved_image_result(6), "id": "user-current"})
    messages = [*history, current]
    first_run = RequestImageBudget()
    first_run.record_delivered(history)
    retired = first_run.shrink(messages, max_bytes=None)
    pin = first_run.take_unsaved_pin()
    assert pin is not None
    assert first_run.take_unsaved_pin() is None

    next_run = RequestImageBudget()
    next_run.restore(pin, messages, current_user_message_id="user-current")

    # The next Run renders the same notes, so its request prefix stays cached.
    assert next_run.project(messages) == retired
    # Images from before the Run may retire; the new user turn's image is fresh.
    with pytest.raises(ImageBudgetExceededError):
        RequestImageBudget().project(messages, image_limit=1)
    fresh_only = next_run.project(messages, image_limit=1)
    assert _native_image_calls(fresh_only) == []
    assert fresh_only[-1] == current


def test_changed_image_at_the_same_address_is_fresh() -> None:
    original = _resolved_image_result(0)
    budget = RequestImageBudget()
    budget.record_delivered([original])
    changed = deepcopy(original)
    changed[TOOL_RESULT_CONTENT_BLOCKS_FIELD][0]["base64"] = "BBBB"

    with pytest.raises(ImageBudgetExceededError):
        budget.project([changed, _resolved_image_result(1)], image_limit=1)
