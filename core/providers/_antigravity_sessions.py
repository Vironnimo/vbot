"""Short-lived native conversation profiles for an enrolled Antigravity Connection."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import ExitStack, asynccontextmanager
from dataclasses import dataclass

from core.providers._antigravity_transport import NativeConversation
from core.providers._native_agent_wire import Conversation
from core.storage import TemporaryFileManager
from core.utils.workers import BoundedWorkerPool, settle_before_cancelling

_FILES = BoundedWorkerPool(name="antigravity-profiles", max_workers=2)


@dataclass
class Profile:
    stack: ExitStack
    native: NativeConversation
    signature: bytes
    history: tuple[bytes, ...] = ()


class Conversations:
    """Cache completed profiles. An active profile has exactly one writer."""

    def __init__(self, files: TemporaryFileManager | None) -> None:
        self._files = files
        self._idle: OrderedDict[Conversation, Profile] = OrderedDict()
        self._writers: dict[Conversation, object] = {}

    @asynccontextmanager
    async def checkout(
        self, key: Conversation, signature: bytes, history: tuple[bytes, ...]
    ) -> AsyncIterator[Profile]:
        assert self._files is not None
        writer = object()
        self._writers[key] = writer
        profile = self._idle.pop(key, None)
        stack = profile.stack if profile else ExitStack()
        kept = False
        try:
            if profile is not None and (
                signature != profile.signature
                or len(history) <= len(profile.history)
                or history[: len(profile.history)] != profile.history
            ):
                await _FILES.run(stack.close)
                profile = None
            if profile is None:
                root = await _FILES.run(stack.enter_context, self._files.provider_directory())
                profile = Profile(stack, NativeConversation(root), signature)
            yield profile
            if self._writers.get(key) is writer and profile.history:
                self._idle[key] = profile
                kept = True
                # Idle profiles hold files only. Active producers have a separate capacity limit.
                if len(self._idle) > 16:
                    _, oldest = self._idle.popitem(last=False)
                    await settle_before_cancelling(_FILES.run(oldest.stack.close))
        finally:
            if self._writers.get(key) is writer:
                self._writers.pop(key)
            if not kept:
                await settle_before_cancelling(_FILES.run(stack.close))

    def retire(self, sessions: set[tuple[str, str]] | None = None) -> ExitStack:
        """Detach matching profiles before cleanup can suspend or a writer can finish."""
        retired = ExitStack()
        for key in set(self._idle) | set(self._writers):
            if sessions is None or (key.agent_id, key.session_id) in sessions:
                self._writers.pop(key, None)
                if profile := self._idle.pop(key, None):
                    retired.callback(profile.stack.close)
        return retired

    async def close_sessions(self, sessions: set[tuple[str, str]] | None = None) -> None:
        await settle_before_cancelling(_FILES.run(self.retire(sessions).close))
