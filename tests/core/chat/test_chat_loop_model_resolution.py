"""Model resolution in Chat Runs: the Provider Connection and Model a Run's requests use, how
invalid Model bindings fail, Session Agent overrides and the sampling parameters sent."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from core.chat import ChatError
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

    answer = await build_chat_loop(runtime).send("coder", "Hi", session_id="session-one")

    provider_id = model.split("/", 1)[0]
    assert (runtime.adapter_provider_id, runtime.adapter_connection_id) == (provider_id, connection)
    assert adapter.requests[0]["model_id"] == "gpt-5.2"
    # Replayable reasoning is scoped to the resolved Connection and account.
    assert answer.reasoning_scope == reasoning_scope


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
@pytest.mark.parametrize(
    ("model", "agent_temperature", "temperature", "top_p"),
    [
        ("ollama-cloud/glm-5.2", None, None, None),
        ("openai/gpt-5.2", 0.1, 0.1, None),
    ],
    ids=["unset-ignores-model-recommendations", "agent-temperature"],
)
async def test_resolved_sampling_parameters_reach_the_adapter(
    tmp_path: Path,
    model: str,
    agent_temperature: float | None,
    temperature: float | None,
    top_p: float | None,
) -> None:
    agent = StubAgent(
        id="coder",
        model=model,
        allowed_tools=["*"],
        temperature=agent_temperature,  # type: ignore[arg-type]
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
