"""Tests for the shared canonical Passage policy."""

from datetime import UTC, datetime

from core.chat import ChatMessage
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


def test_passage_ids_and_boundaries_are_deterministic() -> None:
    messages = [
        ChatMessage.user("alpha " * 200, timestamp=timestamp(1)),
        ChatMessage.assistant(model="test", content="beta " * 200, timestamp=timestamp(2)),
    ]

    first = build_session_passages(messages, target_chars=700, overlap_chars=100)
    second = build_session_passages(messages, target_chars=700, overlap_chars=100)

    assert first == second
    assert len({passage.passage_id for passage in first}) == len(first)
    assert first[0].start_message_id == messages[0].id
    assert first[-1].end_message_id == messages[-1].id


def test_passages_exclude_tool_messages_by_default() -> None:
    user = ChatMessage.user("user text", timestamp=timestamp(1))
    tool = ChatMessage.tool(
        name="bash",
        content="tool payload",
        tool_call_id="call-1",
        timestamp=timestamp(2),
    )

    passages = build_session_passages([user, tool])

    assert len(passages) == 1
    assert passages[0].text == "user text"
