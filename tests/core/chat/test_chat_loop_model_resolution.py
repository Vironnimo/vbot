"""Model resolution in Chat Runs: the Provider Connection and Model a Run's requests use, how
invalid Model bindings fail, Session Agent overrides and the sampling parameters sent."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any

import pytest

from core.chat import ChatError
from core.projects import AgentOverrides
from core.providers.errors import ProviderError
from core.providers.reasoning import ReasoningIntent
from core.runs import RunCancelledError
from tests.core.chat.chat_loop_support import (
    StubAdapter,
    StubAgent,
    StubModels,
    StubProviderCredentials,
    StubRuntime,
    build_chat_loop,
    session_address,
)

JsonObject = dict[str, Any]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "usable", "connection", "reasoning_scope"),
    [
        (
            "openai/gpt-5.2::subscription",
            None,
            "openai:subscription",
            "openai/gpt-5.2::subscription",
        ),
        ("openrouter/gpt-5.2::api-key", None, "openrouter:api-key", "openrouter/gpt-5.2::api-key"),
        ("openai/gpt-5.2", {"openai:api-key"}, "openai:api-key", "openai/gpt-5.2::api-key"),
        (
            "openai/gpt-5.2",
            {"openai:subscription", "openai:api-key"},
            "openai:subscription",
            "openai/gpt-5.2::subscription",
        ),
        (
            "openai/gpt-5.2",
            {"openai:api-key:work"},
            "openai:api-key",
            "openai/gpt-5.2::api-key:work",
        ),
    ],
    ids=[
        "connection-suffix",
        "provider-from-binding",
        "first-usable",
        "provider-connection-order",
        "named-account",
    ],
)
async def test_model_binding_selects_the_provider_connection_and_reasoning_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    model: str,
    usable: set[str] | None,
    connection: str,
    reasoning_scope: str,
) -> None:
    agent = StubAgent(id="coder", model=model, allowed_tools=["*"])
    adapter = StubAdapter(
        [
            {
                "content": "Hello",
                "reasoning": "Private state",
                "reasoning_meta": {"encrypted_content": "opaque"},
                "tool_calls": None,
            }
        ]
    )
    runtime: Any = StubRuntime(
        data_dir=tmp_path, agent=agent, adapter=adapter, provider_ids={"openai", "openrouter"}
    )
    if usable is not None:
        runtime.provider_credentials = StubProviderCredentials(usable)
    get_adapter = runtime.get_adapter
    loop_thread = threading.get_ident()

    def prepare_adapter(connection: Any) -> Any:
        assert threading.get_ident() != loop_thread
        return get_adapter(connection)

    monkeypatch.setattr(runtime, "get_adapter", prepare_adapter)

    answer = await build_chat_loop(runtime).send("coder", "Hi", session_id="session-one")

    provider_id = model.split("/", 1)[0]
    assert (runtime.adapter_provider_id, runtime.adapter_connection_id) == (provider_id, connection)
    assert adapter.requests[0]["model_id"] == "gpt-5.2"
    # Replayable reasoning is scoped to the resolved Connection and account.
    assert answer.reasoning_scope == reasoning_scope


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["cancelled", "route-metadata"])
async def test_aborted_model_setup_closes_the_constructed_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    adapter = StubAdapter([])
    runtime: Any = StubRuntime(
        data_dir=tmp_path, agent=StubAgent(id="coder", model="openai/gpt-5.2"), adapter=adapter
    )
    entered = asyncio.Event()
    release = threading.Event()
    event_loop = asyncio.get_running_loop()
    closed = 0

    def prepare_adapter(_connection: Any) -> StubAdapter:
        event_loop.call_soon_threadsafe(entered.set)
        assert release.wait(2)
        return adapter

    def invalid_metadata(_model: str) -> Any:
        raise ProviderError("route metadata unavailable", retryable=False)

    async def close_adapter() -> None:
        nonlocal closed
        closed += 1

    monkeypatch.setattr(runtime, "get_adapter", prepare_adapter)
    monkeypatch.setattr(adapter, "aclose", close_adapter, raising=False)
    if failure == "route-metadata":
        monkeypatch.setattr(adapter, "reasoning_replay_policy", invalid_metadata)
    runtime.chat_sessions.create("coder", session_id="session-one")
    run = await build_chat_loop(runtime).start_run("coder", "Hi", session_id="session-one")
    try:
        await asyncio.wait_for(entered.wait(), 1)
        if failure == "cancelled":
            run.request_cancel("user")
        release.set()
        with pytest.raises(RunCancelledError if failure == "cancelled" else ProviderError):
            await run.wait()
        assert closed == 1
        assert adapter.requests == []
    finally:
        release.set()
        await runtime.chat_runs.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "new_session"),
    [("", False), ("missing/gpt-5.2", False), ("openai/gpt-5.2::", False), ("", True)],
    ids=["empty", "missing-provider", "dangling-suffix", "empty-in-new-session"],
)
async def test_invalid_model_binding_fails_before_any_session_or_request(
    tmp_path: Path, model: str, new_session: bool
) -> None:
    agent = StubAgent(id="coder", model=model, allowed_tools=["*"])
    runtime: Any = StubRuntime(
        data_dir=tmp_path, agent=agent, adapter=StubAdapter([]), provider_ids={"openai"}
    )
    loop = build_chat_loop(runtime)

    with pytest.raises(ChatError):
        if new_session:
            await loop.start_run_in_new_session("coder", "Scheduled work")
        else:
            await loop.send("coder", "Hi", session_id="session-one")

    assert runtime.adapter_provider_id is None
    assert runtime.chat_sessions.list("coder") == []

    # A bad temporary Model is checked before a newly requested Session exists.
    with pytest.raises(ChatError):
        await loop.start_run_in_new_session(
            "coder", "Scheduled work", run_overrides=AgentOverrides(model="missing/model")
        )
    assert runtime.chat_sessions.list("coder") == []


@pytest.mark.asyncio
async def test_session_agent_overrides_apply_to_every_run_of_only_that_session(
    tmp_path: Path,
) -> None:
    agent = StubAgent(
        id="coder",
        model="openai/gpt-5.2",
        thinking_effort="low",
        temperature=0.1,
        allowed_tools=["*"],
    )
    adapter = StubAdapter(
        [
            {"content": "Overridden", "tool_calls": None},
            {"content": "Overridden again", "tool_calls": None},
            {"content": "Configured", "tool_calls": None},
        ]
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    runtime.agent_resolver.update_session_overrides(
        session_address("coder", "session-one"),
        {"model": "openai/gpt-mini", "thinking_effort": "high", "temperature": 0.7},
    )
    loop = build_chat_loop(runtime)

    await loop.send("coder", "First", session_id="session-one")
    run = await runtime.chat_run_manager.start(
        session_address("coder", "session-one"), loop.run_executor("Second")
    )
    await run.wait()
    await loop.send("coder", "Elsewhere", session_id="session-two")

    overridden = [
        (
            request["model_id"],
            request["kwargs"]["thinking_effort"],
            request["kwargs"]["temperature"],
        )
        for request in adapter.requests
    ]
    assert overridden == [
        ("gpt-mini", "high", 0.7),
        ("gpt-mini", "high", 0.7),
        ("gpt-5.2", "low", 0.1),
    ]


@pytest.mark.asyncio
async def test_a_new_session_starts_with_its_agent_overrides_and_keeps_them(
    tmp_path: Path,
) -> None:
    agent = StubAgent(
        id="coder", model="openai/gpt-5.2", thinking_effort="low", allowed_tools=["*"]
    )
    adapter = StubAdapter([{"content": "Overridden", "tool_calls": None}])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    overrides = AgentOverrides(model="openai/gpt-mini", thinking_effort="high")

    run = await build_chat_loop(runtime).start_run_in_new_session(
        "coder", "First", agent_overrides=overrides
    )
    await run.wait()

    # The first Run already uses the overrides, and the new Session stores them.
    request = adapter.requests[0]
    assert (request["model_id"], request["kwargs"]["thinking_effort"]) == ("gpt-mini", "high")
    metadata = runtime.chat_sessions.get_metadata(session_address("coder", run.session_id))
    assert metadata["agent_overrides"] == {"model": "openai/gpt-mini", "thinking_effort": "high"}


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["new", "existing", "queued"])
@pytest.mark.parametrize(
    ("effort", "intent", "rendered"),
    [
        ("high", ReasoningIntent("effort", effort_level="high"), "high"),
        (None, ReasoningIntent("default"), ""),
        ("none", ReasoningIntent("off"), "none"),
        ("max", ReasoningIntent("effort", effort_level="high"), "high"),
        ("high", ReasoningIntent("on"), "on"),
        ("high", ReasoningIntent("on", effort_level="high"), "high"),
        ("high", ReasoningIntent("budget", budget_tokens=1024), None),
    ],
    ids=[
        "effort",
        "provider-default",
        "disabled",
        "provider-snapped",
        "toggle",
        "toggle-with-effort",
        "budget-without-effort",
    ],
)
async def test_run_overrides_leave_session_defaults_for_the_next_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    effort: str | None,
    intent: ReasoningIntent,
    rendered: str | None,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", thinking_effort="low")
    adapter = StubAdapter(
        [{"content": "Done", "tool_calls": None} for _ in range(2 if path == "new" else 3)]
    )
    describe = adapter.describe_reasoning_render
    monkeypatch.setattr(
        adapter,
        "describe_reasoning_render",
        lambda model, selected: intent if model == "gpt-mini" else describe(model, selected),
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    loop = build_chat_loop(runtime)
    overrides = AgentOverrides(
        model="openai/gpt-mini", thinking_effort=effort, thinking_effort_default=effort is None
    )
    if path == "new":
        run = await loop.start_run_in_new_session("coder", "Automated", run_overrides=overrides)
        session_id = run.session_id
    else:
        initial = await loop.start_run_in_new_session("coder", "Initial")
        await initial.wait()
        session_id = initial.session_id
        if path == "existing":
            run = await loop.start_run(
                "coder", "Automated", session_id=session_id, run_overrides=overrides
            )
        else:
            release = asyncio.Event()

            async def block(_run: Any) -> str:
                await release.wait()
                return "Released"

            blocker = await runtime.chat_run_manager.start(
                session_address("coder", session_id), block
            )
            try:
                queued = await loop.queue_run(
                    "coder", "Automated", session_id=session_id, run_overrides=overrides
                )
                assert not queued.steerable
                _, executor, display = await loop.build_queue_update(
                    "coder", session_id, "Updated automated task", queued
                )
                assert runtime.chat_runs.update_queued(
                    "coder", session_id, queued.item_id, executor, display, project_id=None
                )
            finally:
                release.set()
                await blocker.wait()
            run = await queued.future
    automated = await run.wait()
    normal = await loop.start_run("coder", "Manual", session_id=session_id)
    ordinary = await normal.wait()
    assert [
        (request["model_id"], request["kwargs"]["thinking_effort"])
        for request in adapter.requests[-2:]
    ] == [("gpt-mini", effort), ("gpt-5.2", "low")]
    assert (automated.model, automated.thinking_effort) == ("openai/gpt-mini", rendered)
    assert (ordinary.model, ordinary.thinking_effort) == ("openai/gpt-5.2", "low")
    assert run.run_overrides == overrides
    assert normal.run_overrides is None
    saved = runtime.agent_resolver.session_overrides(session_address("coder", session_id))
    assert (saved.model, saved.thinking_effort) == ("openai/gpt-5.2", "low")
    assert (agent.model, agent.thinking_effort) == ("openai/gpt-5.2", "low")
    history = runtime.chat_sessions.get(session_address("coder", session_id)).load_active()
    answers = [message for message in history if message.role == "assistant"]
    assert [message.thinking_effort for message in answers[-2:]] == [rendered, "low"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "temperature", "top_p"),
    [
        ("ollama-cloud/glm-5.2", None, None),
        ("openai/gpt-5.2", 0.1, None),
        ("openai/gpt-5.2", None, 0.9),
    ],
    ids=["unset-ignores-model-recommendations", "agent-temperature", "agent-top-p"],
)
async def test_resolved_sampling_parameters_reach_the_adapter(
    tmp_path: Path,
    model: str,
    temperature: float | None,
    top_p: float | None,
) -> None:
    agent = StubAgent(
        id="coder",
        model=model,
        allowed_tools=["*"],
        temperature=temperature,
        top_p=top_p,
    )
    adapter = StubAdapter([{"content": "Hi", "tool_calls": None}])
    models = StubModels(
        {("ollama-cloud", "glm-5.2"): 200_000, ("openai", "gpt-5.2"): 128_000},
        recommended_temperatures={("ollama-cloud", "glm-5.2"): 1.0},
        recommended_top_ps={("ollama-cloud", "glm-5.2"): 0.95},
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, models=models)

    await build_chat_loop(runtime).send("coder", "Hello", session_id="session-one")

    sent = adapter.requests[0]["kwargs"]
    assert (sent["temperature"], sent["top_p"]) == (temperature, top_p)
