"""StreamDraft: the stored copy of streamed output before its Assistant entry persists."""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

from core.chat._stream_draft import StreamDraft


class FakeSession:
    def __init__(self, *, failures: int = 0) -> None:
        self.chunks: list[tuple[str, str]] = []
        self.discards = 0
        self.failures = failures
        self.gate = asyncio.Event()
        self.gate.set()
        self.writing = asyncio.Event()

    async def append_stream_draft_async(
        self, *, model: str, reasoning_delta: str, content_delta: str
    ) -> None:
        self.writing.set()
        await self.gate.wait()
        if self.failures:
            self.failures -= 1
            raise OSError("disk busy")
        self.chunks.append((reasoning_delta, content_delta))

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
