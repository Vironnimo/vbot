"""Skills in Chat Runs: trigger activation, unmatched-trigger reminders, expiry at Compaction
and announcements of newly available Skills."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from core.chat import ChatMessage
from core.sessions import SKILL_AVAILABLE_NOTE_PREFIX, ChatSession, is_skill_available_note
from core.skills.policy import SkillPackageRef
from core.skills.skills import SkillRegistry
from core.tools import ToolContext, ToolRegistry, register_skill_tool, tool_failure
from tests.core.chat.chat_loop_support import (
    StubAdapter,
    StubAgent,
    StubRuntime,
    StubSkill,
    StubSkills,
    build_chat_loop,
    build_request_messages,
    history,
    persisted_roles,
)
from tests.core.chat.chat_loop_tools_test_support import final, tool_runtime, tool_turn

JsonObject = dict[str, Any]

SKILL_CONTENT = '<skill_content name="debugging">'


def _write_skill(skills_dir: Path, name: str, *, requirements: str = "") -> Path:
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(
        f"---\nname: {name}\ndescription: Test skill.\n{requirements}---\n\n# {name}\n\n"
        "Use this skill content.\n",
        encoding="utf-8",
    )
    return skill_file


def _runtime(tmp_path: Path, responses: int, *, allowed_skills: list[str], skills: Any) -> Any:
    agent = StubAgent(
        id="coder", model="openai/gpt-5.2", allowed_tools=["*"], allowed_skills=allowed_skills
    )
    adapter = StubAdapter(
        [{"content": f"Answer {i}", "tool_calls": None} for i in range(responses)]
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    runtime.skills = skills
    return runtime


def _debugging_skills(tmp_path: Path) -> StubSkills:
    skill_file = _write_skill(tmp_path / "skills", "debugging")
    return StubSkills(
        [
            StubSkill(
                "debugging",
                "Debug failures",
                skill_file,
                package_ref=SkillPackageRef("agent", "debugging", "coder"),
            )
        ]
    )


def _contents(request_messages: list[JsonObject]) -> list[str]:
    return [message.get("content", "") or "" for message in request_messages]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "trigger",
    [
        "/debugging fix this",
        "Please use $debugging on this issue",
        '$debugging: check "$env:LOCALAPPDATA/app.exe" and $script:state',
    ],
    ids=["slash", "inline", "powershell-variables"],
)
async def test_skill_trigger_places_its_content_under_the_input_for_later_requests(
    tmp_path: Path, trigger: str
) -> None:
    runtime = _runtime(
        tmp_path, 2, allowed_skills=["debugging"], skills=_debugging_skills(tmp_path)
    )
    loop = build_chat_loop(runtime)

    await loop.send("coder", trigger, session_id="session-one")
    await loop.send("coder", "continue", session_id="session-one")

    first, second = (request["messages"] for request in runtime.adapter.requests)
    # The Skill content sits directly under the triggering user message — in
    # place, not hoisted — and replays there in later requests.
    for request_messages in (first, second):
        assert all("skill_package" not in message for message in request_messages)
        assert request_messages[1]["content"] == trigger
        assert request_messages[2]["content"].startswith(SKILL_CONTENT)
        assert all("<system-reminder>" not in content for content in _contents(request_messages))
    assert second[-1]["content"] == "continue"
    assert all("[skill-context]" not in content for content in _contents(second))
    visible = [message for message in history(runtime) if message.role != "note"]
    assert persisted_roles(visible) == ["user", "assistant", "user", "assistant"]
    assert all(SKILL_CONTENT not in str(message.content) for message in visible)
    [activation] = [message for message in history(runtime) if message.skill_package is not None]
    assert activation.skill_package == SkillPackageRef("agent", "debugging", "coder")


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_after_activation", [False, True])
async def test_skill_tool_persists_loaded_package_without_exposing_it_to_the_model(
    tmp_path: Path,
    fail_after_activation: bool,
) -> None:
    _write_skill(tmp_path / "skills", "debugging")
    skills = SkillRegistry.load(tmp_path / "skills", package_sources=[("agent", "owner")])
    tools = ToolRegistry()
    package = SkillPackageRef("agent", "debugging", "owner")
    if fail_after_activation:

        def fail(context: ToolContext, _arguments: JsonObject) -> JsonObject:
            context.activate_skill("debugging", "Instructions", package)
            return tool_failure("read_error", "The instructions could not be read.")

        tools.register("skill", "Load a Skill.", {"type": "object"}, fail)
    else:
        register_skill_tool(tools, lambda _project, _agent: skills, lambda: None)
    runtime = tool_runtime(
        tmp_path, tools, [tool_turn(("load", "skill", {"name": "debugging"})), final("Done")]
    )
    runtime.skills = skills

    await build_chat_loop(runtime).send(
        "coder", "Investigate the failure", session_id="session-one"
    )

    [result] = [message for message in history(runtime) if message.role == "tool"]
    assert result.skill_package == (None if fail_after_activation else package)
    for request in runtime.adapter.requests:
        assert all("skill_package" not in message for message in request["messages"])


def _unavailable_skills(tmp_path: Path) -> SkillRegistry:
    _write_skill(
        tmp_path / "skills",
        "openai-helper",
        requirements=("metadata:\n  vbot:\n    requirements:\n      env: OPENAI_API_KEY\n"),
    )
    return SkillRegistry.load(tmp_path / "skills", environment={})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("trigger", "allowed_skills", "skills", "reminders"),
    [
        ("/missing do it", [], lambda _path: StubSkills([]), ["'missing' did not match"]),
        (
            "Please use $debugging on this issue",
            [],
            _debugging_skills,
            ["'debugging' did not match"],
        ),
        (
            "/openai-helper help",
            ["openai-helper"],
            _unavailable_skills,
            [
                "'openai-helper' matched a skill, but it is unavailable",
                "missing environment variable 'OPENAI_API_KEY'",
            ],
        ),
    ],
    ids=["unknown", "not-allowed", "unavailable"],
)
async def test_unmatched_skill_trigger_adds_one_reminder_instead_of_content(
    tmp_path: Path,
    trigger: str,
    allowed_skills: list[str],
    skills: Callable[[Path], Any],
    reminders: list[str],
) -> None:
    runtime = _runtime(tmp_path, 1, allowed_skills=allowed_skills, skills=skills(tmp_path))

    await build_chat_loop(runtime).send("coder", trigger, session_id="session-one")

    contents = _contents(runtime.adapter.requests[0]["messages"])
    assert contents[1] == trigger
    assert all("<skill_content" not in content for content in contents)
    for reminder in reminders:
        assert reminder in contents[2]
        assert "\n".join(contents).count(reminder) == 1


def _activate(session: ChatSession, steps: str) -> ChatMessage:
    session.activate_skill_context(
        "debugging",
        {"activation_content": f'<skill_content name="debugging">{steps}</skill_content>'},
    )
    return session.load()[-1]


def _skill_before_the_tail(session: ChatSession) -> list[ChatMessage]:
    session.append(ChatMessage.user("Old question"))
    _activate(session, "Steps")
    session.append(ChatMessage.assistant(model="openai/gpt-5.2", content="Old answer"))
    session.append(ChatMessage.user("Tail question"))
    session.append(ChatMessage.assistant(model="openai/gpt-5.2", content="Tail answer"))
    return session.load()[-2:]


def _skill_inside_the_tail(session: ChatSession) -> list[ChatMessage]:
    session.append(ChatMessage.user("Old question"))
    session.append(ChatMessage.assistant(model="openai/gpt-5.2", content="Old answer"))
    session.append(ChatMessage.user("/debugging fix this"))
    _activate(session, "Steps")
    session.append(ChatMessage.assistant(model="openai/gpt-5.2", content="Tail answer"))
    return session.load()[-3:]


def _superseded_skill_in_the_tail(session: ChatSession) -> list[ChatMessage]:
    session.append(ChatMessage.user("Old question"))
    old_carrier = _activate(session, "Old steps")
    _activate(session, "New steps")
    return [old_carrier]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("seed", "tail_input"),
    [
        (_skill_before_the_tail, "Tail question"),
        (_skill_inside_the_tail, "/debugging fix this"),
        (_superseded_skill_in_the_tail, None),
    ],
    ids=["before-tail", "inside-tail", "superseded-version"],
)
async def test_compaction_checkpoint_expires_triggered_skill_content(
    tmp_path: Path,
    seed: Callable[[ChatSession], list[ChatMessage]],
    tail_input: str | None,
) -> None:
    runtime = _runtime(tmp_path, 0, allowed_skills=["*"], skills=StubSkills([]))
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    projection = seed(session)
    session.append(
        ChatMessage.compaction_checkpoint(
            summary="Compacted historical context.",
            projection=projection,
            compacted_token_count=123,
        )
    )

    request_messages = await build_request_messages(
        build_chat_loop(runtime), runtime.agents.get("coder"), session
    )

    contents = _contents(request_messages)
    assert all(
        "<skill_content" not in content and "steps" not in content.lower() for content in contents
    )
    assert session.activated_skill_contents() == {}
    if tail_input is not None:
        assert contents[1] == "<system-reminder>\nCompacted historical context.\n</system-reminder>"
        assert contents[2] == tail_input


@pytest.mark.asyncio
async def test_newly_available_skills_are_announced_once_with_the_run_input(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path, 4, allowed_skills=["*"], skills=StubSkills([]))
    loop = build_chat_loop(runtime)

    def available_notes() -> list[ChatMessage]:
        return [message for message in history(runtime, "s1") if is_skill_available_note(message)]

    # The first Run seeds the baseline (here empty) without announcing anything.
    await loop.send("coder", "first", session_id="s1")
    assert available_notes() == []

    # A Skill that becomes available is announced exactly once, with name and
    # description, directly ahead of the input it was persisted with.
    runtime.skills = StubSkills([StubSkill("deploy", "Ship the app.", tmp_path / "deploy")])
    await loop.send("coder", "second", session_id="s1")
    notes = available_notes()
    assert len(notes) == 1
    assert "deploy: Ship the app." in str(notes[0].content)
    messages = history(runtime, "s1")
    note_index = messages.index(notes[0])
    assert (messages[note_index + 1].role, messages[note_index + 1].content) == ("user", "second")
    # The Model sees the announcement as a reminder, without the internal prefix.
    reminder = runtime.adapter.requests[1]["messages"][-2]["content"]
    assert reminder.startswith("<system-reminder>")
    assert "- deploy: Ship the app." in reminder
    assert SKILL_AVAILABLE_NOTE_PREFIX not in reminder

    # Later Runs with the same catalog do not re-announce it.
    await loop.send("coder", "third", session_id="s1")
    assert len(available_notes()) == 1

    # A Skill going away is deliberately not announced (additions only).
    runtime.skills = StubSkills([])
    await loop.send("coder", "fourth", session_id="s1")
    assert len(available_notes()) == 1
