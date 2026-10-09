"""The shared canonical Passage policy."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from core.chat import ChatMessage
from core.chat.messages import ToolCall
from core.recall.passages import Passage, build_session_passages


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


@pytest.mark.parametrize(
    ("target_chars", "overlap_chars", "expected"),
    [
        (
            1,
            0,
            [
                ("ä", "m1", 0, "m1", 1, "f982e4117b461f8c5b0c1d332b66fbe6"),
                ("\t", "m1", 1, "m1", 2, "2419edbbca76f70a06b9dc891d6be057"),
                ("A", "m1", 2, "m1", 3, "7cede75edb8f50db1da7218da41a27c6"),
                ("β", "m2", 0, "m2", 1, "5c376160f6b9f956826a70bd87ccf6ef"),
                ("B", "m2", 1, "m2", 2, "0602543ac1cc42f19f0bfd2f8fe3f451"),
                ("終", "m3", 0, "m3", 1, "6b384db05a3f7a3fe3b89028ade1c8e0"),
                (" ", "m3", 1, "m3", 2, "866031beb208946886ff7b6c1b51285d"),
                ("D", "m3", 2, "m3", 3, "100dfd13a3bfbb8e5cfc488afd60e856"),
                (" ", "m3", 3, "m3", 4, "16e0893cb4f5f916b28784afc5ac4a25"),
            ],
        ),
        (
            3,
            0,
            [
                ("ä\tA", "m1", 0, "m1", 3, "a6c9d2562d7245538049d36502be1624"),
                ("\n\nβ", "m2", 0, "m2", 1, "5c376160f6b9f956826a70bd87ccf6ef"),
                ("B\n\n", "m2", 1, "m2", 2, "0602543ac1cc42f19f0bfd2f8fe3f451"),
                ("終 D", "m3", 0, "m3", 3, "f2c2bed325b41808736aeaf135b65e2e"),
                (" ", "m3", 3, "m3", 4, "16e0893cb4f5f916b28784afc5ac4a25"),
            ],
        ),
        (
            6,
            2,
            [
                ("ä\tA\n\nβ", "m1", 0, "m2", 1, "59a81c457ca03f56a295762f9e9cdf5e"),
                ("\nβB\n\n終", "m2", 0, "m3", 1, "46f67595c4346fb9493f1f79c303a39c"),
                ("\n終 D ", "m3", 0, "m3", 4, "3ee61f38cd8f3864cdabc667ed548240"),
            ],
        ),
        (30, 0, [("ä\tA\n\nβB\n\n終 D ", "m1", 0, "m3", 4, "a5421a901fea96df70d2cc70229c3dba")]),
    ],
    ids=["separator-only-windows", "separator-edges", "overlapping-boundaries", "whole-session"],
)
def test_passage_ids_and_boundaries_are_deterministic(
    target_chars: int,
    overlap_chars: int,
    expected: list[tuple[str, str, int, str, int, str]],
) -> None:
    messages = [
        replace(ChatMessage.user("ä\tA", timestamp=timestamp(1)), id="m1"),
        replace(ChatMessage.assistant(model="test", content="βB", timestamp=timestamp(2)), id="m2"),
        replace(ChatMessage.user("終 D ", timestamp=timestamp(3)), id="m3"),
    ]
    by_id = {message.id: message for message in messages}
    # Empty and ineligible Messages do not move the source-text boundaries.
    messages.insert(1, ChatMessage.user("", timestamp=timestamp(1)))
    messages.insert(2, ChatMessage.note("hidden", timestamp=timestamp(1)))

    passages = build_session_passages(
        messages, target_chars=target_chars, overlap_chars=overlap_chars
    )

    assert passages == [
        Passage(
            passage_id=passage_id,
            text=text,
            start_message_id=start,
            end_message_id=end,
            start_timestamp=str(by_id[start].timestamp),
            end_timestamp=str(by_id[end].timestamp),
            start_role=by_id[start].role,
            end_role=by_id[end].role,
            start_offset=start_offset,
            end_offset=end_offset,
        )
        for text, start, start_offset, end, end_offset, passage_id in expected
    ]


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
    ids=["tool", "recall-result", "note", "error", "assistant-metadata", "run-summary"],
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
