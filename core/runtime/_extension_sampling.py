"""Extension sampling request preparation, usage accounting and Adapter lifetime."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from core.chat.streaming import stream_model_response
from core.providers.accounts import ConnectionRef
from core.providers.adapter import ProviderAdapter
from core.runtime._workers import _RUNTIME_WORKERS

if TYPE_CHECKING:
    from core.runtime.runtime import Runtime


async def sample_extension(
    runtime: Runtime, context: Any, request: dict[str, Any]
) -> dict[str, Any]:
    from core.chat.model_resolution import resolve_agent_model_target

    agent = await runtime._extension_tool_agent_async(context)
    adapter: ProviderAdapter | None = None

    def prepare() -> tuple[str, str, str]:
        nonlocal adapter
        provider_id, model_id, connection_id = resolve_agent_model_target(runtime, agent)
        adapter = runtime.get_adapter(ConnectionRef(provider_id, connection_id))
        return provider_id, model_id, connection_id

    options = {
        key: request[key]
        for key in ("stop_sequences", "tool_choice")
        if request.get(key) is not None
    }
    owner = context.execution_owner
    recorder = runtime.usage_recorder
    call_id = None
    usage = None
    reported: dict[str, Any] = {}
    outcome = "failed"
    try:
        provider_id, model_id, connection_id = await _RUNTIME_WORKERS.run(prepare)
        assert adapter is not None
        call_id = await recorder.start(
            model=f"{provider_id}/{model_id}",
            kind="extension_sampling",
            connection_id=connection_id,
            agent_id=context.agent_id,
            session_id=context.session_id,
            project_id=context.project_id,
            run_id=context.run_id,
            owner_name=owner.extension if owner else None,
            group_id=owner.group_id if owner else None,
        )
        normalized = await stream_model_response(
            adapter,
            request["messages"],
            model_id=model_id,
            tools=request.get("tools"),
            max_tokens=request["max_tokens"],
            temperature=request.get("temperature")
            if request.get("temperature") is not None
            else agent.temperature,
            on_usage=reported.update,
            **options,
            **adapter.request_context_kwargs(
                agent_id=context.agent_id,
                session_id=context.session_id,
                project_id=context.project_id,
            ),
        )
        usage = normalized.get("usage")
        outcome = "completed"
    except asyncio.CancelledError:
        outcome = "cancelled"
        raise
    finally:
        try:
            if call_id is not None:
                await recorder.finish(
                    call_id, usage if usage is not None else reported or None, status=outcome
                )
        finally:
            if adapter is not None:
                await adapter.aclose()
    return {"model": f"{provider_id}/{model_id}", **normalized}
