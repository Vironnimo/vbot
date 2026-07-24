"""Chat-loop tests grouped by model resolution."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from core.chat import (
    ChatError,
)
from core.projects import AgentRunOverrides
from core.utils.errors import ProviderError
from tests.core.chat.chat_loop_support import (
    StubAdapter,
    StubAgent,
    StubProviderCredentials,
    StubRuntime,
    build_chat_loop,
    persisted_roles,
)

JsonObject = dict[str, Any]


@pytest.mark.asyncio
async def test_provider_errors_propagate_after_user_message_is_persisted(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openai/unknown-new-model", allowed_tools=["*"])
    adapter = StubAdapter([ProviderError("provider failed", retryable=False)])  # type: ignore[list-item]
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)

    with pytest.raises(ProviderError, match="provider failed"):
        await build_chat_loop(runtime).send("coder", "Hi", session_id="session-one")

    messages = runtime.chat_sessions.get("coder", "session-one").load()
    assert persisted_roles(messages) == ["user", "error"]
    assert messages[1].error_kind == "provider_fatal"
    assert adapter.requests[0]["model_id"] == "unknown-new-model"


@pytest.mark.asyncio
async def test_empty_agent_model_raises_chat_error_before_persisting(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="", allowed_tools=["*"])
    adapter = StubAdapter([])
    runtime: Any = StubRuntime(
        data_dir=tmp_path, agent=agent, adapter=adapter, provider_ids={"openai"}
    )

    with pytest.raises(ChatError, match="no model set"):
        await build_chat_loop(runtime).send("coder", "Hi", session_id="session-one")

    assert runtime.chat_sessions.list("coder") == []


@pytest.mark.asyncio
async def test_automation_new_session_is_created_only_after_target_validation(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="", allowed_tools=["*"])
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=StubAdapter([]),
        provider_ids={"openai"},
    )

    with pytest.raises(ChatError, match="no model set"):
        await build_chat_loop(runtime).start_run_in_new_session("coder", "Scheduled work")

    assert runtime.chat_sessions.list("coder") == []


@pytest.mark.asyncio
async def test_chat_loop_uses_connection_from_model_suffix(tmp_path: Path) -> None:
    agent = StubAgent(
        id="coder",
        model="openai/gpt-5.2::subscription",
        allowed_tools=["*"],
    )
    adapter = StubAdapter([{"content": "Hello", "tool_calls": None}])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)

    await build_chat_loop(runtime).send("coder", "Hi", session_id="session-one")

    assert runtime.adapter_provider_id == "openai"
    assert runtime.adapter_connection_id == "openai:subscription"


@pytest.mark.asyncio
async def test_chat_loop_provider_comes_from_model_with_connection_suffix(tmp_path: Path) -> None:
    agent = StubAgent(
        id="coder",
        model="openrouter/gpt-5.2::api-key",
        allowed_tools=["*"],
    )
    adapter = StubAdapter([{"content": "Hello", "tool_calls": None}])
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=adapter,
        provider_ids={"openai", "openrouter"},
    )

    await build_chat_loop(runtime).send("coder", "Hi", session_id="session-one")

    assert runtime.adapter_provider_id == "openrouter"
    assert runtime.adapter_connection_id == "openrouter:api-key"
    assert adapter.requests[0]["model_id"] == "gpt-5.2"


@pytest.mark.asyncio
async def test_run_executor_applies_overrides_to_only_its_admitted_run(tmp_path: Path) -> None:
    agent = StubAgent(
        id="coder",
        model="openai/gpt-5.2",
        thinking_effort="low",
        allowed_tools=["*"],
    )
    adapter = StubAdapter(
        [
            {"content": "Overridden", "tool_calls": None},
            {"content": "Configured", "tool_calls": None},
        ]
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    runtime.chat_sessions.create("coder", session_id="session-one")
    loop = build_chat_loop(runtime)
    executor = loop.run_executor(
        "First",
        agent_overrides=AgentRunOverrides(
            model="openai/gpt-mini",
            thinking_effort="high",
        ),
    )

    run = await runtime.chat_run_manager.start(
        agent_id="coder",
        session_id="session-one",
        executor=executor,
        project_id=None,
    )
    await run.wait()
    await loop.send("coder", "Second", session_id="session-one")

    assert adapter.requests[0]["model_id"] == "gpt-mini"
    assert adapter.requests[0]["kwargs"]["thinking_effort"] == "high"
    assert adapter.requests[1]["model_id"] == "gpt-5.2"
    assert adapter.requests[1]["kwargs"]["thinking_effort"] == "low"


@pytest.mark.asyncio
async def test_chat_loop_model_without_suffix_falls_back_to_first_usable(tmp_path: Path) -> None:
    agent = StubAgent(
        id="coder",
        model="openai/gpt-5.2",
        allowed_tools=["*"],
    )
    adapter = StubAdapter([{"content": "Hello", "tool_calls": None}])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    runtime.provider_credentials = StubProviderCredentials({"openai:api-key"})

    await build_chat_loop(runtime).send("coder", "Hi", session_id="session-one")

    assert runtime.adapter_provider_id == "openai"
    assert runtime.adapter_connection_id == "openai:api-key"


@pytest.mark.asyncio
async def test_chat_loop_model_without_suffix_prefers_first_usable_in_provider_order(
    tmp_path: Path,
) -> None:
    agent = StubAgent(
        id="coder",
        model="openai/gpt-5.2",
        allowed_tools=["*"],
    )
    adapter = StubAdapter([{"content": "Hello", "tool_calls": None}])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    runtime.provider_credentials = StubProviderCredentials(
        {"openai:subscription", "openai:api-key"}
    )

    await build_chat_loop(runtime).send("coder", "Hi", session_id="session-one")

    assert runtime.adapter_provider_id == "openai"
    assert runtime.adapter_connection_id == "openai:subscription"


@pytest.mark.asyncio
async def test_reasoning_scope_includes_resolved_named_account(tmp_path: Path) -> None:
    agent = StubAgent(
        id="coder",
        model="openai/gpt-5.6-sol",
        allowed_tools=["*"],
    )
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
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    runtime.provider_credentials = StubProviderCredentials({"openai:api-key:work"})

    assistant = await build_chat_loop(runtime).send(
        "coder",
        "Hi",
        session_id="session-one",
    )

    assert assistant.reasoning_scope == "openai/gpt-5.6-sol::api-key:work"


class TestParseModelWithConnection:
    def test_no_suffix(self) -> None:
        from core.chat.chat import parse_model_with_connection

        assert parse_model_with_connection("openai/gpt-5.2") == (
            "openai",
            "gpt-5.2",
            "",
        )

    def test_suffix_present(self) -> None:
        from core.chat.chat import parse_model_with_connection

        assert parse_model_with_connection("openai/gpt-5.2::oauth") == (
            "openai",
            "gpt-5.2",
            "oauth",
        )

    def test_model_id_with_colon(self) -> None:
        from core.chat.chat import parse_model_with_connection

        assert parse_model_with_connection("openrouter/poolside/laguna-xs.2:free::api-key") == (
            "openrouter",
            "poolside/laguna-xs.2:free",
            "api-key",
        )

    def test_empty_model_raises(self) -> None:
        from core.chat.chat import parse_model_with_connection

        with pytest.raises(ChatError, match="no model set"):
            parse_model_with_connection("")

    def test_model_id_with_slashes(self) -> None:
        from core.chat.chat import parse_model_with_connection

        assert parse_model_with_connection("openrouter/anthropic/claude-sonnet-4::oauth") == (
            "openrouter",
            "anthropic/claude-sonnet-4",
            "oauth",
        )

    def test_dangling_suffix_raises(self) -> None:
        from core.chat.chat import parse_model_with_connection

        with pytest.raises(ChatError, match="connection suffix must not be empty"):
            parse_model_with_connection("openai/gpt-5.2::")


class TestParseBareModel:
    def test_strips_suffix(self) -> None:
        from core.chat.chat import parse_bare_model

        assert parse_bare_model("openai/gpt-5.2::oauth") == "openai/gpt-5.2"


@pytest.mark.asyncio
async def test_missing_provider_raises_chat_error_before_adapter_request(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="missing/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter([])
    runtime: Any = StubRuntime(
        data_dir=tmp_path, agent=agent, adapter=adapter, provider_ids={"openai"}
    )

    with pytest.raises(ChatError, match="provider not found: missing"):
        await build_chat_loop(runtime).send("coder", "Hi", session_id="session-one")

    assert runtime.adapter_provider_id is None
    assert runtime.chat_sessions.list("coder") == []


@pytest.mark.asyncio
async def test_dangling_model_suffix_raises_chat_error_before_adapter_request(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2::", allowed_tools=["*"])
    adapter = StubAdapter([])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)

    with pytest.raises(ChatError, match="connection suffix must not be empty"):
        await build_chat_loop(runtime).send("coder", "Hi", session_id="session-one")

    assert runtime.adapter_provider_id is None
    assert runtime.chat_sessions.list("coder") == []
