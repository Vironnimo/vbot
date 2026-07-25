"""Chat-loop tests grouped by skills."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

import pytest

from core.chat import (
    ChatMessage,
)
from core.chat.messages import _notes_to_synthetic_user_message
from core.sessions import SKILL_AVAILABLE_NOTE_PREFIX, is_skill_available_note
from core.skills.skills import SkillRegistry
from tests.core.chat.chat_loop_support import (
    StubAdapter,
    StubAgent,
    StubRuntime,
    StubSkill,
    StubSkills,
    _write_test_skill,
    build_chat_loop,
    persisted_roles,
)

JsonObject = dict[str, Any]


@pytest.mark.asyncio
async def test_slash_skill_trigger_activates_before_provider_request(tmp_path: Path) -> None:
    skill_file = _write_test_skill(tmp_path, "debugging")
    agent = StubAgent(
        id="coder",
        model="openai/gpt-5.2",
        allowed_tools=["*"],
        allowed_skills=["debugging"],
    )
    adapter = StubAdapter([{"content": "Hello", "tool_calls": None}])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    runtime.skills = StubSkills([StubSkill("debugging", "Debug failures", skill_file)])

    await build_chat_loop(runtime).send("coder", "/debugging fix this", session_id="session-one")

    request_messages = adapter.requests[0]["messages"]
    # The skill content sits directly under the triggering user message — in
    # place, not hoisted to the front of the request.
    assert request_messages[1]["content"] == "/debugging fix this"
    assert request_messages[2]["content"].startswith('<skill_content name="debugging">')
    request_text = "\n".join(message.get("content", "") or "" for message in request_messages)
    assert "[skill-context]" not in request_text
    assert "<system-reminder>\n[skill-context]" not in request_text


@pytest.mark.asyncio
async def test_skill_context_persists_across_later_sends_without_visible_user_message(
    tmp_path: Path,
) -> None:
    skill_file = _write_test_skill(tmp_path, "debugging")
    agent = StubAgent(
        id="coder",
        model="openai/gpt-5.2",
        allowed_tools=["*"],
        allowed_skills=["debugging"],
    )
    adapter = StubAdapter(
        [
            {"content": "First", "tool_calls": None},
            {"content": "Second", "tool_calls": None},
        ]
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    runtime.skills = StubSkills([StubSkill("debugging", "Debug failures", skill_file)])

    await build_chat_loop(runtime).send("coder", "/debugging fix this", session_id="session-one")
    await build_chat_loop(runtime).send("coder", "continue", session_id="session-one")

    second_request_messages = adapter.requests[1]["messages"]
    # The activation note replays at its chronological position: right after the
    # triggering user message, before the first assistant reply.
    assert second_request_messages[1]["content"] == "/debugging fix this"
    assert second_request_messages[2]["content"].startswith('<skill_content name="debugging">')
    assert second_request_messages[-1]["content"] == "continue"
    persisted_messages = runtime.chat_sessions.get("coder", "session-one").load()
    visible_messages = [message for message in persisted_messages if message.role != "note"]
    assert persisted_roles(visible_messages) == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert all(
        not (
            message.role == "user"
            and isinstance(message.content, str)
            and message.content.startswith("<skill_content ")
        )
        for message in visible_messages
    )


@pytest.mark.asyncio
async def test_inline_skill_trigger_preserves_original_message(tmp_path: Path) -> None:
    skill_file = _write_test_skill(tmp_path, "debugging")
    agent = StubAgent(
        id="coder",
        model="openai/gpt-5.2",
        allowed_tools=["*"],
        allowed_skills=["debugging"],
    )
    adapter = StubAdapter([{"content": "Hello", "tool_calls": None}])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    runtime.skills = StubSkills([StubSkill("debugging", "Debug failures", skill_file)])

    await build_chat_loop(runtime).send(
        "coder",
        "Please use $debugging on this issue",
        session_id="session-one",
    )

    request_messages = adapter.requests[0]["messages"]
    assert request_messages[1]["content"] == "Please use $debugging on this issue"
    assert request_messages[2]["content"].startswith('<skill_content name="debugging">')


def test_activated_skill_reinjected_ahead_of_compaction_summary(tmp_path: Path) -> None:
    # A skill whose carrier was folded into the summarized region stays loaded:
    # its content is re-injected ahead of the summary reminder in the rebuilt
    # request instead of being lost with the summarized history.
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=StubAdapter([]))
    session = runtime.chat_sessions.create("coder", session_id="session-one")

    session.append(ChatMessage.user("Old question"))
    session.activate_skill_context(
        "debugging", {"content": '<skill_content name="debugging">Steps</skill_content>'}
    )
    session.append(ChatMessage.assistant(model=agent.model, content="Old answer"))
    tail_user = ChatMessage.user("Tail question")
    session.append(tail_user)
    session.append(ChatMessage.assistant(model=agent.model, content="Tail answer"))
    session.append(
        ChatMessage.compaction_checkpoint(
            summary="Compacted historical context.",
            projection=session.load()[-2:],
            compacted_token_count=123,
        )
    )

    request_messages = asyncio.run(build_chat_loop(runtime)._build_request_messages(agent, session))

    contents = [message.get("content", "") or "" for message in request_messages]
    assert contents[1] == '<skill_content name="debugging">Steps</skill_content>'
    assert contents[2] == "<system-reminder>\nCompacted historical context.\n</system-reminder>"
    assert contents[3] == "Tail question"
    assert sum("<skill_content" in content for content in contents) == 1


def test_skill_carried_in_tail_not_duplicated_after_compaction(tmp_path: Path) -> None:
    # A skill activated inside the preserved tail keeps its chronological carrier
    # and must not additionally be re-injected ahead of the summary.
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=StubAdapter([]))
    session = runtime.chat_sessions.create("coder", session_id="session-one")

    session.append(ChatMessage.user("Old question"))
    session.append(ChatMessage.assistant(model=agent.model, content="Old answer"))
    tail_user = ChatMessage.user("/debugging fix this")
    session.append(tail_user)
    session.activate_skill_context(
        "debugging", {"content": '<skill_content name="debugging">Steps</skill_content>'}
    )
    session.append(ChatMessage.assistant(model=agent.model, content="Tail answer"))
    session.append(
        ChatMessage.compaction_checkpoint(
            summary="Compacted historical context.",
            projection=session.load()[-3:],
            compacted_token_count=123,
        )
    )

    request_messages = asyncio.run(build_chat_loop(runtime)._build_request_messages(agent, session))

    contents = [message.get("content", "") or "" for message in request_messages]
    assert contents[1] == "<system-reminder>\nCompacted historical context.\n</system-reminder>"
    assert contents[2] == "/debugging fix this"
    assert contents[3] == '<skill_content name="debugging">Steps</skill_content>'
    assert sum("<skill_content" in content for content in contents) == 1


def test_updated_skill_follows_stale_tail_carrier_after_compaction(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=StubAdapter([]))
    session = runtime.chat_sessions.create("coder", session_id="session-one")

    session.append(ChatMessage.user("Old question"))
    session.activate_skill_context(
        "debugging", {"content": '<skill_content name="debugging">Old steps</skill_content>'}
    )
    old_carrier = session.load()[-1]
    session.activate_skill_context(
        "debugging", {"content": '<skill_content name="debugging">New steps</skill_content>'}
    )
    session.append(
        ChatMessage.compaction_checkpoint(
            summary="Compacted historical context.",
            projection=[old_carrier],
            compacted_token_count=123,
        )
    )

    request_messages = asyncio.run(build_chat_loop(runtime)._build_request_messages(agent, session))

    contents = [message.get("content", "") or "" for message in request_messages]
    assert '<skill_content name="debugging">Old steps</skill_content>' in contents
    assert contents[-1] == '<skill_content name="debugging">New steps</skill_content>'


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    ["/debugging fix this", "Please use $debugging on this issue"],
)
async def test_skill_trigger_does_not_activate_when_allowed_skills_empty(
    tmp_path: Path,
    message: str,
) -> None:
    skill_file = _write_test_skill(tmp_path, "debugging")
    agent = StubAgent(
        id="coder",
        model="openai/gpt-5.2",
        allowed_tools=["*"],
        allowed_skills=[],
    )
    adapter = StubAdapter([{"content": "Hello", "tool_calls": None}])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    runtime.skills = StubSkills([StubSkill("debugging", "Debug failures", skill_file)])

    await build_chat_loop(runtime).send("coder", message, session_id="session-one")

    request_messages = adapter.requests[0]["messages"]
    request_text = "\n".join(message.get("content", "") or "" for message in request_messages)
    assert '<skill_content name="debugging">' not in request_text
    assert request_messages[1]["content"] == message
    assert "Skill trigger 'debugging' did not match" in request_messages[2]["content"]


@pytest.mark.asyncio
async def test_skill_trigger_does_not_activate_unavailable_skill(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    skill_dir = skills_dir / "openai-helper"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: openai-helper
description: Use OpenAI.
metadata:
    vbot:
        requirements:
            env: OPENAI_API_KEY
---

# OpenAI Helper
""",
        encoding="utf-8",
    )
    agent = StubAgent(
        id="coder",
        model="openai/gpt-5.2",
        allowed_tools=["*"],
        allowed_skills=["openai-helper"],
    )
    adapter = StubAdapter([{"content": "Hello", "tool_calls": None}])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    runtime.skills = SkillRegistry.load(skills_dir, environment={})

    await build_chat_loop(runtime).send("coder", "/openai-helper help", session_id="session-one")

    request_messages = adapter.requests[0]["messages"]
    request_text = "\n".join(message.get("content", "") or "" for message in request_messages)
    assert '<skill_content name="openai-helper">' not in request_text
    assert request_messages[1]["content"] == "/openai-helper help"
    assert "Skill trigger 'openai-helper' matched a skill, but it is unavailable" in request_text
    assert "missing environment variable 'OPENAI_API_KEY'" in request_text


