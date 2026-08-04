"""Chat-loop tests grouped by requests."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from core.automation import TriggerService
from core.chat import (
    INPUT_ORIGIN_SPEECH_TRANSCRIPTION,
    ChatMessage,
    MessageSender,
    ReplySurface,
)
from core.runs import (
    MODEL_STEP_USAGE_EVENT,
)
from core.tools import JsonObject as ToolJsonObject
from core.tools import (
    ToolContext,
    ToolRegistry,
    tool_success,
)
from tests.core.chat.chat_loop_support import (
    BlockingStubAdapter,
    StubAdapter,
    StubAgent,
    StubPrompts,
    StubRuntime,
    build_chat_loop,
    persisted_roles,
)

JsonObject = dict[str, Any]


@pytest.mark.asyncio
async def test_send_appends_user_and_final_assistant_without_tools(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openrouter/anthropic/claude-sonnet-4", allowed_tools=["*"])
    adapter = StubAdapter([{"content": "Hello", "reasoning": None, "tool_calls": None}])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)

    assistant = await build_chat_loop(runtime).send("coder", "Hi", session_id="session-one")

    session = runtime.chat_sessions.get("coder", "session-one")
    messages = session.load()
    assert assistant.content == "Hello"
    assert persisted_roles(messages) == ["user", "assistant"]
    assert messages[0].content == "Hi"
    assert messages[1].content == "Hello"
    assert runtime.adapter_provider_id == "openrouter"
    assert runtime.adapter_connection_id == "openrouter:api-key"
    assert adapter.requests[0]["model_id"] == "anthropic/claude-sonnet-4"
    assert adapter.requests[0]["kwargs"] == {
        "temperature": 0.1,
        "thinking_effort": "high",
        "tools": [
            {
                "name": "get_weather",
                "description": "Get weather.",
                "parameters": {"type": "object"},
            }
        ],
    }
    assert [message["role"] for message in adapter.requests[0]["messages"]] == ["system", "user"]
    run = next(iter(runtime.chat_runs._runs.values()))
    assert [event.type for event in run.events] == [
        "run_started",
        "user_message_persisted",
        MODEL_STEP_USAGE_EVENT,
        "assistant_output",
        "run_completed",
    ]
    assert run.events[1].payload["message"]["content"] == "Hi"
    assert run.events[3].payload["message"]["content"] == "Hello"


@pytest.mark.asyncio
async def test_send_logs_run_start_and_end_lines(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    agent = StubAgent(id="coder", model="openrouter/anthropic/claude-sonnet-4", allowed_tools=["*"])
    adapter = StubAdapter([{"content": "Hello", "reasoning": None, "tool_calls": None}])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)

    with caplog.at_level("INFO", logger="vbot.chat"):
        await build_chat_loop(runtime).send("coder", "Hi", session_id="session-one")

    run = next(iter(runtime.chat_runs._runs.values()))
    log_messages = [record.getMessage() for record in caplog.records]
    start_line = next(
        message for message in log_messages if message.startswith(f"Run {run.id} started")
    )
    assert "agent=coder" in start_line
    assert "session=session-one" in start_line
    assert "model=openrouter/anthropic/claude-sonnet-4" in start_line
    assert "connection=openrouter:api-key" in start_line
    end_line = next(
        message for message in log_messages if message.startswith(f"Run {run.id} completed")
    )
    assert "agent=coder" in end_line
    assert "session=session-one" in end_line
    assert "duration_ms=" in end_line
    assert "iterations=1" in end_line
    assert "tool_calls=0" in end_line
    assert "input_tokens=" in end_line
    assert "output_tokens=" in end_line


@pytest.mark.asyncio
async def test_send_omits_empty_system_prompt(tmp_path: Path) -> None:
    class EmptySystemPrompts(StubPrompts):
        def build_system_prompt(
            self,
            agent: StubAgent,
            scope: Any = None,
            *,
            agent_body: str = "",
            project_context: Any = None,
            working_project_context: str | None = None,
            agent_project_id: str | None = None,
            nesting_depth: int = 0,
            skill_registry: Any = None,
            skill_catalog: Any = None,
            read_paths: list[Path] | None = None,
            effective_tool_names: Any = None,
            session_tool_grants: Any = (),
        ) -> str:
            del agent_project_id
            return "\n"

    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter([{"content": "Hello", "tool_calls": None}])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    runtime.system_prompts = EmptySystemPrompts()

    await build_chat_loop(runtime).send("coder", "Hi", session_id="session-one")

    request_messages = adapter.requests[0]["messages"]
    assert [message["role"] for message in request_messages] == ["user"]
    assert request_messages[0]["content"] == "Hi"


@pytest.mark.asyncio
async def test_note_before_user_turn_is_embedded_as_synthetic_user_message(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter([{"content": "Hello", "tool_calls": None}])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    session.add_note("Background job completed")

    await build_chat_loop(runtime).send("coder", "Hi", session_id="session-one")

    request_messages = adapter.requests[0]["messages"]
    assert [message["role"] for message in request_messages] == ["system", "user", "user"]
    assert request_messages[1] == {
        "role": "user",
        "content": "<system-reminder>\nBackground job completed\n</system-reminder>",
    }
    assert request_messages[2]["content"] == "Hi"
    assert all(message["role"] != "note" for message in request_messages)


@pytest.mark.asyncio
async def test_speech_transcription_origin_adds_system_reminder_before_user_turn(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter([{"content": "Hello", "tool_calls": None}])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    runtime.chat_sessions.create("coder", session_id="session-one")

    await build_chat_loop(runtime).send(
        "coder",
        "helo wrld",
        session_id="session-one",
        input_origin=INPUT_ORIGIN_SPEECH_TRANSCRIPTION,
    )

    messages = runtime.chat_sessions.get("coder", "session-one").load()
    request_messages = adapter.requests[0]["messages"]
    assert persisted_roles(messages) == ["note", "user", "assistant"]
    assert "speech-to-text transcription" in str(messages[0].content)
    assert messages[1].content == "helo wrld"
    assert [message["role"] for message in request_messages] == ["system", "user", "user"]
    assert "speech-to-text transcription" in request_messages[1]["content"]
    assert request_messages[2]["content"] == "helo wrld"


@pytest.mark.asyncio
async def test_reply_surface_note_follows_speech_note_and_precedes_user_turn(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter([{"content": "Hello", "tool_calls": None}])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    runtime.chat_sessions.create("coder", session_id="session-one")

    run = await build_chat_loop(runtime).start_run(
        "coder",
        "helo wrld",
        session_id="session-one",
        input_origin=INPUT_ORIGIN_SPEECH_TRANSCRIPTION,
        reply_surface=ReplySurface.webui(),
    )
    await run.wait()

    messages = runtime.chat_sessions.get("coder", "session-one").load()
    assert persisted_roles(messages) == ["note", "note", "user", "assistant"]
    assert "speech-to-text transcription" in str(messages[0].content)
    assert str(messages[1].content).startswith("[reply-surface] ")
    assert messages[2].content == "helo wrld"
    reminder_text = adapter.requests[0]["messages"][1]["content"]
    assert reminder_text.index("speech-to-text transcription") < reminder_text.index(
        "shown in the WebUI"
    )


@pytest.mark.asyncio
async def test_reply_surface_initial_repeat_and_switches_follow_execution_chronology(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter(
        [
            {"content": "One", "tool_calls": None},
            {"content": "Two", "tool_calls": None},
            {"content": "Three", "tool_calls": None},
            {"content": "Four", "tool_calls": None},
        ]
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    runtime.chat_sessions.create("coder", session_id="session-one")
    loop = build_chat_loop(runtime)
    webui = ReplySurface.webui()
    telegram = ReplySurface.channel(
        platform="telegram",
        platform_display_name="Telegram",
        channel_id="tg-main",
    )

    for content, surface in (
        ("first", webui),
        ("same", webui),
        ("channel", telegram),
        ("back", webui),
    ):
        run = await loop.start_run(
            "coder", content, session_id="session-one", reply_surface=surface
        )
        await run.wait()

    messages = runtime.chat_sessions.get("coder", "session-one").load()
    surface_note_indexes = [
        index
        for index, message in enumerate(messages)
        if message.role == "note" and str(message.content).startswith("[reply-surface] ")
    ]
    user_indexes = [index for index, message in enumerate(messages) if message.role == "user"]
    assert len(surface_note_indexes) == 3
    assert surface_note_indexes == [
        user_indexes[0] - 1,
        user_indexes[2] - 1,
        user_indexes[3] - 1,
    ]


@pytest.mark.asyncio
async def test_first_same_surface_run_after_compaction_appends_one_fresh_note(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter(
        [
            {"content": "One", "tool_calls": None},
            {"content": "Two", "tool_calls": None},
            {"content": "Three", "tool_calls": None},
        ]
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    loop = build_chat_loop(runtime)
    surface = ReplySurface.webui()

    first = await loop.start_run("coder", "first", session_id="session-one", reply_surface=surface)
    await first.wait()
    session.append(
        ChatMessage.compaction_checkpoint(
            summary="Earlier work.",
            projection=[],
            compacted_token_count=10,
        )
    )
    second = await loop.start_run(
        "coder", "after compact", session_id="session-one", reply_surface=surface
    )
    await second.wait()
    third = await loop.start_run(
        "coder", "same again", session_id="session-one", reply_surface=surface
    )
    await third.wait()

    surface_notes = [
        message
        for message in session.load()
        if message.role == "note" and str(message.content).startswith("[reply-surface] ")
    ]
    assert len(surface_notes) == 2


@pytest.mark.asyncio
async def test_internal_start_run_embeds_content_without_visible_user_message(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter([{"content": "Continuing parent work", "tool_calls": None}])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    runtime.chat_sessions.create("coder", session_id="session-one")
    content = "Sub-agent batch completed.\n\nResults:\n- worker/sub-session: Done"

    run = await build_chat_loop(runtime).start_run(
        "coder",
        content,
        session_id="session-one",
        internal=True,
    )
    await run.wait()

    messages = runtime.chat_sessions.get("coder", "session-one").load()
    request_messages = adapter.requests[0]["messages"]
    assert persisted_roles(messages) == ["note", "assistant"]
    assert messages[0].content == content
    assert [event.type for event in run.events] == [
        "run_started",
        MODEL_STEP_USAGE_EVENT,
        "assistant_output",
        "run_completed",
    ]
    assert all(event.type != "user_message_persisted" for event in run.events)
    assert request_messages[1] == {
        "role": "user",
        "content": f"<system-reminder>\n{content}\n</system-reminder>",
    }
    assert all(message["role"] != "note" for message in request_messages)


@pytest.mark.asyncio
async def test_internal_interactive_run_places_surface_immediately_before_prompt(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter([{"content": "Hello", "tool_calls": None}])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    runtime.chat_sessions.create("coder", session_id="session-one")
    prompt = "Greet the user who opened this Channel."
    surface = ReplySurface.channel(
        platform="telegram",
        platform_display_name="Telegram",
        channel_id="tg-main",
    )

    run = await build_chat_loop(runtime).start_run(
        "coder",
        prompt,
        session_id="session-one",
        internal=True,
        reply_surface=surface,
    )
    await run.wait()

    messages = runtime.chat_sessions.get("coder", "session-one").load()
    assert persisted_roles(messages) == ["note", "note", "assistant"]
    assert str(messages[0].content).startswith("[reply-surface] ")
    assert messages[1].content == prompt
    request_content = adapter.requests[0]["messages"][1]["content"]
    assert request_content.index("delivered via Telegram") < request_content.index(prompt)


@pytest.mark.asyncio
async def test_queued_cross_surface_run_decides_when_it_actually_starts(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = BlockingStubAdapter()
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    runtime.chat_sessions.create("coder", session_id="session-one")
    loop = build_chat_loop(runtime)
    channel_surface = ReplySurface.channel(
        platform="telegram",
        platform_display_name="Telegram",
        channel_id="tg-main",
    )

    first_run = await loop.start_run(
        "coder",
        "web request",
        session_id="session-one",
        reply_surface=ReplySurface.webui(),
    )
    await adapter.request_started.wait()
    queued = await loop.queue_run(
        "coder",
        "channel request",
        session_id="session-one",
        reply_surface=channel_surface,
    )
    adapter.release.set()
    await first_run.wait()
    second_run = await queued.future
    await second_run.wait()

    messages = runtime.chat_sessions.get("coder", "session-one").load()
    surface_notes = [
        message
        for message in messages
        if message.role == "note" and str(message.content).startswith("[reply-surface] ")
    ]
    assert len(surface_notes) == 2
    assert "webui" in str(surface_notes[0].content)
    assert "telegram" in str(surface_notes[1].content)


@pytest.mark.asyncio
async def test_start_run_persists_sender_and_renders_request_attribution(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter([{"content": "Hello Alice", "tool_calls": None}])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    runtime.chat_sessions.create("coder", session_id="session-one")
    sender = MessageSender(id="50", display_name="Alice")

    run = await build_chat_loop(runtime).start_run(
        "coder",
        "Hi",
        session_id="session-one",
        sender=sender,
    )
    await run.wait()

    messages = runtime.chat_sessions.get("coder", "session-one").load()
    request_messages = adapter.requests[0]["messages"]
    assert persisted_roles(messages) == ["user", "assistant"]
    assert messages[0].sender == sender
    assert messages[0].content == "Hi"
    assert request_messages[1]["content"] == "[Alice|50|member]: Hi"
    assert all("sender" not in message for message in request_messages)
    persisted_event = next(event for event in run.events if event.type == "user_message_persisted")
    assert persisted_event.payload["message"]["sender"] == {
        "id": "50",
        "display_name": "Alice",
        "role": "member",
    }


@pytest.mark.asyncio
async def test_queue_run_persists_sender_on_user_message(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter([{"content": "Hello Alice", "tool_calls": None}])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    runtime.chat_sessions.create("coder", session_id="session-one")
    sender = MessageSender(id="50", display_name="Alice")

    queued_item = await build_chat_loop(runtime).queue_run(
        "coder",
        "Hi",
        session_id="session-one",
        sender=sender,
    )
    run = await queued_item.future
    await run.wait()

    messages = runtime.chat_sessions.get("coder", "session-one").load()
    assert persisted_roles(messages) == ["user", "assistant"]
    assert messages[0].sender == sender
    assert messages[0].content == "Hi"


@pytest.mark.asyncio
async def test_multiple_consecutive_notes_are_embedded_as_one_synthetic_user_message(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter([{"content": "Hello", "tool_calls": None}])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    session.add_note("First background event")
    session.add_note("Second background event")

    await build_chat_loop(runtime).send("coder", "Hi", session_id="session-one")

    request_messages = adapter.requests[0]["messages"]
    assert [message["role"] for message in request_messages] == ["system", "user", "user"]
    assert request_messages[1] == {
        "role": "user",
        "content": (
            "<system-reminder>\nFirst background event\n</system-reminder>\n"
            "<system-reminder>\nSecond background event\n</system-reminder>"
        ),
    }
    assert all(message["role"] != "note" for message in request_messages)


@pytest.mark.asyncio
async def test_notes_and_visible_errors_are_embedded_as_system_reminders(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter([{"content": "Hello", "tool_calls": None}])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    session.append(ChatMessage.note("Background event"))
    session.append(ChatMessage.error("rate_limit", "Provider rate limited the previous run"))
    session.append(ChatMessage.error("auth_error", "Invalid provider credential"))

    await build_chat_loop(runtime).send("coder", "Hi", session_id="session-one")

    request_messages = adapter.requests[0]["messages"]
    request_text = "\n".join(message.get("content", "") or "" for message in request_messages)
    assert "<system-reminder>\nBackground event\n</system-reminder>" in request_text
    assert (
        "<system-reminder>\nProvider rate limited the previous run\n</system-reminder>"
        in request_text
    )
    assert "Invalid provider credential" not in request_text
    assert all(message["role"] != "error" for message in request_messages)


@pytest.mark.asyncio
async def test_note_added_between_tool_iterations_is_sent_on_next_request(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["record_note"])
    adapter = StubAdapter(
        [
            {
                "content": None,
                "tool_calls": [{"id": "call_1", "name": "record_note", "arguments": {}}],
            },
            {"content": "Saw reminder", "tool_calls": None},
        ]
    )

    def record_note(context: ToolContext, _arguments: ToolJsonObject) -> ToolJsonObject:
        context.add_note("Tool finished background work")
        return tool_success({"ok": True})

    tools = ToolRegistry()
    tools.register("record_note", "Record note.", {"type": "object"}, record_note)
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, tools=tools)

    await build_chat_loop(runtime).send("coder", "Run tool", session_id="session-one")

    second_request_messages = adapter.requests[1]["messages"]
    assert [message["role"] for message in second_request_messages] == [
        "system",
        "user",
        "assistant",
        "tool",
        "user",
    ]
    assert second_request_messages[-1] == {
        "role": "user",
        "content": "<system-reminder>\nTool finished background work\n</system-reminder>",
    }
    assert all(
        message["role"] != "note" for request in adapter.requests for message in request["messages"]
    )


@pytest.mark.asyncio
async def test_background_completion_joins_next_request_in_same_run(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["start_work"])
    adapter = StubAdapter(
        [
            {
                "content": None,
                "tool_calls": [{"id": "call_1", "name": "start_work", "arguments": {}}],
            },
            {"content": "Used the completed work", "tool_calls": None},
        ]
    )
    deliveries: list[asyncio.Future[None]] = []
    tools = ToolRegistry()
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, tools=tools)
    chat_loop = build_chat_loop(runtime)
    trigger_service = TriggerService(
        chat_loop,
        runtime.chat_run_manager,
        runtime,
        trigger_chat_loop=chat_loop,
        sessions=runtime.chat_sessions,
    )
    runtime.deliver_background_completions = trigger_service.deliver_background_completions

    def start_work(context: ToolContext, _arguments: ToolJsonObject) -> ToolJsonObject:
        deliveries.append(
            trigger_service.submit_completion(
                "coder",
                "session-one",
                notice_id="bash:completed",
                origin_run_id=context.run_id,
                body="### Bash process — completed\nBuild finished successfully.",
            )
        )
        return tool_success({"status": "running"})

    tools.register("start_work", "Start background work.", {"type": "object"}, start_work)

    await chat_loop.send("coder", "Start it", session_id="session-one")
    await asyncio.wait_for(deliveries[0], timeout=1)
    await asyncio.sleep(0)

    assert len(adapter.requests) == 2
    second_request_messages = adapter.requests[1]["messages"]
    assert [message["role"] for message in second_request_messages] == [
        "system",
        "user",
        "assistant",
        "tool",
        "user",
    ]
    assert second_request_messages[-1]["content"].startswith(
        "<system-reminder>\nAutomatic completion delivery — this is not a new user request."
    )
    assert "Build finished successfully." in second_request_messages[-1]["content"]


@pytest.mark.asyncio
async def test_note_added_during_tool_dispatch_is_persisted_after_tool_results(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["record_note"])
    adapter = StubAdapter(
        [
            {
                "content": None,
                "tool_calls": [{"id": "call_1", "name": "record_note", "arguments": {}}],
            },
            {"content": "First turn complete", "tool_calls": None},
            {"content": "Second turn complete", "tool_calls": None},
        ]
    )

    def record_note(context: ToolContext, _arguments: ToolJsonObject) -> ToolJsonObject:
        context.add_note("Tool finished background work")
        return tool_success({"ok": True})

    tools = ToolRegistry()
    tools.register("record_note", "Record note.", {"type": "object"}, record_note)
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, tools=tools)

    await build_chat_loop(runtime).send("coder", "Run tool", session_id="session-one")

    persisted_after_first_turn = runtime.chat_sessions.get("coder", "session-one").load()
    assert persisted_roles(persisted_after_first_turn) == [
        "user",
        "assistant",
        "tool",
        "note",
        "assistant",
    ]
    assert persisted_after_first_turn[3].content == "Tool finished background work"

    await build_chat_loop(runtime).send("coder", "Follow up", session_id="session-one")

    second_turn_request = adapter.requests[2]["messages"]
    assert [message["role"] for message in second_turn_request] == [
        "system",
        "user",
        "assistant",
        "tool",
        "user",
        "assistant",
        "user",
    ]
    assert second_turn_request[4] == {
        "role": "user",
        "content": "<system-reminder>\nTool finished background work\n</system-reminder>",
    }


@pytest.mark.asyncio
async def test_request_messages_without_notes_keep_existing_shape(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter([{"content": "Hello", "tool_calls": None}])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)

    await build_chat_loop(runtime).send("coder", "Hi", session_id="session-one")

    request_messages = adapter.requests[0]["messages"]
    assert [message["role"] for message in request_messages] == ["system", "user"]
    assert request_messages[0]["content"] == "System for coder"
    assert request_messages[1]["content"] == "Hi"
    assert all(message["role"] != "note" for message in request_messages)
