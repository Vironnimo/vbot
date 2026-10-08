"""StreamDraft: the stored copy of streamed output before its Assistant entry persists."""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

from core.chat._stream_draft import StreamDraft
from core.chat.messages import ToolCall


class FakeSession:
    def __init__(self, *, failures: int = 0) -> None:
        self.chunks: list[tuple[str, str]] = []
        self.tool_calls: list[dict[str, Any]] = []
        self.discards = 0
        self.failures = failures
        self.gate = asyncio.Event()
        self.gate.set()
        self.writing = asyncio.Event()

    async def append_stream_draft_async(
        self,
        *,
        model: str,
        reasoning_delta: str,
        content_delta: str,
        tool_calls: list[dict[str, Any]],
    ) -> None:
        self.writing.set()
        await self.gate.wait()
        if self.failures:
            self.failures -= 1
            raise OSError("disk busy")
        self.chunks.append((reasoning_delta, content_delta))
        self.tool_calls.extend(tool_calls)

    async def discard_stream_draft_async(self) -> None:
        self.discards += 1


async def _idle() -> None:
    for _ in range(10):
        await asyncio.sleep(0)


def _draft(session: FakeSession) -> StreamDraft:
    return StreamDraft(cast(Any, session), flush_interval=0.0)


@pytest.mark.asyncio
async def test_a_failed_draft_write_keeps_its_text_in_order_for_the_next() -> None:
    session = FakeSession(failures=1)
    draft = _draft(session)

    draft.record(model="openai/test", reasoning="R1", content="C1")
    await _idle()
    draft.record(model="openai/test", reasoning="", content="C2")
    await _idle()

    assert "".join(reasoning for reasoning, _ in session.chunks) == "R1"
    assert "".join(content for _, content in session.chunks) == "C1C2"
    await draft.discard()
    assert session.discards == 1


@pytest.mark.asyncio
async def test_settle_waits_for_a_running_write_and_drops_unwritten_text() -> None:
    session = FakeSession()
    session.gate.clear()
    draft = _draft(session)

    draft.record(model="openai/test", reasoning="", content="written")
    await session.writing.wait()
    draft.record(model="openai/test", reasoning="", content="unwritten")
    settling = asyncio.create_task(draft.settle())
    await _idle()
    assert not settling.done()

    session.gate.set()
    await settling
    await _idle()

    assert session.chunks == [("", "written")]
    # The caller's Assistant entry deleted the stored draft; nothing remains to discard.
    await draft.discard()
    assert session.discards == 0


@pytest.mark.asyncio
async def test_started_tool_calls_are_written_without_waiting_for_the_interval() -> None:
    session = FakeSession()

    async def never(_seconds: float) -> None:
        await asyncio.Event().wait()

    draft = StreamDraft(cast(Any, session), flush_interval=60.0, sleep=never)
    draft.record(model="openai/test", reasoning="", content="Checking.")
    await _idle()
    assert session.chunks == []

    draft.record_tool_calls(
        model="openai/test",
        tool_calls=[ToolCall(id="call_berlin", name="get_weather", arguments={"city": "Berlin"})],
    )
    await _idle()

    # The pending text goes with the calls, in one chunk.
    assert session.chunks == [("", "Checking.")]
    assert session.tool_calls == [
        {"id": "call_berlin", "name": "get_weather", "arguments": {"city": "Berlin"}}
    ]
    await draft.settle()
