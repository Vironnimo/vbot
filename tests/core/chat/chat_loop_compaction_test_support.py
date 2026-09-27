"""Shared runtimes, fakes and probes for Chat loop Compaction tests."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from core.chat import ChatLoop, ChatMessage
from core.chat._run_state import (
    RequestBuildInputs,
    _RequestState,
    _RunRequest,
    create_run_execution_context,
)
from core.chat.continuation import ContinuationTracker, recover_continuation
from core.runs import Run
from core.sessions import SessionAddress
from core.tools import ToolRegistry, tool_success
from tests.core.chat.chat_loop_support import (
    StubAdapter,
    StubAgent,
    StubModels,
    StubRuntime,
    StubStorage,
    build_request_messages,
)

JsonObject = dict[str, Any]

WAIT_SECONDS = 10.0

# Automatic Compaction at 80% of the window; tests pair it with a 100-token window.
AUTO_COMPACTION: JsonObject = {
    "auto": True,
    "threshold": 0.8,
    "tail_tokens": 15_000,
    "summary_model": None,
}


def compaction_runtime(
    tmp_path: Path,
    *,
    agent: StubAgent | None = None,
    adapter: StubAdapter | None = None,
    settings: JsonObject | None = None,
    context_window: int | None = 100,
    **kwargs: Any,
) -> Any:
    """Build a Chat runtime for ``openai/gpt-5.2`` with automatic Compaction enabled."""
    kwargs.setdefault("storage", StubStorage({**AUTO_COMPACTION, **(settings or {})}))
    kwargs.setdefault("models", StubModels({("openai", "gpt-5.2"): context_window}))
    return StubRuntime(
        data_dir=tmp_path,
        agent=agent or StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"]),
        adapter=adapter if adapter is not None else StubAdapter([]),
        **kwargs,
    )


def real_compaction_runtime(
    tmp_path: Path, adapter: StubAdapter, policy: JsonObject, **kwargs: Any
) -> Any:
    """Build a runtime whose real Compaction Engine reads fixed prompt fragments."""
    return compaction_runtime(
        tmp_path,
        adapter=adapter,
        storage=CompactionPromptStorage(policy, data_dir=tmp_path),
        context_window=1_000_000,
        **kwargs,
    )


def seed_tail(session: Any, *, model: str = "openai/gpt-5.2") -> ChatMessage:
    """Append one exchange and return a checkpoint that retains it as the Tail."""
    session.append(ChatMessage.user("Tail user"))
    session.append(ChatMessage.assistant(model=model, content="Tail assistant"))
    return ChatMessage.compaction_checkpoint(
        summary="Compacted tail context.",
        projection=session.load()[-2:],
        compacted_token_count=42,
    )


def word_count_tools() -> ToolRegistry:
    tools = ToolRegistry()
    tools.register(
        "word_count",
        "Count words.",
        {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
        lambda _context, arguments: tool_success({"words": len(str(arguments["text"]).split())}),
    )
    return tools


class RecordingCompactionAdapter(StubAdapter):
    """Serve Agent steps via ``send`` and Compaction summaries via ``stream``, in order."""

    def __init__(self, responses: list[Any], *, summaries: list[str]) -> None:
        super().__init__(
            responses,
            stream_responses=[
                [
                    {"type": "content_delta", "text": summary},
                    {"type": "finish", "reason": "stop"},
                ]
                for summary in summaries
            ],
        )
        self.events: list[str] = []

    async def send(self, messages: list[JsonObject], *, model_id: str, **kwargs: Any) -> JsonObject:
        self.events.append("agent")
        return await super().send(messages, model_id=model_id, **kwargs)

    async def stream(self, messages: list[JsonObject], *, model_id: str, **kwargs: Any) -> Any:
        self.events.append("compaction")
        async for delta in super().stream(messages, model_id=model_id, **kwargs):
            yield delta


class CompactionPromptStorage(StubStorage):
    """Serve every Compaction prompt fragment and record which one was read."""

    def __init__(self, compaction_settings: JsonObject, *, data_dir: Path) -> None:
        super().__init__(compaction_settings, data_dir=data_dir)
        self.prompt_fragment_reads: list[str] = []

    def read_prompt_fragment(self, name: str) -> str:
        self.prompt_fragment_reads.append(name)
        assert name in {
            "compaction.md",
            "compaction-manual.md",
            "compaction-continuation.md",
            "compaction-continuation-manual.md",
        }
        return "Summarize the earlier Context and preserve unfinished work."


class CompactOnceService:
    """Compact once, at the second automatic check (the first check after a Model step).

    With ``block=True`` the attempt waits for ``release`` so a test can race it.
    """

    def __init__(self, *, block: bool = False, keep_last: int | None = None) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self._block = block
        self._keep_last = keep_last
        self._checks = 0
        self._attempted = False

    def has_new_compactable_context(self, *_args: Any, **_kwargs: Any) -> bool:
        return True

    def should_auto_compact(self, *_args: Any, **_kwargs: Any) -> bool:
        self._checks += 1
        return self._checks == 2 and not self._attempted

    async def compact(self, messages: list[ChatMessage], **_kwargs: Any) -> ChatMessage:
        self._attempted = True
        self.started.set()
        if self._block:
            await self.release.wait()
        return ChatMessage.compaction_checkpoint(
            summary="Compacted snapshot.",
            projection=messages[-self._keep_last :] if self._keep_last else messages,
            compacted_token_count=20,
        )


async def append_note_while_compacting(
    runtime: Any, service: CompactOnceService, address: SessionAddress, text: str
) -> None:
    """Commit a Session note while the blocked attempt holds its snapshot, then release it."""
    await asyncio.wait_for(service.started.wait(), WAIT_SECONDS)
    try:
        async with asyncio.timeout(WAIT_SECONDS):
            async with runtime.chat_sessions.write_lock(address):
                await runtime.chat_sessions.get(address).add_note_async(text)
    finally:
        service.release.set()


@dataclass(frozen=True)
class AutoCompaction:
    """One automatic Compaction boundary evaluated with a production Run context."""

    run: Run
    request: list[JsonObject]
    rebuilt: list[JsonObject]


async def auto_compact(
    loop: ChatLoop,
    agent: Any,
    session: Any,
    *,
    usage: JsonObject | None,
    request: list[JsonObject] | None = None,
    run_id: str = "run-1",
    continuation_tracker: ContinuationTracker | None = None,
    continuation_reminder: str | None = None,
) -> AutoCompaction:
    """Evaluate one automatic boundary for ``request`` (default: the Session's request)."""
    if request is None:
        request = await build_request_messages(loop, agent, session)
    run = Run(run_id=run_id, agent_id=agent.id, session_id=session.id)
    context = await create_run_execution_context(
        loop._dependencies,
        loop._requests,
        run,
        _RunRequest(content="test"),
        session=session,
        prior_continuation=(await recover_continuation(session) if continuation_reminder else None),
        continuation_reminder=continuation_reminder,
        continuation_tracker=continuation_tracker,
    )
    context.request_state = _RequestState(request, [], (), ())
    if usage is not None:
        context.context_usage.observe(
            usage,
            request,
            adapter=context.primary_target.adapter,
            model_id=context.primary_target.model_id,
            tools=[],
            scope=context.prompt_cache_affinity_id,
        )
    state = await loop._compaction_runs.maybe_auto_compact_state(
        context, context.primary_target, usage
    )
    return AutoCompaction(run=run, request=request, rebuilt=state.messages)


async def run_context(
    loop: ChatLoop, run: Run, session: Any, request: _RunRequest | None = None
) -> Any:
    """Create a production Run context with its first request state built."""
    context = await create_run_execution_context(
        loop._dependencies,
        loop._requests,
        run,
        request or _RunRequest(content="test"),
        session=session,
        prior_continuation=None,
        continuation_reminder=None,
        continuation_tracker=None,
    )
    context.request_state = await loop._requests.build_request_state(
        context.agent,
        session,
        inputs=RequestBuildInputs.from_context(context, context.primary_target),
    )
    return context


async def compact_context(loop: ChatLoop, context: Any) -> Any:
    """Run the automatic boundary for ``context`` with usage above the trigger."""
    return cast(
        Any,
        await loop._compaction_runs.maybe_auto_compact_state(
            context, context.primary_target, {"input_tokens": 90}
        ),
    )
