"""Tests for block resolver image budget."""

from __future__ import annotations

from copy import deepcopy

import pytest

from core.chat import wire_shaping
from core.chat.errors import ImageBudgetExceededError
from core.chat.wire_shaping import RequestImageBudget, limit_request_images
from core.providers.adapter import TOOL_RESULT_CONTENT_BLOCKS_FIELD

pytestmark = pytest.mark.usefixtures("current_format_data_directory")


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


def _native_image_calls(messages: list[dict]) -> list[str]:
    return [
        message["tool_call_id"]
        for message in messages
        if any(
            block.get("type") == "media" and block.get("media_type") == "image/png"
            for block in message.get(TOOL_RESULT_CONTENT_BLOCKS_FIELD, [])
        )
    ]


def test_images_preserve_existing_request_prefix_as_run_grows() -> None:
    source = [_resolved_image_result(index) for index in range(14)]
    source.insert(2, {"role": "assistant", "content": "inspection findings"})
    before = deepcopy(source)
    bounded = limit_request_images(source)
    assert _native_image_calls(bounded) == [f"call-{index}" for index in range(14)]
    assert bounded == before
    assert source == before
    assert limit_request_images(bounded) == bounded
    for original, projected in zip(source, bounded, strict=True):
        assert projected["content"] == original["content"]
        assert projected.get("tool_call_id") == original.get("tool_call_id")
        if original.get("role") == "tool":
            assert (
                projected[TOOL_RESULT_CONTENT_BLOCKS_FIELD][-1]
                == original[TOOL_RESULT_CONTENT_BLOCKS_FIELD][-1]
            )
    reopened = limit_request_images([*bounded, _resolved_image_result(0)])
    assert reopened[: len(bounded)] == bounded
    assert _native_image_calls(reopened) == [*[f"call-{index}" for index in range(14)], "call-0"]


def test_image_budget_preserves_all_images_in_sibling_results() -> None:
    first = _resolved_image_result(0)
    second = _resolved_image_result(1)
    second[TOOL_RESULT_CONTENT_BLOCKS_FIELD] *= 4
    bounded = limit_request_images([first, second])
    assert _native_image_calls(bounded) == ["call-0", "call-1"]
    parts = bounded[1][TOOL_RESULT_CONTENT_BLOCKS_FIELD]
    assert sum(part["type"] == "media" for part in parts) == 4
    assert len(parts) == 8
    assert parts[0]["type"] == "media"


def test_image_and_text_tool_batches_preserve_all_older_images() -> None:
    older = [_resolved_image_result(index) for index in range(5)]
    fresh = [_resolved_image_result(index) for index in range(5, 9)]
    calls = [{"id": message["tool_call_id"], "name": "read", "arguments": {}} for message in fresh]
    messages = [*older, {"role": "assistant", "tool_calls": calls}, *fresh]
    bounded = limit_request_images(messages)
    assert _native_image_calls(bounded) == [f"call-{index}" for index in range(9)]
    assert limit_request_images(bounded) == bounded
    # A later text-only Tool step must not rewrite the previous image prefix.
    aged = limit_request_images(
        [
            *bounded,
            {"role": "assistant", "tool_calls": [{"id": "text", "name": "read", "arguments": {}}]},
            {"role": "tool", "tool_call_id": "text", "content": "notes"},
        ]
    )
    assert aged[: len(bounded)] == bounded
    assert _native_image_calls(aged) == [f"call-{index}" for index in range(9)]


def test_one_fresh_tool_result_can_carry_more_than_three_images() -> None:
    message = _resolved_image_result(0)
    message[TOOL_RESULT_CONTENT_BLOCKS_FIELD] *= 4
    bounded = limit_request_images(
        [
            {"role": "assistant", "tool_calls": [{"id": "call-0"}]},
            message,
        ]
    )
    assert (
        sum(block["type"] == "media" for block in bounded[1][TOOL_RESULT_CONTENT_BLOCKS_FIELD]) == 4
    )


