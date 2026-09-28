"""Run-local Model fallback: when a route switches, what the new route receives, and cleanup."""

from __future__ import annotations

import base64
import io
import json
import random
from pathlib import Path
from typing import Any, cast

import pytest
from PIL import Image

from core.attachments import AttachmentStore
from core.chat._request_builder import RequestBuilder
from core.chat._request_history import _restore_in_run_tool_result_content
from core.chat._tool_epoch import tool_change_from_note
from core.model_tasks import TASK_IMAGE_UNDERSTANDING
from core.providers.adapter import TOOL_RESULT_CONTENT_BLOCKS_FIELD
from core.providers.errors import ProviderAuthError, ProviderRateLimitError
from core.runs import ERROR_MESSAGE_PERSISTED_EVENT, MODEL_FALLBACK_ACTIVATED_EVENT, RunStatus
from core.tools import (
    ANALYZE_IMAGE_TOOL_NAME,
    ToolRegistry,
    tool_failure,
    tool_success,
)
from core.tools.file_state import FileReadState
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
    ("failure", "streaming"),
    [
        (ProviderRateLimitError("primary rate limited"), False),
        (_model_not_found(), False),
        # A rate limit skips the remaining same-Model restarts on a dead quota.
        (ProviderRateLimitError("quota exhausted"), True),
    ],
    ids=["rate-limit", "model-not-found", "streaming-rate-limit"],
)
async def test_route_scoped_failure_switches_to_the_fallback_for_this_run(
    tmp_path: Path, failure: ProviderError, streaming: bool
) -> None:
    recovered = [
        {"type": "content_delta", "text": "Recovered"},
        {"type": "finish", "reason": "stop"},
    ]
    primary = StubAdapter([failure], stream_responses=[failure])
    fallback = StubAdapter(
        [{"content": "Recovered", "tool_calls": None}], stream_responses=[recovered]
    )
    runtime = _fallback_runtime(tmp_path, primary, fallback)

    assistant = await build_chat_loop(runtime, streaming=streaming).send(
        "coder", "Hi", session_id="session-one"
    )

    run = last_run(runtime)
    messages = history(runtime)
    primary_requests = primary.stream_requests if streaming else primary.requests
    fallback_requests = fallback.stream_requests if streaming else fallback.requests
    assert assistant.content == "Recovered"
    assert persisted_roles(messages) == ["user", "note", "assistant"]
    assert (
        messages[1].content == f"Model {PRIMARY} unavailable. Switched to {FALLBACK} for this run."
    )
    # Usage and cost belong to the Model that actually answered.
    assert messages[2].model == FALLBACK
    assert await _fallback_events(runtime, run) == [(PRIMARY, FALLBACK)]
    assert run.iteration_count == 1
    assert messages[-1].iteration_count == 1
    assert run.events[-1].payload["iteration_count"] == 1
    assert [request["model_id"] for request in primary_requests] == ["gpt-5.2"]
    assert [request["model_id"] for request in fallback_requests] == ["claude-sonnet-4"]


@pytest.mark.asyncio
async def test_rate_limit_without_a_fallback_fails_the_run_after_same_model_recovery(
    tmp_path: Path, recovery_waits: list[float]
) -> None:
    adapter = StubAdapter([ProviderRateLimitError("too many requests")] * 9)
    runtime = _fallback_runtime(tmp_path, adapter, fallback_models=[])

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
        ERROR_MESSAGE_PERSISTED_EVENT,
        "run_failed",
    ]
    persisted_error = next(
        e for e in await runtime.timelines.events(run) if e.type == ERROR_MESSAGE_PERSISTED_EVENT
    )
    assert persisted_error.payload["message"]["role"] == "error"
    assert persisted_error.payload["message"]["error_kind"] == "rate_limit"


@pytest.mark.asyncio
async def test_account_wide_fatal_error_never_advances_the_fallback_chain(tmp_path: Path) -> None:
    primary = StubAdapter([ProviderAuthError("invalid credential")])
    fallback = StubAdapter([{"content": "Should not be used", "tool_calls": None}])
    runtime = _fallback_runtime(tmp_path, primary, fallback)

    with pytest.raises(ProviderAuthError, match="invalid credential"):
        await build_chat_loop(runtime).send("coder", "Hi", session_id="session-one")

    run = last_run(runtime)
    assert run.status == RunStatus.FAILED
    assert persisted_roles(history(runtime)) == ["user", "error"]
    assert await _fallback_events(runtime, run) == []
    assert fallback.requests == []


