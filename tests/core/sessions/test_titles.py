"""Immediate local and background Model-generated Session titles."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any, cast

import pytest

from core.chat import ChatMessage
from core.chat.content_blocks import ContentBlock, FileBlock, FileMentionBlock, TextBlock
from core.models.pricing import TokenPricing, TokenRates
from core.providers.accounts import ConnectionRef
from core.providers.errors import ProviderError
from core.sessions import ChatSessionManager, OwnedSessionSummary, SessionAddress
from core.sessions.titles import (
    GENERATED_TITLE_MAX_CHARACTERS,
    TITLE_INPUT_HEAD_BYTES,
    TITLE_INPUT_TAIL_BYTES,
    TITLE_OMISSION_MARKER,
    TITLE_SYSTEM_PROMPT,
    SessionTitleService,
    _generated_title,
)
from tests.core.chat.usage_recorder_support import RecordingUsageRecorder
from tests.core.providers.adapter_test_support import AdapterHookDefaults


def _address(agent_id: str, session_id: str, project_id: str | None = None) -> SessionAddress:
    return SessionAddress(project_id=project_id, agent_id=agent_id, session_id=session_id)


class StubStorage:
    def __init__(self, *, enabled: bool, model: str = "") -> None:
        self.settings = {"enabled": enabled, "model": model}

    def load_session_title_settings(self) -> dict[str, Any]:
        return dict(self.settings)


class StubAdapter(AdapterHookDefaults):
    def __init__(
        self,
        title: str = "Generated title",
        error: Exception | None = None,
        usage: dict[str, int] | None = None,
    ) -> None:
        self.title = title
        self.error = error
        self.usage = usage
        self.requests: list[dict[str, Any]] = []
        self.closed = False
        self.debug_context: Any = None

    def set_debug_context(self, context: Any) -> None:
        self.debug_context = context

    async def send(self, messages: list[dict], **kwargs: Any) -> dict[str, Any]:
        self.requests.append({"messages": messages, **kwargs})
        if self.error is not None:
            raise self.error
        return {"content": self.title, **({"usage": self.usage} if self.usage else {})}

    def normalize_response(
        self, response: dict[str, Any], *, model_id: str | None = None
    ) -> dict[str, Any]:
        assert model_id is not None
        return response

    async def aclose(self) -> None:
        self.closed = True


class StubModels:
    def __init__(
        self,
        recommended: dict[tuple[str, str], float] | None = None,
        prices: dict[str, tuple[float, float]] | None = None,
    ) -> None:
        self._recommended = recommended or {}
        self._prices = prices or {}

    def pricing_for(self, model_reference: str) -> TokenPricing | None:
        price = self._prices.get(model_reference.split("::", 1)[0])
        if price is None:
            return None
        return TokenPricing("catalog", TokenRates(input=price[0], output=price[1]))

    def get(self, provider_id: str, model_id: str) -> Any:
        recommended = self._recommended.get((provider_id, model_id))
        if recommended is None:
            raise KeyError(model_id)
        return SimpleNamespace(recommended_temperature=recommended)


class StubRuntime:
    def __init__(
        self,
        chat_sessions: ChatSessionManager,
        *,
        enabled: bool,
        configured_model: str = "",
        adapters: list[StubAdapter] | None = None,
        recommended_temperatures: dict[tuple[str, str], float] | None = None,
        prices: dict[str, tuple[float, float]] | None = None,
    ) -> None:
        self.chat_sessions = chat_sessions
        self.storage = StubStorage(enabled=enabled, model=configured_model)
        self.models = StubModels(recommended_temperatures, prices)
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


def _notify(service: SessionTitleService, content: Any, run_id: str = "run-one") -> None:
    service.notify_user_message(
        agent_id="coder",
        session_id="session-one",
        project_id=None,
        agent=SimpleNamespace(model="openai/agent::main"),
        content=content,
        run_id=run_id,
    )


async def _title_first_message(service: SessionTitleService, content: Any) -> None:
    _notify(service, content)
    await _wait_for_background(service)


def _metadata(runtime: StubRuntime) -> dict[str, Any]:
    metadata: dict[str, Any] = runtime.chat_sessions.get_metadata(_address("coder", "session-one"))
    return metadata


@pytest.mark.asyncio
async def test_aclose_cancels_and_drains_generated_title_tasks(manager) -> None:
    started = asyncio.Event()

    class BlockingAdapter(StubAdapter):
        async def send(self, messages: list[dict], **kwargs: Any) -> dict[str, Any]:
            self.requests.append({"messages": messages, **kwargs})
            started.set()
            await asyncio.Event().wait()
            return {"content": "unreachable"}

    adapter = BlockingAdapter()
    runtime = StubRuntime(
        manager,
        enabled=True,
        configured_model="openai/title::cheap",
        adapters=[adapter],
    )
    _append_first_user(runtime, "A title request")
    service = SessionTitleService(cast(Any, runtime))
    _notify(service, "A title request")
    await started.wait()

    await service.aclose()

    assert service._background_tasks == set()
    assert adapter.closed is True
    _notify(service, "late title request", run_id="run-two")
    assert service._background_tasks == set()


@pytest.mark.asyncio
async def test_disabled_generation_keeps_local_title_without_adapter(manager) -> None:
    runtime = StubRuntime(manager, enabled=False, adapters=[])
    content = "  Investigate\n login   failures in production  " + "x" * 50
    _append_first_user(runtime, content)
    service = SessionTitleService(cast(Any, runtime))

    await _title_first_message(service, content)

    metadata = _metadata(runtime)
    # The local title collapses whitespace and is capped at 40 characters.
    assert metadata["auto_title"] == "Investigate login failures in productio…"
    assert metadata["auto_title_initialized"] is True
    assert runtime.adapter_calls == []


@pytest.mark.asyncio
async def test_closed_session_database_skips_the_title_with_a_warning(
    manager, caplog: pytest.LogCaptureFixture
) -> None:
    runtime = StubRuntime(manager, enabled=True, configured_model="openai/title::main")
    _append_first_user(runtime, "Investigate login failures")
    runtime.chat_sessions.close()
    service = SessionTitleService(cast(Any, runtime))

    with caplog.at_level(logging.WARNING, logger="vbot.sessions.titles"):
        await _title_first_message(service, "Investigate login failures")

    assert "Session title initialization failed" in caplog.text
    assert runtime.adapter_calls == []


@pytest.mark.asyncio
async def test_configured_title_model_replaces_local_title_with_bounded_request(manager) -> None:
    adapter = StubAdapter('Title: "Review report".')
    runtime = StubRuntime(
        manager,
        enabled=True,
        configured_model="openai/title::cheap",
        adapters=[adapter],
    )
    secret = "must-not-reach-title-model"
    content: list[ContentBlock] = [
        TextBlock(type="text", text="Please review\n" + "x" * 7000 + "\nand summarize risks"),
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
    _append_first_user(runtime, content)
    service = SessionTitleService(cast(Any, runtime))

    await _title_first_message(service, content)

    assert runtime.adapter_calls == [("openai", "openai:cheap")]
    request = adapter.requests[0]
    assert len(request["messages"]) == 2
    assert request["messages"][0] == {"role": "system", "content": TITLE_SYSTEM_PROMPT}
    assert request["messages"][1]["role"] == "user"
    # A bounded projection of the text; attachments are named, never included.
    title_input = request["messages"][1]["content"]
    assert TITLE_OMISSION_MARKER in title_input
    assert "report.pdf (application/pdf)" in title_input
    assert "private.txt (mentioned file, 999 bytes)" in title_input
    assert secret not in title_input
    text_bound = TITLE_INPUT_HEAD_BYTES + TITLE_INPUT_TAIL_BYTES
    assert len(title_input.encode("utf-8")) <= text_bound + 200
    assert "max_tokens" not in request
    assert request["thinking_effort"] == "none"
    assert request["temperature"] is None
    assert _metadata(runtime)["auto_title"] == "Review report"
    assert adapter.closed is True
    assert adapter.debug_context.run_id == "title-run-one"


@pytest.mark.asyncio
async def test_reasoning_mandatory_endpoint_retries_with_default_effort(manager) -> None:
    """A rejected explicit disable retries once at the provider-default effort."""

    class RejectingAdapter(StubAdapter):
        def request_context_kwargs(self, **kwargs: Any) -> dict[str, Any]:
            assert kwargs == {
                "agent_id": "coder",
                "session_id": "session-one",
                "project_id": None,
            }
            return {"routing_sentinel": "title-session"}

        async def send(self, messages: list[dict], **kwargs: Any) -> dict[str, Any]:
            self.requests.append({"messages": messages, **kwargs})
            if kwargs.get("thinking_effort") == "none":
                raise ProviderError("Reasoning is mandatory for this endpoint.")
            return {"content": self.title, "usage": {"input_tokens": 50, "output_tokens": 5}}

    adapter = RejectingAdapter('Title: "Mandatory reasoning"')
    runtime = StubRuntime(
        manager,
        enabled=True,
        configured_model="openrouter/stealth/ox-alpha::api-key",
        adapters=[adapter],
    )
    recorder = RecordingUsageRecorder()
    _append_first_user(runtime, "Please fix the scroll bug")
    service = SessionTitleService(cast(Any, runtime), usage_recorder=cast(Any, recorder))

    await _title_first_message(service, "Please fix the scroll bug")

    assert [request["thinking_effort"] for request in adapter.requests] == ["none", ""]
    assert [request["routing_sentinel"] for request in adapter.requests] == [
        "title-session",
        "title-session",
    ]
    assert adapter.requests[0]["messages"] == adapter.requests[1]["messages"]
    assert adapter.requests[1]["messages"][0] == {
        "role": "system",
        "content": TITLE_SYSTEM_PROMPT,
    }
    assert _metadata(runtime)["auto_title"] == "Mandatory reasoning"
    assert adapter.closed is True
    # Both attempts are accounted: the rejected one as failed, without usage.
    failed, completed = recorder.calls
    assert (failed["status"], failed["usage"]) == ("failed", {"usage_call_id": failed["id"]})
    assert completed["kind"] == "session_title"
    assert completed["model"] == "openrouter/stealth/ox-alpha"
    assert completed["connection_id"] == "openrouter:api-key"
    assert completed["group_id"] is None
    assert (completed["usage"]["input_tokens"], completed["usage"]["output_tokens"]) == (50, 5)


@pytest.mark.asyncio
async def test_generated_title_uses_model_recommended_temperature(manager) -> None:
    adapter = StubAdapter('Title: "Audit".')
    runtime = StubRuntime(
        manager,
        enabled=True,
        configured_model="ollama-cloud/glm-5.2::cloud",
        adapters=[adapter],
        recommended_temperatures={("ollama-cloud", "glm-5.2"): 1.0},
    )
    _append_first_user(runtime, "Audit the workspace")
    service = SessionTitleService(cast(Any, runtime))

    await _title_first_message(service, "Audit the workspace")

    assert adapter.requests[0]["temperature"] == 1.0


# The parser carries its own contract: it must pick exactly one unambiguous
# title out of free-form Model output, so its cases are listed here directly.
@pytest.mark.parametrize(
    "response",
    [
        # Final content only; Reasoning never becomes the title.
        {
            "content": "Session naming audit",
            "reasoning": "The user is asking me to inspect the naming agent",
            "reasoning_meta": {"reasoning_details": [{"text": "Internal analysis"}]},
        },
        {
            "content": [
                {"type": "reasoning", "text": "The user wants a title"},
                {"type": "text", "text": "Session naming audit"},
            ]
        },
        # Unambiguous plain-text wrappers.
        {"content": "```\nSession naming audit\n```"},
        {"content": "~~~plaintext\r\nSession naming audit\r\n~~~"},
        {"content": "Title:\nSession naming audit"},
        {"content": 'Titel:\n"Session naming audit"'},
        {"content": "<think>Hidden\nanalysis</think>\n```\nSession naming audit\n```"},
        {"content": "```text\nTitle:\nSession naming audit\n```"},
        # One explicit block, with straight or typographic quotes.
        {"content": "[title=Session naming audit]"},
        {"content": '[title="Session naming audit"]'},
        {"content": "[title=„Session naming audit“]"},
        {"content": "[title=«Session naming audit»]"},
        {"content": "[ TITLE =  'Session naming audit'  ]"},
        {"content": '"[title=Session naming audit]"'},
        {"content": "Explanation before.\n[title=Session naming audit]\nExplanation after."},
        {"content": "```text\n[title=Session naming audit]\n```"},
        {"content": "<think>[title=Discarded draft]</think>[title=Session naming audit]"},
        {"content": 'Draft: `[title="Session naming audit"]`</think>[title=Session naming audit]'},
        {"content": "[title=Session naming audit][title=Session naming audit]"},
    ],
)
def test_generated_title_extracts_one_unambiguous_title(response: dict[str, Any]) -> None:
    assert _generated_title(response) == "Session naming audit"


def test_generated_title_preserves_internal_quotes_and_counts_only_the_title() -> None:
    assert _generated_title({"content": """[title="Fix O'Brien's parser"]"""}) == (
        "Fix O'Brien's parser"
    )
    longest = "x" * GENERATED_TITLE_MAX_CHARACTERS
    assert _generated_title({"content": longest}) == longest
    assert _generated_title({"content": f"[title={longest}]"}) == longest


@pytest.mark.parametrize(
    "response",
    [
        # Invalid explicit blocks.
        "[title=First][title=Second]",
        "[title=Unfinished",
        "[title=First] [title=Unfinished",
        "[title=Outer [title=Inner]]",
        "[title=Nested [brackets]]",
        "[title=]",
        '[title=""]',
        "[title=First\nSecond]",
        "[title=" + "x" * (GENERATED_TITLE_MAX_CHARACTERS + 1) + "]",
        # Ambiguous, unfinished or empty plain text.
        "First candidate\nSecond candidate",
        "Here is your title:\nSession naming audit",
        "Session naming audit\nThis title summarizes the request.",
        "```\nFirst candidate\nSecond candidate\n```",
        "```\nSession naming audit\n~~~",
        "```\nSession naming audit",
        "```\n```",
        "Title:\n",
        "",
        "<think>The user is asking me to perform a naming audit",
        "x" * (GENERATED_TITLE_MAX_CHARACTERS + 1),
        # Descriptions of the request instead of a title.
        "The user is asking me to perform a session naming audit",
        "Was macht der User in dieser Session",
    ],
)
def test_generated_title_rejects_invalid_ambiguous_or_meta_output(response: str) -> None:
    with pytest.raises(ValueError):
        _generated_title({"content": response})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "accepted"),
    [
        ('Explanation\n[title="Session naming audit"]\nDone.', True),
        ("private-candidate-one\nprivate-candidate-two", False),
        ("<think>The user is asking me to perform a naming audit", False),
    ],
)
async def test_title_validation_preserves_fallback_and_logs_without_content_or_trace(
    manager, caplog: pytest.LogCaptureFixture, response: str, accepted: bool
) -> None:
    adapter = StubAdapter(response)
    runtime = StubRuntime(manager, enabled=True, adapters=[adapter])
    _append_first_user(runtime, "Inspect session naming")
    service = SessionTitleService(cast(Any, runtime))
    caplog.set_level(logging.WARNING, logger="vbot.sessions.titles")

    await _title_first_message(service, "Inspect session naming")

    metadata = _metadata(runtime)
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
        assert response not in record.getMessage()


@pytest.mark.asyncio
async def test_empty_title_model_selection_uses_resolved_agent_model(manager) -> None:
    adapter = StubAdapter("Agent-generated title")
    runtime = StubRuntime(manager, enabled=True, configured_model="", adapters=[adapter])
    _append_first_user(runtime, "Investigate login failure")
    service = SessionTitleService(cast(Any, runtime))

    await _title_first_message(service, "Investigate login failure")

    assert runtime.adapter_calls == [("openai", "openai:main")]
    assert adapter.requests[0]["model_id"] == "agent"
    assert _metadata(runtime)["auto_title"] == "Agent-generated title"


@pytest.mark.asyncio
async def test_failed_configured_model_keeps_local_title_without_agent_retry(
    manager, caplog: pytest.LogCaptureFixture
) -> None:
    adapter = StubAdapter(error=RuntimeError("provider down"))
    runtime = StubRuntime(
        manager,
        enabled=True,
        configured_model="openai/title::cheap",
        adapters=[adapter],
    )
    _append_first_user(runtime, "Investigate login failure")
    service = SessionTitleService(cast(Any, runtime))

    await _title_first_message(service, "Investigate login failure")

    assert runtime.adapter_calls == [("openai", "openai:cheap")]
    assert _metadata(runtime)["auto_title"] == "Investigate login failure"
    records = [record for record in caplog.records if record.name == "vbot.sessions.titles"]
    assert len(records) == 1
    assert records[0].exc_info is not None
    assert adapter.closed is True


@pytest.mark.asyncio
async def test_existing_session_is_marked_without_backfill_or_model_request(
    manager, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = StubRuntime(manager, enabled=True, adapters=[])
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

    _notify(service, "New request", run_id="run-two")
    await _wait_for_background(service)

    metadata = _metadata(runtime)
    assert metadata["auto_title_initialized"] is True
    assert "auto_title" not in metadata
    assert runtime.adapter_calls == []


@pytest.mark.asyncio
async def test_manual_name_skips_model_but_preserves_local_title_underneath(manager) -> None:
    runtime = StubRuntime(manager, enabled=True, adapters=[])
    _append_first_user(runtime, "Investigate login failure")
    runtime.chat_sessions.set_title(_address("coder", "session-one"), "Manual name")
    service = SessionTitleService(cast(Any, runtime))

    await _title_first_message(service, "Investigate login failure")

    metadata = _metadata(runtime)
    assert metadata["title"] == "Manual name"
    assert metadata["auto_title"] == "Investigate login failure"
    assert runtime.adapter_calls == []


def _participant(participant_id: str, model: str | None) -> OwnedSessionSummary:
    return OwnedSessionSummary(
        address=_address(f"tmp_{participant_id}", f"ses_{participant_id}"),
        owner_name="swarm",
        group_id="swr_one",
        group_title=None,
        participant_id=participant_id,
        participant_name=participant_id.title(),
        model=model,
        summary={},
    )


async def _group_title(
    runtime: StubRuntime, *participants: OwnedSessionSummary, usage_recorder: Any = None
) -> str | None:
    service = SessionTitleService(cast(Any, runtime), usage_recorder=usage_recorder)
    return await service.generate_group_title(
        owner_name="swarm",
        group_id="swr_one",
        source_text="  Rework the\n parser   error recovery  ",
        participants=participants,
    )


async def _stored_group_title(runtime: StubRuntime) -> str | None:
    titles = await runtime.chat_sessions.temporary_group_titles_async(
        owner_name="swarm", group_ids=["swr_one"]
    )
    return titles.get("swr_one")


@pytest.mark.asyncio
async def test_group_title_uses_configured_title_model_for_the_group(manager) -> None:
    adapter = StubAdapter("Parser recovery rework")
    runtime = StubRuntime(
        manager, enabled=True, configured_model="openai/title::cheap", adapters=[adapter]
    )

    title = await _group_title(runtime, _participant("ada", "anthropic/big::main"))

    assert title == "Parser recovery rework"
    assert await _stored_group_title(runtime) == "Parser recovery rework"
    assert runtime.adapter_calls == [("openai", "openai:cheap")]
    assert adapter.requests[0]["messages"][0] == {"role": "system", "content": TITLE_SYSTEM_PROMPT}
    assert adapter.debug_context.run_id == "title-swr_one"
    assert adapter.debug_context.session_id == "ses_ada"
    assert adapter.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("prices", "expected"),
    [
        # The cheapest priced participant Model wins.
        ({"anthropic/big": (3.0, 15.0), "openai/small": (0.1, 0.4)}, ("openai", "openai:fast")),
        # Without prices, the first participant that has a Model.
        ({}, ("google", "google:main")),
    ],
    ids=["priced", "unpriced"],
)
async def test_group_title_without_title_model_picks_a_participant_model(
    manager, prices, expected
) -> None:
    runtime = StubRuntime(
        manager, enabled=True, adapters=[StubAdapter("Parser recovery rework")], prices=prices
    )

    await _group_title(
        runtime,
        _participant("none", None),
        _participant("unpriced", "google/unpriced::main"),
        _participant("big", "anthropic/big::main"),
        _participant("small", "openai/small::fast"),
    )

    assert runtime.adapter_calls == [expected]


@pytest.mark.asyncio
async def test_disabled_group_title_generation_stores_local_title_only(manager) -> None:
    runtime = StubRuntime(manager, enabled=False, adapters=[])

    title = await _group_title(runtime, _participant("ada", "openai/small::main"))

    assert title == "Rework the parser error recovery"
    assert await _stored_group_title(runtime) == "Rework the parser error recovery"
    assert runtime.adapter_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_title", [False, True], ids=["provider-error", "invalid-title"])
async def test_failed_group_title_keeps_local_title_after_one_accounted_attempt(
    manager, invalid_title
) -> None:
    if invalid_title:
        adapter = StubAdapter("[title=]", usage={"input_tokens": 50, "output_tokens": 5})
    else:
        adapter = StubAdapter(error=RuntimeError("provider down"))
    runtime = StubRuntime(manager, enabled=True, adapters=[adapter, StubAdapter()])
    recorder = RecordingUsageRecorder()

    title = await _group_title(
        runtime,
        _participant("ada", "openai/a::main"),
        _participant("bo", "openai/b::main"),
        usage_recorder=recorder,
    )

    assert title == "Rework the parser error recovery"
    assert await _stored_group_title(runtime) == "Rework the parser error recovery"
    assert runtime.adapter_calls == [("openai", "openai:main")]
    (call,) = recorder.calls
    assert (call["kind"], call["group_id"]) == ("group_title", "swr_one")
    if invalid_title:
        assert (call["usage"]["input_tokens"], call["usage"]["output_tokens"]) == (50, 5)
    else:
        assert call["status"] == "failed"