@pytest.mark.asyncio
async def test_unknown_skill_trigger_adds_system_reminder(tmp_path: Path) -> None:
    agent = StubAgent(
        id="coder",
        model="openai/gpt-5.2",
        allowed_tools=["*"],
        allowed_skills=[],
    )
    adapter = StubAdapter([{"content": "Hello", "tool_calls": None}])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)

    await build_chat_loop(runtime).send("coder", "/missing do it", session_id="session-one")

    request_messages = adapter.requests[0]["messages"]
    assert request_messages[1]["content"] == "/missing do it"
    assert "Skill trigger 'missing' did not match" in request_messages[2]["content"]


@pytest.mark.asyncio
async def test_unknown_skill_trigger_reminder_appears_once_in_first_request(
    tmp_path: Path,
) -> None:
    agent = StubAgent(
        id="coder",
        model="openai/gpt-5.2",
        allowed_tools=["*"],
        allowed_skills=[],
    )
    adapter = StubAdapter([{"content": "Hello", "tool_calls": None}])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)

    await build_chat_loop(runtime).send("coder", "/missing do it", session_id="session-one")

    request_text = "\n".join(
        message.get("content", "") or "" for message in adapter.requests[0]["messages"]
    )
    assert request_text.count("Skill trigger 'missing' did not match") == 1


