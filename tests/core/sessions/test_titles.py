"""Immediate local and background Model-generated Session titles."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any, cast

import pytest

from core.chat import ChatMessage
from core.chat.content_blocks import ContentBlock, FileBlock, FileMentionBlock, TextBlock
from core.providers.accounts import ConnectionRef
from core.providers.errors import ProviderError
from core.sessions import ChatSessionManager, SessionAddress
from core.sessions.titles import (
    GENERATED_TITLE_MAX_CHARACTERS,
    TITLE_INPUT_HEAD_BYTES,
    TITLE_INPUT_TAIL_BYTES,
    TITLE_OMISSION_MARKER,
    SessionTitleService,
    _bounded_text_projection,
    _generated_title,
    _local_title,
    _title_input,
    _title_source_parts,
)


def _address(agent_id: str, session_id: str, project_id: str | None = None) -> SessionAddress:
    return SessionAddress(project_id=project_id, agent_id=agent_id, session_id=session_id)


class StubStorage:
    def __init__(self, *, enabled: bool, model: str = "") -> None:
        self.settings = {"enabled": enabled, "model": model}

    def load_session_title_settings(self) -> dict[str, Any]:
        return dict(self.settings)


class StubAdapter:
    def __init__(self, title: str = "Generated title", error: Exception | None = None) -> None:
        self.title = title
        self.error = error
        self.requests: list[dict[str, Any]] = []
        self.closed = False
        self.debug_context: Any = None

    def set_debug_context(self, context: Any) -> None:
        self.debug_context = context

    async def send(self, messages: list[dict], **kwargs: Any) -> dict[str, Any]:
        self.requests.append({"messages": messages, **kwargs})
        if self.error is not None:
            raise self.error
        return {"content": self.title}

    def normalize_response(
        self, response: dict[str, Any], *, model_id: str | None = None
    ) -> dict[str, Any]:
        assert model_id is not None
        return response

    async def aclose(self) -> None:
        self.closed = True


class StubModels:
    def __init__(self, recommended: dict[tuple[str, str], float] | None = None) -> None:
        self._recommended = recommended or {}

    def get(self, provider_id: str, model_id: str) -> Any:
        recommended = self._recommended.get((provider_id, model_id))
        if recommended is None:
            raise KeyError(model_id)
        return SimpleNamespace(recommended_temperature=recommended)


class StubRuntime:
    def __init__(
        self,
        tmp_path,
        *,
        enabled: bool,
        configured_model: str = "",
        adapters: list[StubAdapter] | None = None,
        recommended_temperatures: dict[tuple[str, str], float] | None = None,
    ) -> None:
        self.chat_sessions = ChatSessionManager(tmp_path)
        self.storage = StubStorage(enabled=enabled, model=configured_model)
        self.models = StubModels(recommended_temperatures)
        self._adapters = list(adapters or [StubAdapter()])
        self.adapter_calls: list[tuple[str, str]] = []

    def get_adapter(self, connection: ConnectionRef) -> StubAdapter:
        self.adapter_calls.append((connection.provider_id, connection.connection_id))
        return self._adapters.pop(0)


def _append_first_user(runtime: StubRuntime, content: Any) -> None:
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    session.append(ChatMessage.user(content))


async def _wait_for_background(service: SessionTitleService) -> None:
    tasks = list(service._background_tasks)
    if tasks:
        await asyncio.gather(*tasks)


@pytest.mark.asyncio
async def test_aclose_cancels_and_drains_generated_title_tasks(tmp_path) -> None:
    started = asyncio.Event()

    class BlockingAdapter(StubAdapter):
        async def send(self, messages: list[dict], **kwargs: Any) -> dict[str, Any]:
            self.requests.append({"messages": messages, **kwargs})
            started.set()
            await asyncio.Event().wait()
            return {"content": "unreachable"}

    adapter = BlockingAdapter()
    runtime = StubRuntime(
        tmp_path,
        enabled=True,
        configured_model="openai/title::cheap",
        adapters=[adapter],
    )
    _append_first_user(runtime, "A title request")
    service = SessionTitleService(cast(Any, runtime))
    service.notify_user_message(
        agent_id="coder",
        session_id="session-one",
        project_id=None,
        agent=SimpleNamespace(model="openai/agent::main"),
        content="A title request",
        run_id="run-one",
    )
    await started.wait()

    await service.aclose()

    assert service._background_tasks == set()
    assert adapter.closed is True
    service.notify_user_message(
        agent_id="coder",
        session_id="session-one",
        project_id=None,
        agent=SimpleNamespace(model="openai/agent::main"),
        content="late title request",
        run_id="run-two",
    )
    assert service._background_tasks == set()


def test_local_title_collapses_whitespace_and_caps_at_40_with_ellipsis() -> None:
    title = _local_title("  Explain\n\nthis   very long request " + "x" * 50, [])

    assert len(title) == 40
    assert title.endswith("…")
    assert "  " not in title


def test_title_projection_bounds_text_and_excludes_attached_content() -> None:
    secret = "must-not-reach-title-model"
    content: list[ContentBlock] = [
        TextBlock(type="text", text="a" * 6000 + "TAIL"),
        FileBlock(
            type="file",
            attachment_id="attachment-one",
            filename="report.pdf",
            media_type="application/pdf",
        ),
        FileMentionBlock(
            type="file_mention",
            path="docs/private.txt",
            status="inlined",
            text=secret,
            size_bytes=999,
        ),
    ]

    text, attachments = _title_source_parts(content)
    projection = _title_input(text, attachments)

    assert TITLE_OMISSION_MARKER in projection
    assert "report.pdf (application/pdf)" in projection
    assert "private.txt (mentioned file, 999 bytes)" in projection
    assert secret not in projection
    bounded_text = _bounded_text_projection(text)
    assert len(bounded_text.encode("utf-8")) <= (
        TITLE_INPUT_HEAD_BYTES + TITLE_INPUT_TAIL_BYTES + len(TITLE_OMISSION_MARKER.encode("utf-8"))
    )


@pytest.mark.asyncio
async def test_disabled_generation_keeps_local_title_without_adapter(tmp_path) -> None:
    runtime = StubRuntime(tmp_path, enabled=False, adapters=[])
    _append_first_user(runtime, "  Investigate\n login   failures in production  ")
    service = SessionTitleService(cast(Any, runtime))

    service.notify_user_message(
        agent_id="coder",
        session_id="session-one",
        project_id=None,
        agent=SimpleNamespace(model="openai/agent::main"),
        content="  Investigate\n login   failures in production  ",
        run_id="run-one",
    )
    await _wait_for_background(service)

    metadata = runtime.chat_sessions.get_metadata(_address("coder", "session-one"))
    assert metadata["auto_title"] == "Investigate login failures in production"
    assert metadata["auto_title_initialized"] is True
    assert runtime.adapter_calls == []


@pytest.mark.asyncio
async def test_configured_title_model_replaces_local_title_with_bounded_request(tmp_path) -> None:
    adapter = StubAdapter('Title: "Review report".')
    runtime = StubRuntime(
        tmp_path,
        enabled=True,
        configured_model="openai/title::cheap",
        adapters=[adapter],
    )
    content = "Please review\n" + "x" * 7000 + "\nand summarize risks"
    _append_first_user(runtime, content)
    service = SessionTitleService(cast(Any, runtime))

    service.notify_user_message(
        agent_id="coder",
        session_id="session-one",
        project_id=None,
        agent=SimpleNamespace(model="openai/agent::main"),
        content=content,
        run_id="run-one",
    )
    await _wait_for_background(service)

    assert runtime.adapter_calls == [("openai", "openai:cheap")]
    request = adapter.requests[0]
    assert request["messages"][0]["role"] == "system"
    system_prompt = request["messages"][0]["content"]
    assert isinstance(system_prompt, str)
    assert system_prompt
    assert TITLE_OMISSION_MARKER in request["messages"][1]["content"]
    assert "max_tokens" not in request
    assert request["thinking_effort"] == "none"
    assert request["temperature"] is None
    assert runtime.chat_sessions.get_metadata(_address("coder", "session-one"))["auto_title"] == (
        "Review report"
    )
    assert adapter.closed is True
    assert adapter.debug_context.run_id == "title-run-one"


@pytest.mark.asyncio
async def test_reasoning_mandatory_endpoint_retries_with_default_effort(tmp_path) -> None:
    """A rejected explicit disable retries once at the provider-default effort."""

    class RejectingAdapter(StubAdapter):
        async def send(self, messages: list[dict], **kwargs: Any) -> dict[str, Any]:
            self.requests.append({"messages": messages, **kwargs})
            if kwargs.get("thinking_effort") == "none":
                raise ProviderError("Reasoning is mandatory for this endpoint.")
            return {"content": self.title}

    adapter = RejectingAdapter('Title: "Mandatory reasoning"')
    runtime = StubRuntime(
        tmp_path,
        enabled=True,
        configured_model="openrouter/stealth/ox-alpha::api-key",
        adapters=[adapter],
    )
    _append_first_user(runtime, "Please fix the scroll bug")
    service = SessionTitleService(cast(Any, runtime))

    service.notify_user_message(
        agent_id="coder",
        session_id="session-one",
        project_id=None,
        agent=SimpleNamespace(model="openai/agent::main"),
        content="Please fix the scroll bug",
        run_id="run-one",
    )
    await _wait_for_background(service)

    assert [request["thinking_effort"] for request in adapter.requests] == ["none", ""]
    assert runtime.chat_sessions.get_metadata(_address("coder", "session-one"))["auto_title"] == (
        "Mandatory reasoning"
    )
    assert adapter.closed is True


@pytest.mark.asyncio
async def test_generated_title_uses_model_recommended_temperature(tmp_path) -> None:
    adapter = StubAdapter('Title: "Audit".')
    runtime = StubRuntime(
        tmp_path,
        enabled=True,
        configured_model="ollama-cloud/glm-5.2::cloud",
        adapters=[adapter],
        recommended_temperatures={("ollama-cloud", "glm-5.2"): 1.0},
    )
    _append_first_user(runtime, "Audit the workspace")
    service = SessionTitleService(cast(Any, runtime))

    service.notify_user_message(
        agent_id="coder",
        session_id="session-one",
        project_id=None,
        agent=SimpleNamespace(model="openai/agent::main"),
        content="Audit the workspace",
        run_id="run-one",
    )
    await _wait_for_background(service)

    assert adapter.requests[0]["temperature"] == 1.0


def test_generated_title_uses_final_content_and_ignores_reasoning() -> None:
    title = _generated_title(
        {
            "content": "Session naming audit",
            "reasoning": "The user is asking me to inspect the naming agent",
            "reasoning_meta": {"reasoning_details": [{"text": "Internal analysis"}]},
        }
    )

    assert title == "Session naming audit"


def test_generated_title_ignores_typed_reasoning_blocks() -> None:
    title = _generated_title(
        {
            "content": [
                {"type": "reasoning", "text": "The user wants a title"},
                {"type": "text", "text": "Session naming audit"},
            ]
        }
    )

    assert title == "Session naming audit"


@pytest.mark.parametrize(
    "response",
    [
        "```\nSession naming audit\n```",
        "```text\nSession naming audit\n```",
        "~~~plaintext\r\nSession naming audit\r\n~~~",
        "Title:\nSession naming audit",
        'Titel:\n"Session naming audit"',
        "<think>Hidden analysis\nMore analysis</think>\n```\nSession naming audit\n```",
        "```text\nTitle:\nSession naming audit\n```",
    ],
)
def test_generated_title_accepts_unambiguous_wrappers(response: str) -> None:
    assert _generated_title({"content": response}) == "Session naming audit"


@pytest.mark.parametrize(
    "response",
    [
        "First candidate\nSecond candidate",
        "Here is your title:\nSession naming audit",
        "Session naming audit\nThis title summarizes the request.",
        "```\nFirst candidate\nSecond candidate\n```",
        "```\nSession naming audit\n~~~",
        "```\nSession naming audit",
        "```\n```",
        "Title:\n",
        "",
    ],
)
def test_generated_title_rejects_ambiguous_or_empty_output(response: str) -> None:
    with pytest.raises(ValueError):
        _generated_title({"content": response})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "accepted"),
    [
        ("```text\nSession naming audit\n```", True),
        ("Title:\nSession naming audit", True),
        ("private-candidate-one\nprivate-candidate-two", False),
        ("", False),
        ("x" * (GENERATED_TITLE_MAX_CHARACTERS + 1), False),
        ("The user is asking me to perform a session naming audit", False),
    ],
)
async def test_title_validation_preserves_fallback_and_logs_without_content_or_trace(
    tmp_path, caplog: pytest.LogCaptureFixture, response: str, accepted: bool
) -> None:
    adapter = StubAdapter(response)
    runtime = StubRuntime(tmp_path, enabled=True, adapters=[adapter])
    _append_first_user(runtime, "Inspect session naming")
    service = SessionTitleService(cast(Any, runtime))
    caplog.set_level(logging.WARNING, logger="vbot.sessions.titles")

    service.notify_user_message(
        agent_id="coder",
        session_id="session-one",
        project_id=None,
        agent=SimpleNamespace(model="openai/agent::main"),
        content="Inspect session naming",
        run_id="run-one",
    )
    await _wait_for_background(service)

    metadata = runtime.chat_sessions.get_metadata(_address("coder", "session-one"))
    assert metadata["auto_title"] == (
        "Session naming audit" if accepted else "Inspect session naming"
    )
    assert metadata["auto_title_initialized"] is True
    assert len(adapter.requests) == 1
    assert adapter.closed is True
    records = [record for record in caplog.records if record.name == "vbot.sessions.titles"]
    assert len(records) == (0 if accepted else 1)
    for record in records:
        assert record.levelno == logging.WARNING
        assert record.exc_info is None
        assert record.args
        if response:
            assert response not in record.getMessage()


def test_generated_title_enforces_absolute_character_limit() -> None:
    assert len("x" * GENERATED_TITLE_MAX_CHARACTERS) == GENERATED_TITLE_MAX_CHARACTERS
    assert _generated_title({"content": "x" * GENERATED_TITLE_MAX_CHARACTERS}) == (
        "x" * GENERATED_TITLE_MAX_CHARACTERS
    )

    with pytest.raises(ValueError):
        _generated_title({"content": "x" * (GENERATED_TITLE_MAX_CHARACTERS + 1)})


@pytest.mark.parametrize(
    "meta_title",
    [
        "The user is asking me to perform a session naming audit",
        "Was macht der User in dieser Session",
    ],
)
def test_generated_title_rejects_meta_descriptions(meta_title: str) -> None:
    with pytest.raises(ValueError):
        _generated_title({"content": meta_title})


@pytest.mark.asyncio
async def test_meta_description_from_model_keeps_immediate_local_title(tmp_path) -> None:
    adapter = StubAdapter("The user is asking me to perform a session naming audit")
    runtime = StubRuntime(tmp_path, enabled=True, adapters=[adapter])
    _append_first_user(runtime, "Inspect session naming")
    service = SessionTitleService(cast(Any, runtime))

    service.notify_user_message(
        agent_id="coder",
        session_id="session-one",
        project_id=None,
        agent=SimpleNamespace(model="openai/agent::main"),
        content="Inspect session naming",
        run_id="run-one",
    )
    await _wait_for_background(service)

    assert runtime.chat_sessions.get_metadata(_address("coder", "session-one"))["auto_title"] == (
        "Inspect session naming"
    )


@pytest.mark.asyncio
async def test_unclosed_thinking_output_keeps_immediate_local_title(tmp_path) -> None:
    adapter = StubAdapter("<think>The user is asking me to perform a naming audit")
    runtime = StubRuntime(tmp_path, enabled=True, adapters=[adapter])
    _append_first_user(runtime, "Inspect session naming")
    service = SessionTitleService(cast(Any, runtime))

    service.notify_user_message(
        agent_id="coder",
        session_id="session-one",
        project_id=None,
        agent=SimpleNamespace(model="openai/agent::main"),
        content="Inspect session naming",
        run_id="run-one",
    )
    await _wait_for_background(service)

    assert runtime.chat_sessions.get_metadata(_address("coder", "session-one"))["auto_title"] == (
        "Inspect session naming"
    )


@pytest.mark.asyncio
async def test_empty_title_model_selection_uses_resolved_agent_model(tmp_path) -> None:
    adapter = StubAdapter("Agent-generated title")
    runtime = StubRuntime(tmp_path, enabled=True, configured_model="", adapters=[adapter])
    _append_first_user(runtime, "Investigate login failure")
    service = SessionTitleService(cast(Any, runtime))

    service.notify_user_message(
        agent_id="coder",
        session_id="session-one",
        project_id=None,
        agent=SimpleNamespace(model="openai/agent::main"),
        content="Investigate login failure",
        run_id="run-one",
    )
    await _wait_for_background(service)

    assert runtime.adapter_calls == [("openai", "openai:main")]
    assert adapter.requests[0]["model_id"] == "agent"
    assert runtime.chat_sessions.get_metadata(_address("coder", "session-one"))["auto_title"] == (
        "Agent-generated title"
    )


@pytest.mark.asyncio
async def test_failed_configured_model_keeps_local_title_without_agent_retry(
    tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    adapter = StubAdapter(error=RuntimeError("provider down"))
    runtime = StubRuntime(
        tmp_path,
        enabled=True,
        configured_model="openai/title::cheap",
        adapters=[adapter],
    )
    _append_first_user(runtime, "Investigate login failure")
    service = SessionTitleService(cast(Any, runtime))

    service.notify_user_message(
        agent_id="coder",
        session_id="session-one",
        project_id=None,
        agent=SimpleNamespace(model="openai/agent::main"),
        content="Investigate login failure",
        run_id="run-one",
    )
    await _wait_for_background(service)

    assert runtime.adapter_calls == [("openai", "openai:cheap")]
    assert runtime.chat_sessions.get_metadata(_address("coder", "session-one"))["auto_title"] == (
        "Investigate login failure"
    )
    records = [record for record in caplog.records if record.name == "vbot.sessions.titles"]
    assert len(records) == 1
    assert records[0].exc_info is not None
    assert adapter.closed is True


@pytest.mark.asyncio
async def test_existing_session_is_marked_without_backfill_or_model_request(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = StubRuntime(tmp_path, enabled=True, adapters=[])
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    session.append(ChatMessage.user("Old first request"))
    session.append(ChatMessage.assistant(model="openai/agent", content="Old answer"))
    session.append(ChatMessage.user("New request"))
    monkeypatch.setattr(
        session,
        "load_active",
        lambda: (_ for _ in ()).throw(
            AssertionError("title initialization must not load the complete active history")
        ),
    )
    service = SessionTitleService(cast(Any, runtime))

    service.notify_user_message(
        agent_id="coder",
        session_id="session-one",
        project_id=None,
        agent=SimpleNamespace(model="openai/agent::main"),
        content="New request",
        run_id="run-two",
    )
    await _wait_for_background(service)

    metadata = runtime.chat_sessions.get_metadata(_address("coder", "session-one"))
    assert metadata["auto_title_initialized"] is True
    assert "auto_title" not in metadata
    assert runtime.adapter_calls == []


@pytest.mark.asyncio
async def test_manual_name_skips_model_but_preserves_local_title_underneath(tmp_path) -> None:
    runtime = StubRuntime(tmp_path, enabled=True, adapters=[])
    _append_first_user(runtime, "Investigate login failure")
    runtime.chat_sessions.set_title(_address("coder", "session-one"), "Manual name")
    service = SessionTitleService(cast(Any, runtime))

    service.notify_user_message(
        agent_id="coder",
        session_id="session-one",
        project_id=None,
        agent=SimpleNamespace(model="openai/agent::main"),
        content="Investigate login failure",
        run_id="run-one",
    )
    await _wait_for_background(service)

    metadata = runtime.chat_sessions.get_metadata(_address("coder", "session-one"))
    assert metadata["title"] == "Manual name"
    assert metadata["auto_title"] == "Investigate login failure"
    assert runtime.adapter_calls == []
