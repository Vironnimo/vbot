"""Compaction Engine contracts shared by every Strategy: triggers, usefulness, prompts, epochs."""

from __future__ import annotations

import json
import threading
from collections.abc import AsyncIterator
from typing import Any

import pytest

from core.chat import ChatMessage
from core.chat._message_history import effective_compaction_messages
from core.chat._tool_epoch import ToolChange
from core.chat.messages import COMPACTION_SKILL_NOTE_PREFIX
from core.chat.wire_shaping import _embed_notes_into_request
from core.compaction import (
    COMPACTION_TRIGGER_MANUAL,
    MIN_AUTO_COMPACTION_RECLAIM_TOKENS,
    CompactionError,
    CompactionInsufficientReclaimError,
    CompactionService,
    CompactionSettings,
    is_compacted_tool_result_content,
)
from core.compaction.compaction import COMPACTION_TRIGGER_AUTO, CompactionPlan
from core.sessions import is_tool_change_note
from core.sessions.history import _skill_context_note_content
from core.tools import tool_success
from tests.core.compaction.compaction_test_support import (
    PROMPT_FRAGMENT,
    StubAdapter,
    StubStorage,
    assistant,
    checkpoint,
    compact,
    provider_request,
    tool_step,
    user,
)


class RetainContextStrategy:
    id = "retain-context"

    def plan(self, context: Any, settings: Any) -> CompactionPlan:
        del settings
        return CompactionPlan(
            model_messages=None,
            model_target="summary",
            summary_text="RETAINED",
            after_summary=tuple(context.messages),
            compacted_token_count=1,
        )


_RATIO = CompactionSettings(threshold=0.8)
_INPUT_TOKENS = CompactionSettings(trigger="input_tokens", trigger_tokens=100_000)
_CAPPED_RATIO = CompactionSettings(threshold=0.8, max_input_tokens=200_000)


@pytest.mark.parametrize(
    ("input_tokens", "context_window", "settings", "expected"),
    [
        (80, 100, _RATIO, True),
        (79, 100, _RATIO, False),
        (100_000, 1_000_000, _INPUT_TOKENS, True),
        (99_999, 1_000_000, _INPUT_TOKENS, False),
        (200_000, 1_000_000, _CAPPED_RATIO, True),
        (199_999, 1_000_000, _CAPPED_RATIO, False),
        (80_000, 100_000, _CAPPED_RATIO, True),
    ],
    ids=[
        "ratio-reached",
        "ratio-below",
        "tokens-reached",
        "tokens-below",
        "cap-reached-first",
        "cap-below",
        "ratio-reached-under-cap",
    ],
)
def test_auto_compaction_triggers_at_the_first_reached_limit(
    input_tokens: int, context_window: int, settings: CompactionSettings, expected: bool
) -> None:
    assert (
        CompactionService().should_auto_compact(
            input_tokens, context_window, settings.threshold, settings=settings
        )
        is expected
    )


_TURN_USER = user("u1", "One long-running user turn")
_RETAINED_USER = user("u1", "Previously retained turn " * 100)
_FIRST_STEP = tool_step("1", "result one")
_LARGE_HISTORY = [user("u1", "old request " * 2_000), assistant("a1", "old answer " * 2_000)]
_EMPTY_CHECKPOINT = checkpoint([])


