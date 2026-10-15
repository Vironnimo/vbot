"""Runtime collects attachments only when admission and producers are quiescent."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from core.attachments import AttachmentStore
from core.chat import ChatMessage
from core.runtime import Runtime
from core.sessions import ChatSessionManager
from core.utils.config import Config


@pytest.mark.asyncio
@pytest.mark.parametrize("closing", ["clean", "failed", "synchronous"])
async def test_shutdown_only_collects_after_a_successful_async_drain(
    config: Config, tmp_path: Path, closing: str
) -> None:
    runtime = Runtime(config)
    sessions = ChatSessionManager(tmp_path)
    attachments = AttachmentStore(tmp_path)
    record = attachments.store("file.txt", b"bytes")
    session = sessions.create("coder")
    session.append(ChatMessage.user(f"Path: {record.file_path}"))
    sessions.delete(session.address)
    runtime._chat_sessions = sessions  # noqa: SLF001 - lifecycle-owned resources.
    runtime._attachment_store = attachments  # noqa: SLF001
    if closing == "failed":

        async def fail() -> None:
            raise RuntimeError("producer failed to drain")

        runtime._speech = SimpleNamespace(close=lambda: None, aclose=fail)  # type: ignore[assignment]  # noqa: SLF001
        with pytest.raises(RuntimeError):
            await runtime.aclose()
    elif closing == "clean":
        await runtime.aclose()
    else:
        runtime.stop()
    assert Path(record.file_path).exists() is (closing != "clean")