def test_announce_newly_available_skills_seeds_then_announces_once(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_skills=["*"])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=StubAdapter([]))
    runtime.chat_sessions.create("coder", session_id="s1")
    session = runtime.chat_sessions.get("coder", "s1")
    loop = build_chat_loop(runtime)

    def announce(skills: Any) -> None:
        loop._announce_newly_available_skills("coder", "s1", session, agent, skills, None)

    def available_notes() -> list[ChatMessage]:
        return [message for message in session.load() if is_skill_available_note(message)]

    # The first build seeds the baseline (here empty) without announcing anything.
    announce(StubSkills([]))
    assert available_notes() == []

    # A skill that becomes available is announced exactly once, with name + description.
    deploy = StubSkills([StubSkill("deploy", "Ship the app.", tmp_path / "deploy")])
    announce(deploy)
    notes = available_notes()
    assert len(notes) == 1
    assert "deploy: Ship the app." in cast(str, notes[0].content)

    # Re-running with the same registry does not re-announce it.
    announce(deploy)
    assert len(available_notes()) == 1

    # A skill going away is deliberately not announced (additions only).
    announce(StubSkills([]))
    assert len(available_notes()) == 1


def test_skill_available_note_renders_as_reminder_without_prefix() -> None:
    note = ChatMessage.note(SKILL_AVAILABLE_NOTE_PREFIX + "New skills:\n- deploy: Ship the app.")

    rendered = cast(str, _notes_to_synthetic_user_message([note])["content"])

    assert "<system-reminder>" in rendered
    assert "New skills:" in rendered
    assert "- deploy: Ship the app." in rendered
    assert SKILL_AVAILABLE_NOTE_PREFIX not in rendered
