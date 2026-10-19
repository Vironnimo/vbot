"""Run-local Model fallback: when a route switches, what the new route receives, and cleanup."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import random
from pathlib import Path
from typing import Any, cast, override

import pytest
from PIL import Image

from core.attachments import AttachmentStore
from core.chat._request_builder import RequestBuilder
from core.chat._request_history import _restore_in_run_tool_result_content
from core.chat._tool_epoch import tool_change_from_note
from core.chat.messages import ModelFallback
from core.model_tasks import TASK_IMAGE_UNDERSTANDING
from core.providers.adapter import TOOL_RESULT_CONTENT_BLOCKS_FIELD
from core.providers.errors import (
    ProviderAuthError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUsageLimitError,
)
from core.runs import (
    ERROR_MESSAGE_PERSISTED_EVENT,
    MODEL_FALLBACK_ACTIVATED_EVENT,
    STREAM_ATTEMPT_RESTARTED_EVENT,
    RunCancelledError,
    RunStatus,
)
from core.tools import (
    ANALYZE_IMAGE_TOOL_NAME,
    ToolRegistry,
    tool_failure,
    tool_success,
)
from core.tools.file_state import FileReadState
from core.tools.load_tools import register_load_tools_tool
from core.tools.read import register_read_tool
from core.utils.errors import ConfigError, ProviderError
from tests.core.chat.chat_loop_support import (
    ClosingStubAdapter,
    StubAdapter,
    StubAgent,
    StubModels,
    StubRuntime,
    build_chat_loop,
    event_types,
    history,
    last_run,
    persisted_roles,
)

JsonObject = dict[str, Any]

PRIMARY = "openai/gpt-5.2"
FALLBACK = "anthropic/claude-sonnet-4::api-key"


def _fallback_runtime(
    tmp_path: Path,
    primary: StubAdapter,
    fallback: StubAdapter | None = None,
    *,
    fallback_models: list[str] | None = None,
    model: str = PRIMARY,
    allowed_tools: list[str] | None = None,
    tool_loading: dict[str, Any] | None = None,
    **runtime_options: Any,
) -> Any:
    """Primary route on openai:api-key; every fallback candidate on anthropic:api-key."""
    adapters = {"openai:api-key": primary}
    if fallback is not None:
        adapters["anthropic:api-key"] = fallback
    agent = StubAgent(
        id="coder",
        model=model,
        fallback_models=[FALLBACK] if fallback_models is None else fallback_models,
        allowed_tools=["*"] if allowed_tools is None else allowed_tools,
        tool_loading=tool_loading,
    )
    return StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=primary,
        adapters_by_connection=adapters,
        provider_ids={"openai", "anthropic"},
        **runtime_options,
    )


async def _fallback_events(runtime: StubRuntime, run: Any) -> list[tuple[str, str]]:
    return [
        (event.payload["from_model"], event.payload["to_model"])
        for event in await runtime.timelines.events(run)
        if event.type == MODEL_FALLBACK_ACTIVATED_EVENT
    ]


def _model_not_found() -> ProviderError:
    retired = ProviderError("Provider error: model not found", retryable=False)
    retired.status_code = 404
    return retired


def _probe_tools(handler: Any) -> ToolRegistry:
    tools = ToolRegistry()
    tools.register("probe", "Probe", {"type": "object"}, handler)
    return tools


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failures", "cools_down"),
    [
        # A rate limit skips the remaining same-Model restarts on a dead quota,
        # and new Runs pass over the route for a while.
        ([ProviderRateLimitError("primary rate limited")], True),
        # Each hang costs minutes, so two of them hand over.
        ([ProviderTimeoutError("primary hung")] * 2, True),
        # A retired Model fails at once; there is no outage to wait out.
        ([_model_not_found()], False),
    ],
    ids=["rate-limit", "hangs", "model-not-found"],
)
async def test_route_scoped_failure_switches_to_the_fallback_for_this_run(
    tmp_path: Path, failures: list[ProviderError], cools_down: bool
) -> None:
    primary = StubAdapter(list(failures))
    fallback = StubAdapter([{"content": "Recovered", "tool_calls": None}])
    runtime = _fallback_runtime(tmp_path, primary, fallback)

    assistant = await build_chat_loop(runtime).send("coder", "Hi", session_id="session-one")

    run = last_run(runtime)
    messages = history(runtime)
    primary_requests = primary.requests
    fallback_requests = fallback.requests
    assert assistant.content == "Recovered"
    assert persisted_roles(messages) == ["user", "note", "assistant"]
    assert (
        messages[1].content == f"Model {PRIMARY} unavailable. Switched to {FALLBACK} for this run."
    )
    # The note records the switch for the Run's display notice; only its text reaches the Model.
    fallback_record = messages[1].model_fallback
    assert fallback_record is not None
    assert (fallback_record.from_model, fallback_record.to_model) == (PRIMARY, FALLBACK)
    assert (fallback_record.retry_at is not None) is cools_down
    assert "model_fallback" not in json.dumps(fallback_requests[0]["messages"])
    # Usage and cost belong to the Model that actually answered.
    assert messages[2].model == FALLBACK
    assert await _fallback_events(runtime, run) == [(PRIMARY, FALLBACK)]
    assert run.iteration_count == 1
    assert messages[-1].iteration_count == 1
    assert run.events[-1].payload["iteration_count"] == 1
    assert [request["model_id"] for request in primary_requests] == ["gpt-5.2"] * len(failures)
    assert [request["model_id"] for request in fallback_requests] == ["claude-sonnet-4"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fallback_models",
    [[], ["ghost-provider/missing"], [f"{PRIMARY}::api-key"], [f"{PRIMARY}::api-key:default"]],
    ids=["none", "unresolvable", "same-route", "same-account"],
)
async def test_rate_limit_without_a_fallback_fails_the_run_after_same_model_recovery(
    tmp_path: Path, recovery_waits: list[float], fallback_models: list[str]
) -> None:
    adapter = StubAdapter([ProviderRateLimitError("too many requests")] * 9)
    runtime = _fallback_runtime(tmp_path, adapter, fallback_models=fallback_models)

    with pytest.raises(ProviderRateLimitError, match="too many requests"):
        await build_chat_loop(runtime).send("coder", "Hi", session_id="session-one")

    run = last_run(runtime)
    messages = history(runtime)
    assert len(adapter.requests) == 9
    assert run.status == RunStatus.FAILED
    assert persisted_roles(messages) == ["user", "error"]
    assert (run.iteration_count, messages[-1].iteration_count) == (0, 0)
    assert run.events[-1].payload["iteration_count"] == 0
    assert (messages[1].error_kind, messages[1].content) == ("rate_limit", "too many requests")
    assert await event_types(runtime, run) == [
        "run_started",
        "user_message_persisted",
        *[STREAM_ATTEMPT_RESTARTED_EVENT] * 8,
        ERROR_MESSAGE_PERSISTED_EVENT,
        "run_failed",
    ]
    persisted_error = next(
        e for e in await runtime.timelines.events(run) if e.type == ERROR_MESSAGE_PERSISTED_EVENT
    )
    assert persisted_error.payload["message"]["role"] == "error"
    assert persisted_error.payload["message"]["error_kind"] == "rate_limit"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        ProviderAuthError("invalid credential"),
        ProviderError("Provider error: insufficient_quota", retryable=False),
        ProviderUsageLimitError("Subscription usage limit reached; it resets at 18:00 UTC"),
    ],
    ids=["auth", "billing", "usage-limit"],
)
async def test_account_scoped_failure_advances_only_to_another_connection(
    tmp_path: Path, failure: ProviderError
) -> None:
    same_connection = "openai/gpt-5.2-mini::api-key"
    primary = StubAdapter([failure])
    fallback = StubAdapter([{"content": "Recovered", "tool_calls": None}])
    runtime = _fallback_runtime(
        tmp_path, primary, fallback, fallback_models=[same_connection, FALLBACK]
    )

    assistant = await build_chat_loop(runtime).send("coder", "Hi", session_id="session-one")

    # The candidate on the failed account is never tried; the other account answers.
    assert assistant.content == "Recovered"
    assert [request["model_id"] for request in primary.requests] == ["gpt-5.2"]
    assert await _fallback_events(runtime, last_run(runtime)) == [(PRIMARY, FALLBACK)]


@pytest.mark.asyncio
async def test_failure_no_route_can_fix_never_advances_the_fallback_chain(tmp_path: Path) -> None:
    primary = StubAdapter([ProviderError("context_length_exceeded", retryable=False)])
    fallback = StubAdapter([{"content": "Should not be used", "tool_calls": None}])
    runtime = _fallback_runtime(tmp_path, primary, fallback)

    with pytest.raises(ProviderError, match="context_length_exceeded"):
        await build_chat_loop(runtime).send("coder", "Hi", session_id="session-one")

    run = last_run(runtime)
    assert run.status == RunStatus.FAILED
    assert persisted_roles(history(runtime)) == ["user", "error"]
    assert await _fallback_events(runtime, run) == []
    assert fallback.requests == []


@pytest.mark.asyncio
async def test_fallback_serves_new_runs_until_the_failed_route_cooled_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [1_000_000.0]
    monkeypatch.setattr("core.chat.recovery._wall_clock", lambda: now[0])
    primary = StubAdapter(
        [
            ProviderRateLimitError("primary rate limited"),
            ProviderRateLimitError("primary still limited"),
            {"content": "Primary turn 4", "tool_calls": None},
        ]
    )
    fallback = StubAdapter(
        [
            {
                "content": None,
                "tool_calls": [{"id": "call_1", "name": "probe", "arguments": {"value": "x"}}],
            },
            {"content": "Fallback turn 1", "tool_calls": None},
            {"content": "Fallback turn 2", "tool_calls": None},
            {"content": "Fallback turn 3", "tool_calls": None},
        ]
    )
    tools = _probe_tools(lambda _context, arguments: tool_success({"value": arguments["value"]}))
    runtime = _fallback_runtime(tmp_path, primary, fallback, allowed_tools=["probe"], tools=tools)
    chat_loop = build_chat_loop(runtime)

    async def turn(text: str) -> tuple[Any, list[ModelFallback]]:
        reply = await chat_loop.send("coder", text, session_id="session-one")
        notes = [
            message.model_fallback
            for message in history(runtime)
            if message.model_fallback is not None and message.run_id == last_run(runtime).id
        ]
        return reply, notes

    # The fallback serves the rest of the Run, Tool round included.
    first, first_notes = await turn("turn 1")
    # A new Run starts on the fallback while the primary route cools down.
    second, second_notes = await turn("turn 2")
    # After the pause one probe fails again: the next pause is twice as long.
    now[0] += 15 * 60
    third, third_notes = await turn("turn 3")
    now[0] += 30 * 60
    fourth, fourth_notes = await turn("turn 4")

    assert [(reply.content, reply.model) for reply in (first, second, third, fourth)] == [
        ("Fallback turn 1", FALLBACK),
        ("Fallback turn 2", FALLBACK),
        ("Fallback turn 3", FALLBACK),
        ("Primary turn 4", PRIMARY),
    ]
    assert [request["model_id"] for request in primary.requests] == ["gpt-5.2"] * 3
    assert [len(notes) for notes in (first_notes, second_notes, third_notes)] == [1, 1, 1]
    assert fourth_notes == []
    retry_at = [notes[0].retry_at for notes in (first_notes, second_notes, third_notes)]
    assert retry_at == [
        "1970-01-12T14:01:40.000000Z",
        "1970-01-12T14:01:40.000000Z",
        "1970-01-12T14:31:40.000000Z",
    ]


class _RecordingAdapter(StubAdapter):
    """Stub adapter recording the wire model id of every send."""

    def __init__(self, responses: list[Any]) -> None:
        super().__init__(responses)
        self.served_model_ids: list[str] = []

    @override
    async def send(self, messages: list[JsonObject], *, model_id: str, **kwargs: Any) -> JsonObject:
        self.served_model_ids.append(model_id)
        return await super().send(messages, model_id=model_id, **kwargs)


@pytest.mark.asyncio
async def test_fallback_chain_skips_unresolvable_candidates_and_cascades_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    primary = StubAdapter([ProviderRateLimitError("primary rate limited")])
    # Both resolvable candidates share anthropic:api-key, so one adapter serves them in order.
    candidates = _RecordingAdapter(
        [
            ProviderRateLimitError("first resort limited"),
            {"content": "Recovered", "tool_calls": None},
        ]
    )
    chain = [
        "ghost-provider/no-such-model",
        "anthropic/missing-account::api-key:removed",
        "anthropic/first-resort::api-key",
        "anthropic/last-resort::api-key",
    ]
    runtime = _fallback_runtime(tmp_path, primary, candidates, fallback_models=chain)
    credentials = runtime.provider_credentials
    usable = credentials.is_usable
    resolve_account = credentials.resolve_account_id

    def is_usable(provider_id: str, connection_id: str) -> bool:
        return connection_id == "anthropic:api-key:removed" or usable(provider_id, connection_id)

    def account(provider_id: str, connection_id: str, account_id: str | None = None) -> str:
        if account_id == "removed":
            raise ConfigError("Account removed after the usability check")
        return cast(str, resolve_account(provider_id, connection_id, account_id))

    monkeypatch.setattr(credentials, "is_usable", is_usable)
    monkeypatch.setattr(credentials, "resolve_account_id", account)

    assistant = await build_chat_loop(runtime).send("coder", "Hi", session_id="session-one")

    messages = history(runtime)
    assert assistant.content == "Recovered"
    assert await _fallback_events(runtime, last_run(runtime)) == [
        (PRIMARY, "anthropic/first-resort::api-key"),
        ("anthropic/first-resort::api-key", "anthropic/last-resort::api-key"),
    ]
    assert persisted_roles(messages) == ["user", "note", "note", "assistant"]
    assert "Switched to anthropic/first-resort::api-key" in str(messages[1].content)
    assert "Switched to anthropic/last-resort::api-key" in str(messages[2].content)
    assert candidates.served_model_ids == ["first-resort", "last-resort"]


@pytest.mark.asyncio
@pytest.mark.parametrize("successor", [False, True], ids=["no-route-left", "another-route"])
async def test_candidate_whose_adapter_cannot_be_built_is_skipped(
    tmp_path: Path, successor: bool
) -> None:
    last_resort = "openai/last-resort::api-key"
    failure = ProviderRateLimitError("primary rate limited")
    primary = StubAdapter(
        [failure, {"content": "Recovered", "tool_calls": None}] if successor else [failure] * 9
    )
    runtime = _fallback_runtime(
        tmp_path,
        primary,
        fallback_models=[FALLBACK, *([last_resort] if successor else [])],
        raise_on_connection={"anthropic:api-key": ConfigError("bad credential")},
    )

    operation = build_chat_loop(runtime).send("coder", "Hi", session_id="session-one")
    if successor:
        assert (await operation).content == "Recovered"
        assert [request["model_id"] for request in primary.requests] == ["gpt-5.2", "last-resort"]
    else:
        # A broken escape route never removes the primary's normal retries.
        with pytest.raises(ProviderRateLimitError, match="primary rate limited"):
            await operation
        assert len(primary.requests) == 9

    run = last_run(runtime)
    assert run.status == (RunStatus.COMPLETED if successor else RunStatus.FAILED)
    assert await _fallback_events(runtime, run) == ([(PRIMARY, last_resort)] if successor else [])
    assert persisted_roles(history(runtime)) == (
        ["user", "note", "assistant"] if successor else ["user", "error"]
    )


@pytest.mark.asyncio
async def test_fallback_failure_persists_the_fallback_error(tmp_path: Path) -> None:
    primary = StubAdapter([ProviderRateLimitError("primary rate limited")])
    # The last route of the chain keeps the full same-Model recovery.
    fallback = StubAdapter([ProviderRateLimitError("fallback rate limited")] * 9)
    runtime = _fallback_runtime(tmp_path, primary, fallback)

    with pytest.raises(ProviderRateLimitError, match="fallback rate limited"):
        await build_chat_loop(runtime).send("coder", "Hi", session_id="session-one")

    run = last_run(runtime)
    messages = history(runtime)
    assert run.status == RunStatus.FAILED
    assert (await event_types(runtime, run)).count(ERROR_MESSAGE_PERSISTED_EVENT) == 1
    assert await _fallback_events(runtime, run) == [(PRIMARY, FALLBACK)]
    assert persisted_roles(messages) == ["user", "note", "error"]
    assert messages[-2].error_kind == "rate_limit"


@pytest.mark.asyncio
async def test_fallback_request_strips_primary_provider_reasoning_meta(tmp_path: Path) -> None:
    primary = StubAdapter(
        [
            {
                "content": None,
                "reasoning": "Primary readable reasoning",
                "reasoning_meta": {"reasoning_details": [{"type": "primary-opaque"}]},
                "tool_calls": [{"id": "call_1", "name": "probe", "arguments": {"value": "x"}}],
            },
            ProviderRateLimitError("primary rate limited"),
        ]
    )
    fallback = StubAdapter(
        [
            {
                "content": "Done",
                "reasoning": "Fallback reasoning",
                "reasoning_meta": {"content_blocks": [{"type": "thinking", "signature": "fb"}]},
                "tool_calls": None,
            }
        ]
    )
    tools = _probe_tools(lambda _context, arguments: tool_success({"value": arguments["value"]}))
    runtime = _fallback_runtime(tmp_path, primary, fallback, allowed_tools=["probe"], tools=tools)

    assistant = await build_chat_loop(runtime).send("coder", "Hi", session_id="session-one")

    assert assistant.content == "Done"
    assert assistant.reasoning_scope == FALLBACK
    # The primary's own Tool-continuation request still round-trips its meta.
    assert any(
        "reasoning_meta" in message
        for message in primary.requests[1]["messages"]
        if message.get("role") == "assistant"
    )
    # The fallback Provider never sees the primary's reasoning fields.
    fallback_messages = fallback.requests[0]["messages"]
    fallback_assistants = [m for m in fallback_messages if m.get("role") == "assistant"]
    assert fallback_assistants
    assert all("reasoning" not in m and "reasoning_meta" not in m for m in fallback_assistants)
    assert "primary-opaque" not in str(fallback_messages)
    # The completed Tool turn's readable work survives the route change only as
    # explicitly provider-neutral context, after its Tool Result.
    portable_notes = [
        index
        for index, message in enumerate(fallback_messages)
        if message.get("role") == "user"
        and "Primary readable reasoning" in message.get("content", "")
    ]
    tool_result_index = next(i for i, m in enumerate(fallback_messages) if m.get("role") == "tool")
    assert len(portable_notes) == 1
    assert portable_notes[0] > tool_result_index


@pytest.mark.asyncio
async def test_fallback_keeps_the_consumed_tool_round_budget(tmp_path: Path) -> None:
    calls: list[str] = []

    def record(context: Any, _arguments: Any) -> JsonObject:
        calls.append(context.tool_call_id)
        return tool_success({})

    primary = StubAdapter(
        [
            {"tool_calls": [{"id": "first", "name": "probe", "arguments": {}}]},
            ProviderRateLimitError("switch route"),
        ]
    )
    fallback = StubAdapter(
        [
            {"tool_calls": [{"id": "second", "name": "probe", "arguments": {}}]},
            {"content": "done", "tool_calls": None},
        ]
    )
    runtime = _fallback_runtime(
        tmp_path,
        primary,
        fallback,
        model="openai/primary",
        fallback_models=["anthropic/fallback::api-key"],
        allowed_tools=["probe"],
        tools=_probe_tools(record),
    )

    await build_chat_loop(runtime, max_tool_iterations=1).send("coder", "Work", session_id="s1")

    assert calls == ["first"]
    rejected = next(
        message
        for message in history(runtime, "s1")
        if message.role == "tool" and message.tool_call_id == "second"
    )
    assert json.loads(str(rejected.content))["error"]["code"] == "tool_iteration_limit"
    # Finalization keeps the pinned Tools on the fallback route too.
    assert fallback.requests[-1]["kwargs"]["tools"] == primary.requests[0]["kwargs"]["tools"]


@pytest.mark.asyncio
async def test_identical_tool_failures_share_the_circuit_breaker_across_fallback(
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    def fail(context: Any, _arguments: Any) -> JsonObject:
        calls.append(context.tool_call_id)
        return tool_failure("unavailable", "still unavailable")

    primary_responses: list[Any] = [
        {"tool_calls": [{"id": f"call-{index}", "name": "probe", "arguments": {}}]}
        for index in range(7)
    ]
    primary = StubAdapter([*primary_responses, ProviderRateLimitError("switch route")])
    fallback = StubAdapter(
        [
            {"tool_calls": [{"id": "last", "name": "probe", "arguments": {}}]},
            {"content": "Cannot complete", "tool_calls": None},
        ]
    )
    runtime = _fallback_runtime(
        tmp_path,
        primary,
        fallback,
        model="openai/primary",
        fallback_models=["anthropic/fallback::api-key"],
        allowed_tools=["probe"],
        tools=_probe_tools(fail),
    )

    await build_chat_loop(runtime).send("coder", "Work", session_id="s1")

    assert len(calls) == 8
    assert any(
        message.role == "note" and "Tool execution is disabled" in str(message.content)
        for message in history(runtime, "s1")
    )
    assert fallback.requests[-1]["kwargs"]["tools"] == primary.requests[0]["kwargs"]["tools"]


@pytest.mark.asyncio
@pytest.mark.parametrize("on_demand", [False, True], ids=["listed-tools", "on-demand-tools"])
async def test_fallback_lists_every_tool_the_model_may_call_without_new_notes(
    tmp_path: Path, on_demand: bool
) -> None:
    # The fallback route's cache is cold anyway, so it lists every Tool the Model may
    # call, On-demand Tools included, and a load changes nothing in that list; it
    # neither re-pins nor re-applies the route gates of its own Model.
    tools = ToolRegistry()
    tools.register(
        ANALYZE_IMAGE_TOOL_NAME,
        "Analyze images.",
        {"type": "object"},
        lambda _context, _arguments: tool_success({"analysis": "ok"}),
    )
    if on_demand:
        register_load_tools_tool(tools)
        tools.register(
            "lazy",
            "Lazy Tool.",
            {"type": "object"},
            lambda _context, _arguments: tool_success({}),
            summary="Lazy Tool.",
        )
    primary = StubAdapter(
        [{"content": "First", "tool_calls": None}, ProviderRateLimitError("rate limited")]
    )
    fallback = StubAdapter(
        [{"content": "Recovered", "tool_calls": None}],
        wire_media_types=frozenset({"image/png"}),
    )
    runtime = _fallback_runtime(
        tmp_path,
        primary,
        fallback,
        tools=tools,
        models=StubModels(
            {("openai", "gpt-5.2"): 128_000, ("anthropic", "claude-sonnet-4"): 128_000},
            input_modalities={("anthropic", "claude-sonnet-4"): ("text", "image")},
        ),
        available_task_models={TASK_IMAGE_UNDERSTANDING},
        tool_loading=(
            {"on_demand": True, "always_loaded": [ANALYZE_IMAGE_TOOL_NAME]} if on_demand else None
        ),
    )
    loop = build_chat_loop(runtime)

    await loop.send("coder", "First", session_id="session-one")
    tools.register(
        "extra",
        "Extra Tool.",
        {"type": "object"},
        lambda _context, _arguments: tool_success({}),
    )
    await loop.send("coder", "Second", session_id="session-one")

    pinned = primary.requests[0]["kwargs"]["tools"]
    extra = {"name": "extra", "description": "Extra Tool.", "parameters": {"type": "object"}}
    assert ANALYZE_IMAGE_TOOL_NAME in {tool["name"] for tool in pinned}
    assert primary.requests[1]["kwargs"]["tools"] == pinned
    if on_demand:
        lazy = {"name": "lazy", "description": "Lazy Tool.", "parameters": {"type": "object"}}
        assert [tool["name"] for tool in pinned] == [ANALYZE_IMAGE_TOOL_NAME, "load_tools"]
        listed = fallback.requests[0]["kwargs"]["tools"]
        assert listed[: len(pinned)] == pinned
        assert [tool["name"] for tool in listed[len(pinned) :]] == ["get_weather", "lazy", "extra"]
        assert lazy in listed and extra in listed
    else:
        assert fallback.requests[0]["kwargs"]["tools"] == [*pinned, extra]
    notes = [message for message in history(runtime) if message.role == "note"]
    assert [change.tool for m in notes if (change := tool_change_from_note(m))] == ["extra"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("vision", "wire_type", "byte_limit"),
    [
        (True, "image/png", None),
        (True, "image/jpeg", None),
        (False, "image/png", None),
        (True, "image/png", 512),
        (True, "image/jpeg", 512),
    ],
)
async def test_fallback_preserves_local_read_pixels_without_disk_copies(
    tmp_path: Path, vision: bool, wire_type: str, byte_limit: int | None
) -> None:
    source = tmp_path / "original.png"
    encoded = io.BytesIO()
    Image.frombytes("RGB", (48, 24), random.Random(4).randbytes(48 * 24 * 3)).save(
        encoded, format="PNG"
    )
    pixels = encoded.getvalue()
    source.write_bytes(pixels)

    class DeletingAdapter(StubAdapter):
        @override
        async def send(self, *args: Any, **kwargs: Any) -> Any:
            if self.requests:
                source.unlink()
            return await super().send(*args, **kwargs)

    class LimitedAdapter(StubAdapter):
        @override
        def image_size_limit(self, model_id: str) -> int | None:
            return byte_limit

    primary = DeletingAdapter(
        [
            {
                "content": None,
                "tool_calls": [
                    {"id": "read-image", "name": "read", "arguments": {"path": str(source)}}
                ],
            },
            ProviderRateLimitError("switch target"),
        ],
        wire_media_types=frozenset({"image/png"}),
    )
    fallback = LimitedAdapter(
        [{"content": "done", "tool_calls": None}], wire_media_types=frozenset({wire_type})
    )
    tools = ToolRegistry()
    register_read_tool(
        tools,
        attachment_store=AttachmentStore(tmp_path),
        speech_service=None,
        file_state=FileReadState(),
        speech_max_size_bytes=1024,
    )
    runtime = _fallback_runtime(
        tmp_path,
        primary,
        fallback,
        model="openai/primary",
        fallback_models=["anthropic/fallback::api-key"],
        allowed_tools=["read"],
        tools=tools,
        models=StubModels(
            {("openai", "primary"): 128_000, ("anthropic", "fallback"): 128_000},
            input_modalities={
                ("openai", "primary"): ("text", "image"),
                ("anthropic", "fallback"): ("text", "image") if vision else ("text",),
            },
        ),
    )

    await build_chat_loop(runtime).send("coder", "inspect", session_id="s1")

    parts = [
        part
        for message in fallback.requests[0]["messages"]
        for part in message.get(TOOL_RESULT_CONTENT_BLOCKS_FIELD, [])
    ]
    native = [part for part in parts if part.get("type") == "media"]
    if vision:
        assert len(native) == 1
        assert native[0]["media_type"] == wire_type
        delivered = base64.b64decode(native[0]["base64"])
        if byte_limit is not None:
            assert len(delivered) <= byte_limit
            assert any("destination limits" in part.get("text", "") for part in parts)
        elif wire_type == "image/png":
            assert delivered == pixels
        with Image.open(io.BytesIO(delivered)) as image:
            image.load()
    else:
        assert native == []
    assert any(source.as_posix() in part.get("text", "") for part in parts)
    assert not list((tmp_path / "artifacts" / "attachments").rglob("*"))
    persisted = history(runtime, "s1")
    assert "base64" not in json.dumps([message.to_dict() for message in persisted])


@pytest.mark.asyncio
async def test_fallback_prepares_multiple_retained_images_in_original_order() -> None:
    colors = [(255, 0, 0), (0, 0, 255)]
    content: list[JsonObject] = []
    for index, color in enumerate(colors):
        encoded = io.BytesIO()
        Image.new("RGB", (16, 12), color).save(encoded, format="PNG")
        content.extend(
            [
                {
                    "type": "media",
                    "media_type": "image/png",
                    "base64": base64.b64encode(encoded.getvalue()).decode("ascii"),
                },
                {"type": "text", "text": f"[Path: /missing/original-{index}.png]"},
            ]
        )
    live = [
        {
            "id": "msg_images",
            "role": "tool",
            "tool_call_id": "images",
            "content": "loaded",
            TOOL_RESULT_CONTENT_BLOCKS_FIELD: content,
        }
    ]
    rebuilt = [{"id": "msg_images", "role": "tool", "tool_call_id": "images", "content": "loaded"}]

    restored = await _restore_in_run_tool_result_content(
        rebuilt,
        live,
        input_modalities=frozenset({"image"}),
        wire_media_types=frozenset({"image/jpeg"}),
        max_image_bytes=512,
    )

    blocks = restored[0][TOOL_RESULT_CONTENT_BLOCKS_FIELD]
    assert len(blocks) == 6
    for index, color in enumerate(colors):
        with Image.open(io.BytesIO(base64.b64decode(blocks[index * 3]["base64"]))) as image:
            pixel = cast(tuple[int, ...], image.getpixel((0, 0)))
        assert all(abs(a - b) <= 2 for a, b in zip(pixel, color, strict=True))
        assert "lossy compression" in blocks[index * 3 + 1]["text"]
        assert f"original-{index}.png" in blocks[index * 3 + 2]["text"]
    # The live Tool Result keeps its original blocks.
    assert len(content) == 4
    assert content[0]["media_type"] == "image/png"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    ["answer", "provider-error", "context-preparation-failure", "cancel", "cancel-during-close"],
)
async def test_the_run_adapter_is_closed_whatever_the_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    response: Any = (
        ProviderError("provider failed", retryable=False)
        if outcome == "provider-error"
        else {"content": "Hello", "tool_calls": None}
    )
    adapter = ClosingStubAdapter([response])
    fallback = ClosingStubAdapter([])
    fallback_closes = 0
    started = asyncio.Event()
    release_close = asyncio.Event()
    acquired_connections: list[str] = []
    runtime = _fallback_runtime(
        tmp_path, adapter, fallback, fallback_models=[FALLBACK, "anthropic/never-needed::api-key"]
    )
    get_adapter = runtime.get_adapter

    def acquire(connection: Any) -> Any:
        acquired_connections.append(connection.connection_id)
        return get_adapter(connection)

    async def close_fallback() -> None:
        nonlocal fallback_closes
        fallback_closes += 1
        if outcome == "cancel-during-close":
            started.set()
            await release_close.wait()
        await ClosingStubAdapter.aclose(fallback)

    monkeypatch.setattr(runtime, "get_adapter", acquire)
    monkeypatch.setattr(fallback, "aclose", close_fallback)
    if outcome == "context-preparation-failure":

        def fail_catalog(*_args: Any, **_kwargs: Any) -> None:
            raise ConfigError("catalog preparation failed")

        monkeypatch.setattr("core.chat._run_state.pinned_skill_catalog", fail_catalog)
    if outcome == "cancel":

        async def block_request(*_args: Any, **_kwargs: Any) -> Any:
            started.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(adapter, "send", block_request)
    send = asyncio.create_task(
        build_chat_loop(runtime).send("coder", "Hi", session_id="session-one")
    )

    if outcome == "answer":
        await send
    elif outcome in {"cancel", "cancel-during-close"}:
        await started.wait()
        active = runtime.chat_run_manager.active_run(
            agent_id="coder", session_id="session-one", project_id=None
        )
        assert active is not None
        if outcome == "cancel-during-close":
            active.request_cancel(reason="user")
            assert fallback.closed is False
            release_close.set()
        else:
            await runtime.chat_run_manager.cancel(active.id, reason="user")
        with pytest.raises(RunCancelledError):
            await send
    else:
        with pytest.raises((ProviderError, ConfigError)):
            await send

    assert adapter.closed is True
    assert len(adapter.requests) == (
        0 if outcome in {"context-preparation-failure", "cancel"} else 1
    )
    prefetched = outcome != "context-preparation-failure"
    assert fallback.closed is prefetched
    assert fallback_closes == int(prefetched)
    assert acquired_connections == [
        "openai:api-key",
        *(["anthropic:api-key"] if prefetched else []),
    ]
    assert fallback.requests == []


@pytest.mark.asyncio
async def test_fallback_adapter_is_closed_when_its_request_preparation_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    switch_route: Any = ProviderRateLimitError("switch route")
    primary = ClosingStubAdapter([switch_route])
    fallback = ClosingStubAdapter([])
    runtime = _fallback_runtime(
        tmp_path,
        primary,
        fallback,
        model="openai/primary",
        fallback_models=["anthropic/fallback::api-key"],
        models=StubModels({("openai", "primary"): 128_000, ("anthropic", "fallback"): 128_000}),
    )
    original = RequestBuilder.build_request_state

    async def fail_fallback(self: Any, *args: Any, **kwargs: Any) -> Any:
        if kwargs["inputs"].reasoning_scope_model.startswith("anthropic/fallback"):
            raise ConfigError("fallback preparation failed")
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(RequestBuilder, "build_request_state", fail_fallback)

    with pytest.raises(ConfigError, match="fallback preparation failed"):
        await build_chat_loop(runtime).send("coder", "hello", session_id="session")

    assert (primary.closed, fallback.closed) == (True, True)
    assert fallback.requests == []