@pytest.mark.parametrize(
    ("strategy", "messages", "minimum_reclaim", "expected"),
    [
        (
            "summary_tail",
            [
                _TURN_USER,
                *_FIRST_STEP,
                checkpoint([_TURN_USER, *_FIRST_STEP]),
                *tool_step("2", "result two", name="edit"),
            ],
            0,
            True,
        ),
        ("summary_tail", [_RETAINED_USER, checkpoint([_RETAINED_USER])], 0, False),
        ("continuation", _LARGE_HISTORY, 0, True),
        ("continuation", [*_LARGE_HISTORY, _EMPTY_CHECKPOINT], 0, False),
        ("continuation", [_EMPTY_CHECKPOINT, assistant("a2", "small step")], 0, True),
        (
            "continuation",
            [_EMPTY_CHECKPOINT, assistant("a2", "small step")],
            MIN_AUTO_COMPACTION_RECLAIM_TOKENS,
            False,
        ),
        (
            "continuation",
            [_EMPTY_CHECKPOINT, assistant("a2", "large step " * 5_000)],
            MIN_AUTO_COMPACTION_RECLAIM_TOKENS,
            True,
        ),
    ],
    ids=[
        "summary-tail-advances-inside-a-retained-user-turn",
        "summary-tail-waits-with-only-the-prior-summary-in-head",
        "continuation-without-checkpoint",
        "continuation-nothing-after-checkpoint",
        "continuation-small-step",
        "continuation-small-step-below-reclaim-floor",
        "continuation-large-step-above-reclaim-floor",
    ],
)
def test_new_compactable_context_decides_whether_a_model_call_is_worth_it(
    strategy: str, messages: list[ChatMessage], minimum_reclaim: int, expected: bool
) -> None:
    assert (
        CompactionService().has_new_compactable_context(
            messages,
            CompactionSettings(strategy=strategy, tail_tokens=1),
            minimum_reclaim_tokens=minimum_reclaim,
        )
        is expected
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("strategy", "trigger", "fragment"),
    [
        ("summary_tail", COMPACTION_TRIGGER_AUTO, "compaction.md"),
        ("summary_tail", COMPACTION_TRIGGER_MANUAL, "compaction-manual.md"),
        ("continuation", COMPACTION_TRIGGER_AUTO, "compaction-continuation.md"),
        ("continuation", COMPACTION_TRIGGER_MANUAL, "compaction-continuation-manual.md"),
    ],
)
async def test_compaction_instruction_is_one_reminder_from_the_strategy_and_trigger_prompt(
    strategy: str, trigger: str, fragment: str
) -> None:
    adapter = StubAdapter("NEW SUMMARY")
    storage = StubStorage()
    messages = [user("u1", "old request"), assistant("a1", "recent response")]

    await compact(
        messages,
        summary_adapter=adapter,
        active_adapter=adapter,
        active_model_id="openai/summary",
        storage=storage,
        settings=CompactionSettings(strategy=strategy, tail_tokens=1),
        request_messages=provider_request(messages),
        trigger=trigger,
    )

    reminder = str(adapter.requests[0]["messages"][-1]["content"])
    assert storage.read_names == [fragment]
    assert reminder.startswith("<system-reminder>\n")
    assert reminder.endswith("\n</system-reminder>")
    assert reminder.count(PROMPT_FRAGMENT) == 1
    assert "<retained_tail>" not in reminder


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["summary_tail", "continuation"])
async def test_model_backed_strategies_require_the_active_request(strategy: str) -> None:
    adapter = StubAdapter()

    with pytest.raises(CompactionError):
        await compact(
            [user("u1", "old"), assistant("a1", "tail")],
            summary_adapter=adapter,
            settings=CompactionSettings(strategy=strategy, tail_tokens=1),
        )

    assert adapter.requests == []


@pytest.mark.asyncio
async def test_automatic_compaction_rejects_projection_below_minimum_reclaim() -> None:
    adapter = StubAdapter("A summary that is intentionally much larger " * 500)
    messages = [user("u1", "old"), assistant("a1", "short"), user("u2", "tail")]

    with pytest.raises(CompactionInsufficientReclaimError):
        await compact(
            messages,
            summary_adapter=adapter,
            request_messages=provider_request(messages),
            minimum_reclaim_tokens=MIN_AUTO_COMPACTION_RECLAIM_TOKENS,
        )

    assert len(adapter.requests) == 1


@pytest.mark.asyncio
async def test_compaction_keeps_sync_transforms_off_loop_and_model_io_on_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.compaction import compaction

    loop_thread = threading.get_ident()
    strategy_threads: list[int] = []
    stream_threads: list[int] = []
    finalization_threads: list[int] = []
    original_extract = compaction._extract_summary_text

    def recording_extract(response: dict[str, Any]) -> str:
        finalization_threads.append(threading.get_ident())
        return original_extract(response)

    monkeypatch.setattr(compaction, "_extract_summary_text", recording_extract)

    class RecordingStrategy:
        id = "recording"

        def plan(self, context: Any, settings: Any) -> CompactionPlan:
            strategy_threads.append(threading.get_ident())
            return CompactionPlan(
                model_messages=({"role": "user", "content": "compact"},),
                model_target="summary",
                compacted_token_count=1,
            )

    class RecordingAdapter(StubAdapter):
        async def stream(
            self, messages: list[dict], **kwargs: Any
        ) -> AsyncIterator[dict[str, Any]]:
            stream_threads.append(threading.get_ident())
            yield {"type": "content_delta", "text": "summary"}
            yield {"type": "finish", "reason": "stop"}

    await compact(
        [user("u1", "old context")],
        service=CompactionService(RecordingStrategy()),
        summary_adapter=RecordingAdapter(),
        settings=CompactionSettings(strategy="recording"),
    )

    assert strategy_threads and strategy_threads != [loop_thread]
    assert stream_threads == [loop_thread]
    assert finalization_threads and finalization_threads != [loop_thread]