@pytest.mark.asyncio
async def test_fallback_serves_the_rest_of_the_run_and_the_next_run_starts_on_the_primary(
    tmp_path: Path,
) -> None:
    primary = StubAdapter(
        [
            ProviderRateLimitError("primary rate limited"),
            {"content": "Primary turn 2", "tool_calls": None},
        ]
    )
    fallback = StubAdapter(
        [
            {
                "content": None,
                "tool_calls": [{"id": "call_1", "name": "probe", "arguments": {"value": "x"}}],
            },
            {"content": "Fallback turn 1", "tool_calls": None},
        ]
    )
    tools = _probe_tools(lambda _context, arguments: tool_success({"value": arguments["value"]}))
    runtime = _fallback_runtime(tmp_path, primary, fallback, allowed_tools=["probe"], tools=tools)

    first = await build_chat_loop(runtime).send("coder", "turn 1", session_id="session-one")
    first_run = last_run(runtime)
    second = await build_chat_loop(runtime).send("coder", "turn 2", session_id="session-one")

    assert (first.content, first.model) == ("Fallback turn 1", FALLBACK)
    assert (second.content, second.model) == ("Primary turn 2", PRIMARY)
    assert [request["model_id"] for request in primary.requests] == ["gpt-5.2", "gpt-5.2"]
    assert [request["model_id"] for request in fallback.requests] == ["claude-sonnet-4"] * 2
    assert await _fallback_events(runtime, first_run) == [(PRIMARY, FALLBACK)]
    assert await _fallback_events(runtime, last_run(runtime)) == []


class _RecordingAdapter(StubAdapter):
    """Stub adapter recording the wire model id of every send."""

    def __init__(self, responses: list[Any]) -> None:
        super().__init__(responses)
        self.served_model_ids: list[str] = []

    async def send(self, messages: list[JsonObject], *, model_id: str, **kwargs: Any) -> JsonObject:
        self.served_model_ids.append(model_id)
        return await super().send(messages, model_id=model_id, **kwargs)


@pytest.mark.asyncio
async def test_fallback_chain_skips_unresolvable_candidates_and_cascades_in_order(
    tmp_path: Path,
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
        "anthropic/first-resort::api-key",
        "anthropic/last-resort::api-key",
    ]
    runtime = _fallback_runtime(tmp_path, primary, candidates, fallback_models=chain)

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
async def test_candidate_whose_adapter_cannot_be_built_is_skipped(tmp_path: Path) -> None:
    primary = StubAdapter([ProviderRateLimitError("primary rate limited")])
    runtime = _fallback_runtime(
        tmp_path, primary, raise_on_connection={"anthropic:api-key": ConfigError("bad credential")}
    )

    # The broken candidate is never activated; the Run fails with the last actual
    # send failure, the primary's original error.
    with pytest.raises(ProviderRateLimitError, match="primary rate limited"):
        await build_chat_loop(runtime).send("coder", "Hi", session_id="session-one")

    run = last_run(runtime)
    assert run.status == RunStatus.FAILED
    assert persisted_roles(history(runtime)) == ["user", "error"]
    assert ERROR_MESSAGE_PERSISTED_EVENT in await event_types(runtime, run)
    assert await _fallback_events(runtime, run) == []


@pytest.mark.asyncio
async def test_fallback_failure_persists_the_fallback_error(tmp_path: Path) -> None:
    primary = StubAdapter([ProviderRateLimitError("primary rate limited")])
    fallback = StubAdapter([ProviderRateLimitError("fallback rate limited")])
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
    assert fallback.requests[-1]["kwargs"]["tools"] == []


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
    assert fallback.requests[-1]["kwargs"]["tools"] == []


@pytest.mark.asyncio
async def test_fallback_lists_the_pinned_tools_and_announced_additions_without_new_notes(
    tmp_path: Path,
) -> None:
    # The fallback route's cache is cold anyway, so it lists every Tool announced as
    # added; it neither re-pins nor re-applies the route gates of its own Model.
    tools = ToolRegistry()
    tools.register(
        ANALYZE_IMAGE_TOOL_NAME,
        "Analyze images.",
        {"type": "object"},
        lambda _context, _arguments: tool_success({"analysis": "ok"}),
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
        async def send(self, *args: Any, **kwargs: Any) -> Any:
            if self.requests:
                source.unlink()
            return await super().send(*args, **kwargs)

    class LimitedAdapter(StubAdapter):
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
            assert any("byte limit" in part.get("text", "") for part in parts)
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
@pytest.mark.parametrize("outcome", ["answer", "provider-error", "context-preparation-failure"])
async def test_the_run_adapter_is_closed_whatever_the_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    response: Any = (
        ProviderError("provider failed", retryable=False)
        if outcome == "provider-error"
        else {"content": "Hello", "tool_calls": None}
    )
    adapter = ClosingStubAdapter([response])
    runtime = _fallback_runtime(tmp_path, adapter, fallback_models=[])
    if outcome == "context-preparation-failure":

        def fail_catalog(*_args: Any, **_kwargs: Any) -> None:
            raise ConfigError("catalog preparation failed")

        monkeypatch.setattr("core.chat._run_state.pinned_skill_catalog", fail_catalog)
    send = build_chat_loop(runtime).send("coder", "Hi", session_id="session-one")

    if outcome == "answer":
        await send
    else:
        with pytest.raises((ProviderError, ConfigError)):
            await send

    assert adapter.closed is True
    assert len(adapter.requests) == (0 if outcome == "context-preparation-failure" else 1)


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
