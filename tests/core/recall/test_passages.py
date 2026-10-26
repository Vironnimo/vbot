"""The shared canonical Passage policy."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from core.chat import ChatMessage
from core.chat.messages import ToolCall
from core.recall.passages import build_session_passages


def timestamp(day: int) -> datetime:
    return datetime(2026, 5, day, 12, tzinfo=UTC)


def test_passages_preserve_source_text_without_per_message_truncation() -> None:
    original = "  start\n" + ("ä\t" * 2_000) + "\nend  "
    message = ChatMessage.user(original, timestamp=timestamp(1))

    passages = build_session_passages([message], target_chars=1_500, overlap_chars=200)

    assert len(passages) > 1
    assert passages[0].text == original[:1_500]
    assert passages[-1].text.endswith("\nend  ")
    assert all(passage.start_message_id == message.id for passage in passages)
    assert all(passage.end_message_id == message.id for passage in passages)


@pytest.mark.parametrize(("target_chars", "overlap_chars"), [(1, 0), (3, 0), (6, 2), (30, 0)])
def test_passage_ids_and_boundaries_are_deterministic(
    target_chars: int, overlap_chars: int
) -> None:
    messages = [
        replace(ChatMessage.user("ä\tA", timestamp=timestamp(1)), id="m1"),
        replace(ChatMessage.assistant(model="test", content="βB", timestamp=timestamp(2)), id="m2"),
        replace(ChatMessage.user("終 D ", timestamp=timestamp(3)), id="m3"),
    ]
    expected = []
    for message in messages:
        assert isinstance(message.content, str)
        own = build_session_passages(
            [message], target_chars=target_chars, overlap_chars=overlap_chars
        )
        assert own
        assert own[-1].end_offset == len(message.content)
        for passage in own:
            assert passage.start_message_id == passage.end_message_id == message.id
            assert passage.start_timestamp == passage.end_timestamp == str(message.timestamp)
            assert passage.start_role == passage.end_role == message.role
            assert passage.text == message.content[passage.start_offset : passage.end_offset]
            assert 0 < len(passage.text) <= target_chars
        expected.extend(own)
    messages.insert(1, ChatMessage.user("", timestamp=timestamp(1)))
    messages.insert(2, ChatMessage.note("hidden", timestamp=timestamp(1)))

    passages = build_session_passages(
        messages, target_chars=target_chars, overlap_chars=overlap_chars
    )

    # Neither different neighboring entries nor ignored entries change the
    # identity/text of a canonical entry's windows.
    assert passages == expected
    assert len({passage.passage_id for passage in passages}) == len(passages)


@pytest.mark.parametrize(
    "excluded",
    [
        ChatMessage.tool(
            name="bash", content="tool payload", tool_call_id="call-1", timestamp=timestamp(1)
        ),
        # A persisted Recall result must never feed Recall back to itself.
        ChatMessage.tool(
            name="session_search", content="earlier hit", tool_call_id="c2", timestamp=timestamp(1)
        ),
        ChatMessage.note("internal note", timestamp=timestamp(1)),
        # Operational errors need an explicit diagnostic read, not ordinary Recall.
        ChatMessage.error("provider", "provider failed", timestamp=timestamp(1)),
        # Reasoning and Tool-call arguments are not conversation text.
        ChatMessage.assistant(
            model="test",
            content=None,
            reasoning="hidden reasoning",
            tool_calls=[ToolCall(id="c3", name="bash", arguments={"command": "hidden"})],
            timestamp=timestamp(1),
        ),
        replace(
            ChatMessage.assistant(model="test", content="intermediate", timestamp=timestamp(1)),
            programmatic=True,
            parent_tool_call_id="call-code",
        ),
        ChatMessage.run_summary(
            run_id="r1",
            status="completed",
            timing={
                "started_at": "2026-05-01T12:00:00+00:00",
                "completed_at": "2026-05-01T12:00:01+00:00",
                "duration_ms": 1000,
            },
            iteration_count=1,
        ),
    ],
    ids=[
        "tool",
        "recall-result",
        "note",
        "error",
        "assistant-metadata",
        "programmatic-activity",
        "run-summary",
    ],
)
def test_passages_contain_only_conversation_text(excluded: ChatMessage) -> None:
    user = ChatMessage.user("user text", timestamp=timestamp(2))

    passages = build_session_passages([excluded, user])

    assert [(passage.text, passage.start_role) for passage in passages] == [("user text", "user")]


def test_compaction_summary_is_its_own_passage() -> None:
    before = ChatMessage.user("before", timestamp=timestamp(1))
    checkpoint = ChatMessage.compaction_checkpoint(
        summary="the summary", projection=[], compacted_token_count=1, timestamp=timestamp(2)
    )
    after = ChatMessage.user("after", timestamp=timestamp(3))

    passages = build_session_passages([before, checkpoint, after])

    # Short enough for one window, yet the summary never merges with verbatim text.
    assert [(passage.text, passage.start_role, passage.end_role) for passage in passages] == [
        ("before", "user", "user"),
        ("the summary", "compaction_checkpoint", "compaction_checkpoint"),
        ("after", "user", "user"),
    ]
