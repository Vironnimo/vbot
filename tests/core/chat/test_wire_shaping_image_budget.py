"""Request image budget: fresh images reach the Model; delivered ones retire under pressure."""

from __future__ import annotations

from copy import deepcopy

import pytest

from core.chat import wire_shaping
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


def test_under_the_limits_every_image_stays_and_the_request_prefix_is_stable() -> None:
    source = [_resolved_image_result(index) for index in range(14)]
    source.insert(2, {"role": "assistant", "content": "inspection findings"})
    before = deepcopy(source)

    bounded = limit_request_images(source)

    assert bounded == before
    assert source == before
    assert limit_request_images(bounded) == bounded
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
    assert _native_image_calls(grown) == [*[f"call-{index}" for index in range(14)], "call-0"]


@pytest.mark.parametrize("groups", [[1, 4], [1, 1, 1, 1], [20, 20, 10]])
def test_fresh_image_groups_up_to_the_count_limit_are_all_retained(groups: list[int]) -> None:
    messages = []
    for index, count in enumerate(groups):
        message = _resolved_image_result(index)
        message[TOOL_RESULT_CONTENT_BLOCKS_FIELD] *= count
        messages.append(message)

    assert limit_request_images(messages) == messages


def test_fresh_user_images_are_all_sent_up_to_150_mib() -> None:
    payload = "A" * (150 * 1024 * 1024 // 4)
    content = [{"type": "media", "media_type": "image/png", "base64": payload} for _ in range(4)]
    message = {"role": "user", "content": content}

    assert limit_request_images([message])[0] == message
    with pytest.raises(ImageBudgetExceededError):
        limit_request_images([message, _resolved_image_result(0)])


@pytest.mark.parametrize(("role", "groups"), [("user", [51]), ("tool", [17, 17, 17])])
def test_fresh_image_overflow_counts_individual_images(role: str, groups: list[int]) -> None:
    messages = []
    for index, count in enumerate(groups):
        message = _resolved_image_result(index)
        message[TOOL_RESULT_CONTENT_BLOCKS_FIELD] *= count
        messages.append(_as_user_message(message) if role == "user" else message)
    before = deepcopy(messages)

    with pytest.raises(ImageBudgetExceededError) as failure:
        limit_request_images(messages)

    assert (failure.value.count, failure.value.max_count) == (51, 50)
    assert messages == before


def test_single_oversized_fresh_image_fails_without_dropping_other_media(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(wire_shaping, "REQUEST_IMAGE_BYTES_LIMIT", 16)
    message = _resolved_image_result(0, 20)
    message[TOOL_RESULT_CONTENT_BLOCKS_FIELD] += [
        {"type": "media", "media_type": "audio/wav", "base64": "audio-sentinel"},
        {"type": "document", "media_type": "application/pdf", "base64": "pdf-sentinel"},
    ]
    before = deepcopy(message)

    with pytest.raises(ImageBudgetExceededError) as failure:
        limit_request_images([message])

    assert (failure.value.count, failure.value.size_bytes, failure.value.max_bytes) == (1, 20, 16)
    assert message == before


def test_image_byte_budget_keeps_fresh_images_before_delivered_tool_images(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(wire_shaping, "REQUEST_IMAGE_BYTES_LIMIT", 16)
    reference = {
        "role": "user",
        "content": [
            {"type": "media", "media_type": "image/png", "base64": "A" * 4},
            {"type": "text", "text": "reference-path"},
        ],
    }
    messages = [reference, _resolved_image_result(0, 4), _resolved_image_result(1, 12)]
    budget = RequestImageBudget()
    budget.record_delivered([messages[1]])

    bounded = limit_request_images(messages, budget=budget)

    assert bounded[0] == reference
    assert _native_image_calls(bounded) == ["call-1"]
    assert bounded[1][TOOL_RESULT_CONTENT_BLOCKS_FIELD][0]["type"] == "text"


def test_image_pressure_reclaims_a_batch_then_preserves_the_prefix() -> None:
    budget = RequestImageBudget()
    original = [_resolved_image_result(i) for i in range(50)]
    budget.record_delivered(original)
    expanded = [*original, _resolved_image_result(50)]

    bounded = budget.project(expanded, remember=True)

    assert _native_image_calls(bounded) == [f"call-{i}" for i in range(47, 51)]
    assert _native_image_calls(original) == [f"call-{i}" for i in range(50)]
    budget.record_delivered(bounded)
    continued = budget.project([*bounded, _resolved_image_result(51)], remember=True)
    assert continued[: len(bounded)] == bounded
    assert _native_image_calls(continued) == [f"call-{i}" for i in range(47, 52)]
    # Canonical attachment resolution may restore retired pixels. A same-Run
    # rebuild must keep their placeholders even with no current budget pressure.
    assert budget.project(expanded) == bounded
    assert _native_image_calls(budget.project([original[0]])) == []


def test_projection_commits_nothing_until_asked() -> None:
    budget = RequestImageBudget()
    original = [_resolved_image_result(i) for i in range(50)]
    # Projecting a request does not acknowledge delivery: a failed request stays fresh.
    assert budget.project(original, remember=True) == original
    with pytest.raises(ImageBudgetExceededError):
        budget.project([*original, _resolved_image_result(50)])
    # A projection without remember (e.g. for Compaction) does not commit retirement.
    budget.record_delivered(original)
    assert len(_native_image_calls(budget.project([*original, _resolved_image_result(50)]))) == 4
    assert budget.project(original) == original


def test_provider_pressure_keeps_four_newest_images_across_user_and_tool_roles() -> None:
    budget = RequestImageBudget()
    messages = [_resolved_image_result(i) for i in range(8)]
    messages[0]["id"] = "reference"
    _as_user_message(messages[0])
    before = deepcopy(messages)
    budget.record_delivered(messages[:-1])

    bounded = budget.project(messages, force=True, remember=True)

    assert _native_image_calls(bounded) == [f"call-{i}" for i in range(4, 8)]
    assert all(block["type"] != "media" for block in bounded[0]["content"])
    assert messages == before
    assert budget.project(messages) == bounded
    # Another Provider/Compaction projection cannot reactivate retired pixels.
    assert budget.project([messages[0]])[0] == bounded[0]


def test_provider_pressure_obeys_four_mib_target_and_protects_fresh_images() -> None:
    budget = RequestImageBudget()
    messages = [_resolved_image_result(i, 2 * 1024 * 1024) for i in range(7)]
    budget.record_delivered(messages[:-1])
    assert _native_image_calls(budget.project(messages, force=True)) == ["call-5", "call-6"]
    # A fresh image above the soft target must still reach the Model once.
    fresh = _resolved_image_result(7, 5 * 1024 * 1024)
    bounded = budget.project([*messages, fresh], force=True)
    assert _native_image_calls(bounded) == ["call-6", "call-7"]
    assert bounded[-1] == fresh


def test_further_provider_pressure_retires_old_images_until_only_fresh_remain() -> None:
    budget = RequestImageBudget()
    messages = [_resolved_image_result(i) for i in range(3)]
    budget.record_delivered(messages[:-1])
    first = budget.project(messages, force=True, remember=True)
    assert _native_image_calls(first) == ["call-1", "call-2"]
    second = budget.project(first, force=True, remember=True)
    assert _native_image_calls(second) == ["call-2"]
    assert budget.project(second, force=True, remember=True) == second
    assert budget.project(messages) == second


def test_fresh_images_take_priority_over_delivered_user_references(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(wire_shaping, "REQUEST_IMAGE_COUNT_LIMIT", 4)
    user = {
        "role": "user",
        "id": "user-1",
        "content": [
            {"type": "media", "media_type": "image/png", "base64": "AAAA"} for _ in range(4)
        ],
    }
    budget = RequestImageBudget()
    budget.record_delivered([user])
    fresh = [_resolved_image_result(i) for i in range(2)]

    bounded = budget.project([user, *fresh])

    assert _native_image_calls(bounded) == ["call-0", "call-1"]
    assert sum(block["type"] == "media" for block in bounded[0]["content"]) == 2


def test_changed_image_at_the_same_address_is_fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wire_shaping, "REQUEST_IMAGE_COUNT_LIMIT", 1)
    original = _resolved_image_result(0)
    budget = RequestImageBudget()
    budget.record_delivered([original])
    changed = deepcopy(original)
    changed[TOOL_RESULT_CONTENT_BLOCKS_FIELD][0]["base64"] = "BBBB"

    with pytest.raises(ImageBudgetExceededError):
        budget.project([changed, _resolved_image_result(1)])