def _skill_note(name: str) -> ChatMessage:
    content = f'<skill_content name="{name}">{name} instructions</skill_content>'
    return ChatMessage.note(_skill_context_note_content(name, content))


def _reported_skill_epochs(effective: list[ChatMessage]) -> list[list[str]]:
    """The Skill names of each `[compaction-skills]` reminder in an effective Context."""
    notes = [
        str(item.content).removeprefix(COMPACTION_SKILL_NOTE_PREFIX)
        for item in effective
        if item.role == "note" and str(item.content).startswith(COMPACTION_SKILL_NOTE_PREFIX)
    ]
    return [json.loads(note[note.index("[") : note.index("]") + 1]) for note in notes]


@pytest.mark.asyncio
async def test_compaction_reports_only_the_immediately_completed_skill_epoch() -> None:
    service = CompactionService(RetainContextStrategy())
    settings = CompactionSettings(strategy="retain-context")
    history: list[ChatMessage] = [user("u1", "First task")]

    for step, skill in enumerate(["alpha", "beta", None], start=1):
        if skill is not None:
            history.append(_skill_note(skill))
        history.append(assistant(f"a{step}", f"Result {step}"))
        history.append(await compact(history, service=service, settings=settings))
        effective = effective_compaction_messages(history)

        assert _reported_skill_epochs(effective) == ([] if skill is None else [[skill]])
        assert all(" instructions</skill_content>" not in str(item.content) for item in effective)
        assert COMPACTION_SKILL_NOTE_PREFIX not in json.dumps(_embed_notes_into_request(effective))


@pytest.mark.asyncio
async def test_compaction_drops_the_tool_change_notes_of_the_ending_epoch() -> None:
    # The next prompt epoch pins every Tool these notes announced.
    note = ChatMessage.note(ToolChange("removed", "probe", "epoch-1").note_content())
    history = [user("u1", "First task"), note, assistant("a1", "Done")]

    history.append(
        await compact(
            history,
            service=CompactionService(RetainContextStrategy()),
            settings=CompactionSettings(strategy="retain-context"),
        )
    )

    effective = effective_compaction_messages(history)
    assert [item.id for item in effective if item.role != "note"] == ["u1", "a1"]
    assert not any(is_tool_change_note(item) for item in effective)


@pytest.mark.asyncio
async def test_compaction_compacts_skill_tool_carrier_without_breaking_its_cycle() -> None:
    skill_result_content = json.dumps(
        tool_success(
            {
                "name": "docx",
                "status": "loaded",
                "content": "Full document instructions",
                "environment_access": "Use DOCX_KEY through bash env_keys.",
            }
        ),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    messages = [
        user("u1", "Create a document"),
        *tool_step("skill", skill_result_content, name="skill", arguments={"name": "docx"}),
        assistant("a2", "Used the Skill"),
    ]

    result = await compact(
        messages,
        service=CompactionService(RetainContextStrategy()),
        settings=CompactionSettings(strategy="retain-context"),
    )
    effective = effective_compaction_messages([*messages, result])
    projected_result = next(item for item in effective if item.id == "t-skill")

    assert [item.id for item in effective if item.role in {"assistant", "tool"}] == [
        "a-skill",
        "t-skill",
        "a2",
    ]
    assert is_compacted_tool_result_content(projected_result.content)
    assert json.loads(str(projected_result.content))["outcome"] == {
        "name": "docx",
        "status": "loaded",
        "compacted": True,
    }
    assert "Full document instructions" not in str(projected_result.content)
    assert "DOCX_KEY" not in str(projected_result.content)
