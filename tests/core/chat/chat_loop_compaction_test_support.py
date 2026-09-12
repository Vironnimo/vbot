"""Shared fixtures and fakes for chat loop compaction behavior tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from core.chat import (
    ChatLoop,
)
from core.chat._run_state import (
    _RequestState,
    _RunRequest,
    create_run_execution_context,
)
from core.chat.continuation import (
    ContinuationTracker,
    recover_continuation,
)
from core.runs import (
    Run,
)
from tests.core.chat.chat_loop_support import (
    StubAdapter,
    StubStorage,
)

JsonObject = dict[str, Any]


class _RealCompactionAdapter(StubAdapter):
    """Exercise real Agent and Compaction requests through one recording adapter."""

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

    async def send(
        self,
        messages: list[JsonObject],
        *,
        model_id: str,
        **kwargs: Any,
    ) -> JsonObject:
        self.events.append("agent")
        return await super().send(messages, model_id=model_id, **kwargs)

    async def stream(
        self,
        messages: list[JsonObject],
        *,
        model_id: str,
        **kwargs: Any,
    ) -> Any:
        self.events.append("compaction")
        async for delta in super().stream(messages, model_id=model_id, **kwargs):
            yield delta

    def normalize_response(
        self,
        response: JsonObject,
        *,
        model_id: str | None = None,
    ) -> JsonObject:
        del model_id
        if "choices" not in response:
            return response
        choices = cast(list[JsonObject], response["choices"])
        message = cast(JsonObject, choices[0]["message"])
        return {"content": message.get("content"), "usage": response.get("usage")}


class _RealCompactionStorage(StubStorage):
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


async def _maybe_auto_compact(
    loop: ChatLoop,
    agent: Any,
    adapter: Any,
    model_id: str,
    session: Any,
    messages: list[JsonObject],
    usage: JsonObject | None,
    *,
    run: Run,
    continuation_tracker: ContinuationTracker | None = None,
    continuation_reminder: str | None = None,
) -> list[JsonObject]:
    """Build the same Run context used by production before probing Compaction."""
    del agent, model_id
    prior_continuation = await recover_continuation(session) if continuation_reminder else None
    context = await create_run_execution_context(
        loop._dependencies,
        loop._requests,
        run,
        _RunRequest(content="test"),
        session=session,
        prior_continuation=prior_continuation,
        continuation_reminder=continuation_reminder,
        continuation_tracker=continuation_tracker,
    )
    assert context.primary_target.adapter is adapter
    context.request_state = _RequestState(messages, [], (), ())
    if usage is not None:
        context.context_usage.observe(
            usage,
            messages,
            adapter=adapter,
            model_id=context.primary_target.model_id,
            tools=[],
            scope=context.prompt_cache_affinity_id,
        )
    state = await loop._compaction_runs.maybe_auto_compact_state(
        context,
        context.primary_target,
        usage,
    )
    return state.messages
