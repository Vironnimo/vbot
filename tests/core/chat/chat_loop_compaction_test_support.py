"""Shared runtimes, fakes and probes for Chat loop Compaction tests."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast, override

from core.chat import ChatLoop, ChatMessage
from core.chat._run_state import (
    RequestBuildInputs,
    _RequestState,
    _RunRequest,
    create_run_execution_context,
)
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

# Every Compaction prompt fragment of CompactionPromptStorage.
COMPACTION_INSTRUCTION = "Summarize the earlier Context and preserve unfinished work."

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
    """Stream Agent steps from ``responses`` and Compaction summaries from ``summaries``.

    A request that ends with the Compaction instruction of
    :class:`CompactionPromptStorage` is a Compaction request, recorded in
    ``stream_requests``; every other request is an Agent step, recorded in
    ``requests``. ``events`` names them in request order.
    """

    def __init__(self, responses: list[Any], *, summaries: list[str]) -> None:
        super().__init__(responses)
        self._summaries = list(summaries)
        self.events: list[str] = []

    @override
    async def send(self, messages: list[JsonObject], *, model_id: str, **kwargs: Any) -> JsonObject:
        self.events.append("agent")
        return await super().send(messages, model_id=model_id, **kwargs)

    @override
    async def stream(self, messages: list[JsonObject], *, model_id: str, **kwargs: Any) -> Any:
        if COMPACTION_INSTRUCTION not in json.dumps(messages[-1]):
            async for delta in super().stream(messages, model_id=model_id, **kwargs):
                yield delta
            return
        self.events.append("compaction")
        self.stream_requests.append(
            {"messages": deepcopy(messages), "model_id": model_id, "kwargs": deepcopy(kwargs)}
        )
        if not self._summaries:
            raise AssertionError("unexpected Compaction request")
        yield {"type": "content_delta", "text": self._summaries.pop(0)}
        yield {"type": "finish", "reason": "stop"}


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
        return COMPACTION_INSTRUCTION


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


async def append_while_compacting(
    runtime: Any, service: CompactOnceService, address: SessionAddress, message: ChatMessage
) -> None:
    """Commit *message* while the blocked attempt holds its snapshot, then release the attempt."""
    await asyncio.wait_for(service.started.wait(), WAIT_SECONDS)
    try:
        async with asyncio.timeout(WAIT_SECONDS):
            async with runtime.chat_sessions.write_lock(address):
                await runtime.chat_sessions.get(address).append_async(message)
    finally:
        service.release.set()


async def append_note_while_compacting(
    runtime: Any, service: CompactOnceService, address: SessionAddress, text: str
) -> None:
    await append_while_compacting(runtime, service, address, ChatMessage.note(text))


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
    compaction_requested: bool = False,
) -> AutoCompaction:
    """Evaluate one boundary for ``request`` (default: the Session's request).

    ``compaction_requested`` evaluates it with a pending user Compaction request.
    """
    if request is None:
        request = await build_request_messages(loop, agent, session)
    run = Run(run_id=run_id, agent_id=agent.id, session_id=session.id)
    if compaction_requested:
        run.set_compaction_state("pending")
    context = await create_run_execution_context(
        loop._dependencies,
        loop._requests,
        run,
        _RunRequest(content="test"),
        session=session,
    )
    context.request_state = _RequestState(request, [], (), ())
    if usage is not None:
        context.context_usage.observe(
            usage,
            request,
            target=context.primary_target,
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
    )
    context.request_state = await loop._requests.build_request_state(
        context.agent,
        session,
        inputs=RequestBuildInputs.from_context(context, context.primary_target),
    )
    return context


async def compact_context(loop: ChatLoop, context: Any, target: Any | None = None) -> Any:
    """Run the automatic boundary for ``context`` on *target* (default: the primary route)."""
    return cast(
        Any,
        await loop._compaction_runs.maybe_auto_compact_state(
            context, target or context.primary_target, {"input_tokens": 90}
        ),
    )