def test_four_user_images_are_all_sent_up_to_150_mib() -> None:
    payload = "A" * (150 * 1024 * 1024 // 4)
    content = [
        {
            "type": "media",
            "media_type": "image/png",
            "base64": payload,
        }
        for _ in range(4)
    ]
    message = {"role": "user", "content": content}
    bounded = limit_request_images([message])
    assert bounded[0] == message
    assert len(bounded[0]["content"]) == 4
    assert _native_image_calls(bounded) == []
    with pytest.raises(ImageBudgetExceededError):
        limit_request_images([message, _resolved_image_result(0)])


def test_image_byte_budget_reserves_user_references_before_recent_tools(
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
    messages = [
        reference,
        _resolved_image_result(0, 4),
        _resolved_image_result(1, 12),
    ]
    budget = RequestImageBudget()
    budget.record_delivered([messages[1]])
    bounded = limit_request_images(messages, budget=budget)
    assert bounded[0] == reference
    assert _native_image_calls(bounded) == ["call-1"]
    assert bounded[1][TOOL_RESULT_CONTENT_BLOCKS_FIELD][0]["type"] == "text"


@pytest.mark.parametrize("role", ["user", "tool"])
def test_single_oversized_fresh_image_fails_without_dropping_other_media(
    role: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(wire_shaping, "REQUEST_IMAGE_BYTES_LIMIT", 16)
    message = _resolved_image_result(0, 20)
    field = TOOL_RESULT_CONTENT_BLOCKS_FIELD
    if role == "user":
        message["role"] = role
        message["content"] = message.pop(field)
        field = "content"
    audio = {"type": "media", "media_type": "audio/wav", "base64": "audio-sentinel"}
    document = {"type": "document", "media_type": "application/pdf", "base64": "pdf-sentinel"}
    message[field].extend([audio, document])
    before = deepcopy(message)
    with pytest.raises(ImageBudgetExceededError) as failure:
        limit_request_images([message])
    assert failure.value.count == 1
    assert failure.value.size_bytes == 20
    assert failure.value.max_bytes == 16
    assert message == before
    assert message[field][0]["type"] == "media"


@pytest.mark.parametrize("groups", [[4], [1, 1, 1, 1], [20, 20, 10]])
def test_fresh_image_groups_are_all_retained(groups: list[int]) -> None:
    messages = []
    for index, count in enumerate(groups):
        message = _resolved_image_result(index)
        message[TOOL_RESULT_CONTENT_BLOCKS_FIELD] *= count
        messages.append(message)
    assert limit_request_images(messages) == messages


@pytest.mark.parametrize("role,groups", [("user", [51]), ("tool", [51]), ("tool", [17, 17, 17])])
def test_fresh_image_overflow_counts_individual_images(role: str, groups: list[int]) -> None:
    messages = []
    for index, count in enumerate(groups):
        message = _resolved_image_result(index)
        message[TOOL_RESULT_CONTENT_BLOCKS_FIELD] *= count
        if role == "user":
            message["role"] = role
            message["content"] = message.pop(TOOL_RESULT_CONTENT_BLOCKS_FIELD)
        messages.append(message)
    before = deepcopy(messages)
    with pytest.raises(ImageBudgetExceededError) as failure:
        limit_request_images(messages)
    assert failure.value.count == 51
    assert failure.value.max_count == 50
    assert messages == before


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


def test_image_projection_does_not_acknowledge_failed_requests() -> None:
    budget = RequestImageBudget()
    original = [_resolved_image_result(i) for i in range(50)]
    assert budget.project(original, remember=True) == original
    with pytest.raises(ImageBudgetExceededError):
        budget.project([*original, _resolved_image_result(50)])


def test_image_projection_for_compaction_does_not_commit_retirement() -> None:
    budget = RequestImageBudget()
    original = [_resolved_image_result(i) for i in range(50)]
    budget.record_delivered(original)
    assert len(_native_image_calls(budget.project([*original, _resolved_image_result(50)]))) == 4
    assert budget.project(original) == original


def test_provider_pressure_keeps_four_newest_images_across_user_and_tool_roles() -> None:
    budget = RequestImageBudget()
    messages = [_resolved_image_result(i) for i in range(8)]
    messages[0]["role"] = "user"
    messages[0]["id"] = "reference"
    messages[0]["content"] = messages[0].pop(TOOL_RESULT_CONTENT_BLOCKS_FIELD)
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
