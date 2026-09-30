"""System Prompt assembly and core data-block tests."""

import asyncio
import logging
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from core.channels import ChannelConfig
from core.memory import (
    MEMORY_PROMPT_MODE_AGENT,
    MEMORY_PROMPT_MODE_AGENT_USER,
    MEMORY_PROMPT_MODE_OFF,
    MemoryPromptMode,
)
from core.prompts import INLINE_FILE_MAX_BYTES
from core.prompts.prompts import (
    PROJECT_FILE_TOO_LARGE_NOTICE,
    SOUL_FRAMING,
    ProjectPromptContext,
    SystemPromptManager,
)
from core.utils.paths import model_path
from tests.core.prompts.prompts_test_support import (
    StubChannels,
    StubSkill,
    StubSkills,
    StubStorage,
    StubTools,
    _agent,
    _manager,
)
from tests.core.prompts.prompts_test_support import workspace as workspace


def _channel(channel_id: str, *, agent_id: str = "coder", enabled: bool = True) -> ChannelConfig:
    return ChannelConfig(
        id=channel_id,
        platform="telegram",
        agent_id=agent_id,
        allowed_chat_ids=["111"],
        token_env_var="TELEGRAM_BOT_TOKEN",
        enabled=enabled,
    )


def test_identity_agent_prompt_assembles_blocks_in_default_layout_order(
    workspace: Path,
    tmp_path: Path,
) -> None:
    tools = StubTools()
    skills = StubSkills(
        [StubSkill("agent-cli", "Delegate coding tasks"), StubSkill("news", "News")]
    )
    channels = StubChannels(
        [
            _channel("tg-private"),
            _channel("tg-group"),
            _channel("other-agent-channel", agent_id="other-agent"),
        ]
    )
    manager = _manager(tmp_path, tools=tools, skills=skills, channels=channels)
    agent = _agent(workspace, allowed_tools=["read_file"], allowed_skills=["agent-cli"])
    details: list[dict[str, object]] = []

    prompt = manager.build_system_prompt(agent, block_details=details)

    # Runtime-owned values are expanded and preserved without pinning their prose labels.
    assert "test-host" in prompt
    assert "test-os" in prompt
    assert "0.1.0" in prompt
    assert model_path((tmp_path / "app").resolve()) in prompt
    assert model_path((tmp_path / "data").resolve()) in prompt
    assert "openai/gpt-5.2" in prompt
    assert model_path(workspace) in prompt
    assert "high" in prompt
    assert "Current local date: `2026-05-04`" in prompt
    assert "15:30:00" not in prompt
    assert "Europe/Berlin" in prompt
    # The opt-in Tool-description list ships disabled.
    assert "Read a workspace file" not in prompt
    assert "- shell:" not in prompt
    # Only this Agent's enabled Channel fixture data is rendered.
    assert "tg-private" in prompt
    assert "tg-group" in prompt
    assert "telegram" in prompt
    assert "other-agent-channel" not in prompt
    # Skills block.
    assert "- agent-cli: Delegate coding tasks" in prompt
    assert "news" not in prompt
    # Data blocks: SOUL + memory entries.
    assert "Soul text" in prompt
    assert "- Memory text" in prompt
    assert "- User text" in prompt
    assert '<file name="SOUL.md">' in prompt
    # Memory is tool-owned content rather than an included workspace file.
    assert '<file name="MEMORY.md">' not in prompt
    assert '<file name="USER.md">' not in prompt
    # No leftover markers and clean normalization.
    assert "{" not in prompt
    assert prompt == prompt.strip()
    assert "\n\n\n" not in prompt
    # Block order is observed through injected values rather than bundled prose.
    order = [
        "Soul text",
        "Memory text",
        "test-os",
        model_path((tmp_path / "app").resolve()),
        "tg-private",
        "agent-cli",
    ]
    positions = [prompt.index(section) for section in order]
    assert positions == sorted(positions)
    # The System Reminder anchor follows the Tool guidance in every build.
    included = [block["id"] for block in details if block["included"]]
    assert included[included.index("core:tools") + 1] == "core:system_reminders"
    anchor = next(block for block in details if block["id"] == "core:system_reminders")
    assert "<system-reminder>" in str(anchor["text"])
    assert str(anchor["text"]).strip() in prompt
    # Same agent allowlist drives prompt tools and gate 2's memory-tool check.
    assert tools.prompt_allowlist_calls[0] == ["read_file", "memory"]
    assert skills.allowlist == ["agent-cli"]


@pytest.mark.asyncio
async def test_async_prompt_build_keeps_sync_assembly_off_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(tmp_path)
    started = threading.Event()
    release = threading.Event()
    worker_threads: list[int] = []

    def blocking_build(_agent: object, _scope: object = None, **_options: object) -> str:
        worker_threads.append(threading.get_ident())
        started.set()
        assert release.wait(timeout=2)
        return "prompt"

    monkeypatch.setattr(manager, "build_system_prompt", blocking_build)
    loop_thread = threading.get_ident()
    build_task = asyncio.create_task(manager.build_system_prompt_async(_agent("")))
    assert await asyncio.to_thread(started.wait, 2)
    await asyncio.sleep(0)

    assert worker_threads and worker_threads != [loop_thread]
    assert build_task.done() is False
    release.set()
    assert await build_task == "prompt"


@pytest.mark.parametrize(
    ("mode", "memory_files"),
    [
        (MEMORY_PROMPT_MODE_OFF, ()),
        (MEMORY_PROMPT_MODE_AGENT, ("MEMORY.md",)),
        (MEMORY_PROMPT_MODE_AGENT_USER, ("MEMORY.md", "USER.md")),
    ],
)
def test_memory_mode_selects_the_rendered_and_reported_memory_files(
    workspace: Path, tmp_path: Path, mode: MemoryPromptMode, memory_files: tuple[str, ...]
) -> None:
    manager = _manager(tmp_path)
    agent = _agent(workspace, memory_prompt_mode=mode)
    read_paths: list[Path] = []

    prompt = manager.build_system_prompt(agent, read_paths=read_paths)

    # Collecting read paths is a pure side channel.
    assert prompt == manager.build_system_prompt(agent)
    assert "Soul text" in prompt
    assert ("<memory>" in prompt) is bool(memory_files)
    assert ("- Memory text" in prompt) is ("MEMORY.md" in memory_files)
    assert ("- User text" in prompt) is ("USER.md" in memory_files)
    # Every auto-injected file is reported so the chat loop can stamp it read-before-write.
    assert set(read_paths) == {(workspace / name).resolve() for name in ("SOUL.md", *memory_files)}


def test_memory_block_renders_with_empty_memory_files(tmp_path: Path) -> None:
    # The guidance is the block's own text and the owner gate is "memory tool enabled",
    # so the block appears whenever memory_prompt_mode != off. Not-yet-created files
    # render like empty ones, and rendering never creates them.
    empty_workspace = tmp_path / "empty-ws"
    empty_workspace.mkdir()
    manager = _manager(tmp_path)
    agent = _agent(empty_workspace, memory_prompt_mode=MEMORY_PROMPT_MODE_AGENT_USER)

    missing = manager.build_system_prompt(agent)

    assert "<memory>" in missing
    assert not (empty_workspace / "MEMORY.md").exists()
    assert not (empty_workspace / "USER.md").exists()
    (empty_workspace / "MEMORY.md").write_text("", encoding="utf-8")
    (empty_workspace / "USER.md").write_text("", encoding="utf-8")
    assert manager.build_system_prompt(agent) == missing


def test_memory_entries_and_skill_descriptions_are_never_expanded(
    workspace: Path, tmp_path: Path
) -> None:
    # Memory entries and Skill descriptions are Producer output: markers inside them
    # stay literal, an unsafe include never fails the build, and no file is inlined.
    (workspace / "MEMORY.md").write_text(
        "- see {include:../etc/passwd}\n- note {include:USER.md} on {model} at {data_root}\n",
        encoding="utf-8",
    )
    skills = StubSkills([StubSkill("notes", "Reads {include:SOUL.md} and {generated:tool_list}")])
    manager = _manager(tmp_path, skills=skills)
    agent = _agent(workspace, memory_prompt_mode=MEMORY_PROMPT_MODE_AGENT)

    prompt = manager.build_system_prompt(agent)

    assert "- see {include:../etc/passwd}" in prompt
    assert "- note {include:USER.md} on {model} at {data_root}" in prompt
    assert "Reads {include:SOUL.md} and {generated:tool_list}" in prompt
    assert "User text" not in prompt
    assert '<file name="USER.md">' not in prompt


@pytest.mark.parametrize("mode", [MEMORY_PROMPT_MODE_OFF, MEMORY_PROMPT_MODE_AGENT_USER])
def test_agent_without_workspace_never_reads_server_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: MemoryPromptMode
) -> None:
    manager = _manager(tmp_path)
    monkeypatch.chdir(tmp_path)
    for name in ("SOUL.md", "MEMORY.md", "USER.md"):
        (tmp_path / name).write_text(f"- SERVER_{name}_SENTINEL", encoding="utf-8")
    reads: list[Path] = []
    read_paths: list[Path] = []
    agent = _agent("", memory_prompt_mode=mode)

    assert manager.render_memory_files(agent, on_read=reads.append) == ""
    prompt = manager.build_system_prompt(agent, read_paths=read_paths)

    assert "SENTINEL" not in prompt
    assert "<memory>" not in prompt
    assert reads == []
    assert read_paths == []


def test_channels_block_renders_only_with_an_enabled_channel_of_this_agent(
    workspace: Path, tmp_path: Path
) -> None:
    # Without such a Channel the whole block gates out (owner "channel").
    agent = _agent(workspace, allowed_tools=["read_file"])

    def prompt(channels: StubChannels | None) -> str:
        return _manager(tmp_path, channels=channels).build_system_prompt(agent)

    empty = prompt(StubChannels([]))
    assert prompt(None) == empty
    assert prompt(StubChannels([_channel("tg-disabled", enabled=False)])) == empty
    assert prompt(StubChannels([_channel("tg-other", agent_id="other-agent")])) == empty
    assert "enabled-fixture-channel" in prompt(StubChannels([_channel("enabled-fixture-channel")]))


def test_soul_block_frames_the_workspace_soul_file(workspace: Path, tmp_path: Path) -> None:
    # SOUL is identity, not reference material: the framing line sits immediately above
    # the file tag so the model reads it as its core contract.
    agent = _agent(workspace, memory_prompt_mode=MEMORY_PROMPT_MODE_OFF, allowed_tools=[])

    prompt = _manager(tmp_path).build_system_prompt(agent)

    assert f'{SOUL_FRAMING}\n\n<file name="SOUL.md">\nSoul text\n</file>' in prompt


@pytest.mark.parametrize("soul", ["no-workspace", "unreadable"])
def test_soul_block_and_framing_gate_out_without_a_readable_soul(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, soul: str
) -> None:
    # A config Agent (workspace "") has no SOUL, and an unreadable SOUL never aborts
    # the Run; the framing line never renders on its own.
    workspace: str | Path = ""
    if soul == "unreadable":
        workspace = tmp_path / "ws"
        (workspace / "SOUL.md").mkdir(parents=True)
    agent = _agent(workspace, memory_prompt_mode=MEMORY_PROMPT_MODE_OFF, allowed_tools=[])

    with caplog.at_level(logging.WARNING):
        prompt = _manager(tmp_path).build_system_prompt(agent)

    assert '<file name="SOUL.md">' not in prompt
    assert SOUL_FRAMING not in prompt
    if soul == "unreadable":
        assert any(record.levelno == logging.WARNING for record in caplog.records)


def test_config_agent_body_renders_verbatim(tmp_path: Path) -> None:
    # The Agent body is a data block, never expanded: a "{...}" inside it (even a real
    # vBot placeholder name) survives verbatim.
    manager = _manager(tmp_path)
    agent = _agent("", memory_prompt_mode=MEMORY_PROMPT_MODE_OFF)
    body = (
        "You are the orchestrator. Use {server_hostname} and {include:SOUL.md} and "
        "{generated:tool_list} literally; also {custom}."
    )

    prompt = manager.build_system_prompt(agent, agent_body=body)

    assert body in prompt


@pytest.mark.parametrize(
    ("fragment", "legacy", "identity"),
    [
        ("identity_runtime.md", "Legacy {host} {app_version} {agent_workspace} {app_dir}", True),
        ("runtime.md", "Legacy {os} {current_date}", False),
    ],
)
def test_legacy_environment_placeholders_are_not_resolved(
    tmp_path: Path, fragment: str, legacy: str, identity: bool
) -> None:
    storage = StubStorage({"identity_runtime.md": "", "runtime.md": "", fragment: legacy})
    manager = _manager(tmp_path, storage=storage)
    agent = _agent(
        tmp_path / "empty-workspace" if identity else "",
        memory_prompt_mode=MEMORY_PROMPT_MODE_OFF,
    )

    assert manager.build_system_prompt(agent) == legacy


@pytest.mark.parametrize("thinking_effort", [None, ""])
def test_runtime_environment_renders_provider_default_thinking_effort(
    tmp_path: Path,
    thinking_effort: str | None,
) -> None:
    manager = _manager(tmp_path)
    agent = _agent(
        "",
        memory_prompt_mode=MEMORY_PROMPT_MODE_OFF,
        thinking_effort=thinking_effort,
    )

    prompt = manager.build_system_prompt(agent)

    equivalent_agent = _agent(
        "",
        memory_prompt_mode=MEMORY_PROMPT_MODE_OFF,
        thinking_effort="" if thinking_effort is None else None,
    )
    assert prompt == manager.build_system_prompt(equivalent_agent)
    assert "{thinking_effort}" not in prompt


def test_runtime_date_defaults_to_today_in_the_configured_time_zone(tmp_path: Path) -> None:
    manager = SystemPromptManager(
        StubStorage({"runtime.md": "{current_local_date} {timezone}"}),
        StubTools(),
        StubSkills([]),
        vbot_version="0.1.0",
        vbot_root=tmp_path,
        data_root=tmp_path,
        timezone_name=lambda: "UTC",
    )
    agent = _agent("", memory_prompt_mode=MEMORY_PROMPT_MODE_OFF)

    before = datetime.now(UTC).date().isoformat()
    prompt = manager.build_system_prompt(agent)
    after = datetime.now(UTC).date().isoformat()

    assert prompt in {f"{before} UTC", f"{after} UTC"}


def test_project_config_agent_receives_project_workspace_without_identity_runtime(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    manager = _manager(tmp_path)
    agent = _agent("", memory_prompt_mode=MEMORY_PROMPT_MODE_OFF)
    context = ProjectPromptContext.from_project("vbot", "vBot", repo, [])

    prompt = manager.build_system_prompt(
        agent,
        agent_body="You are the Project reviewer.",
        project_context=context,
    )

    assert "test-os" in prompt
    assert "vBot" in prompt
    assert "vbot" in prompt
    assert model_path(repo) in prompt
    assert "You are the Project reviewer." in prompt
    assert "<project_context>" in prompt
    assert "<project_context " not in prompt
    assert "test-host" not in prompt
    assert model_path((tmp_path / "app").resolve()) not in prompt
    assert model_path((tmp_path / "data").resolve()) not in prompt


def test_rooted_identity_prompt_distinguishes_identity_and_project_workspaces(
    workspace: Path,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    manager = _manager(tmp_path)
    agent = _agent(workspace, memory_prompt_mode=MEMORY_PROMPT_MODE_AGENT)
    context = ProjectPromptContext.from_project("vbot", "vBot", repo, [])
    snapshot = manager.render_working_project_context(context)

    prompt = manager.build_system_prompt(
        agent,
        project_context=context,
        working_project_context=snapshot,
    )

    assert model_path(workspace) in prompt
    assert model_path(repo) in prompt


def test_project_files_render_readable_files_after_memory_and_report_them(
    workspace: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # A missing, unreadable or non-UTF-8 auto-load file is dropped without aborting
    # the Run and is not reported as read. An oversized one keeps its frame with a
    # notice instead of its content, is not reported as read, and warns once.
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "AGENTS.md").write_text("Team rules", encoding="utf-8")
    (repo / "CONTEXT.md").write_bytes(b"Project\r\ncontext")
    (repo / "ADIR").mkdir()
    (repo / "BINARY.md").write_bytes(b"\xff\xfe\x00\x01 not utf-8")
    (repo / "HUGE.md").write_bytes(b"HUGE-RULES " + b"x" * INLINE_FILE_MAX_BYTES)
    manager = _manager(tmp_path)
    agent = _agent(workspace, memory_prompt_mode=MEMORY_PROMPT_MODE_AGENT)
    context = ProjectPromptContext.from_project(
        "vbot",
        "vBot",
        repo,
        ["AGENTS.md", "MISSING.md", "ADIR", "BINARY.md", "HUGE.md", "CONTEXT.md"],
    )
    read_paths: list[Path] = []

    with caplog.at_level(logging.WARNING):
        prompt = manager.build_system_prompt(agent, project_context=context, read_paths=read_paths)
        manager.build_system_prompt(agent, project_context=context)

    assert ' <file name="AGENTS.md">\nTeam rules\n </file>' in prompt
    assert ' <file name="CONTEXT.md">\nProject\ncontext\n </file>' in prompt
    assert '<file name="ADIR">' not in prompt
    assert '<file name="BINARY.md">' not in prompt
    notice = PROJECT_FILE_TOO_LARGE_NOTICE.format(
        size=INLINE_FILE_MAX_BYTES + len("HUGE-RULES "), limit=INLINE_FILE_MAX_BYTES
    )
    assert f' <file name="HUGE.md">\n{notice}\n </file>' in prompt
    assert "HUGE-RULES" not in prompt
    messages = [record.getMessage() for record in caplog.records]
    [oversized_warning] = [message for message in messages if "HUGE.md" in message]
    assert "HUGE-RULES" not in oversized_warning
    assert any("BINARY.md" in message for message in messages)
    # Default layout: memory before Working Project; AGENTS.md before CONTEXT.md.
    assert prompt.index("<memory>") < prompt.index("AGENTS.md") < prompt.index("CONTEXT.md")
    assert set(read_paths) == {
        (workspace / "SOUL.md").resolve(),
        (workspace / "MEMORY.md").resolve(),
        (repo / "AGENTS.md").resolve(),
        (repo / "CONTEXT.md").resolve(),
    }


def test_working_project_context_uses_exact_rooted_agent_frame(tmp_path: Path) -> None:
    repo = tmp_path / "second-brain"
    repo.mkdir()
    (repo / "AGENTS.md").write_text("Team rules", encoding="utf-8")
    wiki_dir = repo / "wiki"
    wiki_dir.mkdir()
    (wiki_dir / "index.md").write_text("Wiki index", encoding="utf-8")
    manager = _manager(tmp_path)
    agent = _agent("", memory_prompt_mode=MEMORY_PROMPT_MODE_OFF)
    context = ProjectPromptContext.from_project(
        "second-brain",
        "Second Brain",
        repo,
        ["AGENTS.md", "wiki/index.md"],
    )
    read_paths: list[Path] = []

    snapshot = manager.render_working_project_context(context, on_read=read_paths.append)

    assert snapshot == (
        "## Working Project\n\n"
        "- Project: `Second Brain`\n"
        "- Project ID: `second-brain`\n"
        f"- Your Project Workspace (your working directory): `{model_path(repo)}`\n\n"
        "### Project Context\n\n"
        "Follow the instructions in any files included below and use their contents as "
        "context for all work in this Project Workspace.\n\n"
        "<project_context>\n"
        ' <file name="AGENTS.md">\n'
        "Team rules\n"
        " </file>\n\n"
        ' <file name="wiki/index.md">\n'
        "Wiki index\n"
        " </file>\n"
        "</project_context>"
    )
    assert read_paths == [
        (repo / "AGENTS.md").resolve(),
        (repo / "wiki" / "index.md").resolve(),
    ]

    (repo / "AGENTS.md").write_text("Changed rules", encoding="utf-8")
    rebuild_reads: list[Path] = []
    prompt = manager.build_system_prompt(
        agent,
        project_context=context,
        working_project_context=snapshot,
        read_paths=rebuild_reads,
    )

    assert snapshot in prompt
    assert "Changed rules" not in prompt
    assert rebuild_reads == []


def test_working_project_template_preserves_plain_metadata_and_file_placeholders(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "research & development"
    repo.mkdir()
    (repo / "CONTEXT.md").write_text(
        "Keep {project_name}, {project_workspace}, and $project_name literal.",
        encoding="utf-8",
    )
    manager = _manager(tmp_path)
    context = ProjectPromptContext.from_project(
        "research-and-development",
        "Research & Development",
        repo,
        ["CONTEXT.md"],
    )

    snapshot = manager.render_working_project_context(context)

    assert "Research & Development" in snapshot
    assert model_path(repo) in snapshot
    assert "&amp;" not in snapshot
    assert "Keep {project_name}, {project_workspace}, and $project_name literal." in snapshot


def test_legacy_working_project_placeholders_are_not_resolved(tmp_path: Path) -> None:
    legacy = "Legacy $project_name $project_id $project_workspace $project_files"
    manager = _manager(
        tmp_path,
        storage=StubStorage({"working_project.md": legacy}),
    )
    context = ProjectPromptContext.from_project("vbot", "vBot", tmp_path, [])

    snapshot = manager.render_working_project_context(context)

    assert snapshot == legacy


def test_render_project_files_one_source_for_reminder_and_prompt(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "AGENTS.md").write_text("Team rules", encoding="utf-8")
    manager = _manager(tmp_path)
    agent = _agent(tmp_path / "empty-ws", memory_prompt_mode=MEMORY_PROMPT_MODE_OFF)
    context = ProjectPromptContext.from_project("vbot", "vBot", repo, ["AGENTS.md"])

    rendered = manager.render_project_files(context)
    in_prompt = manager.build_system_prompt(agent, project_context=context)

    assert rendered == '<file name="AGENTS.md">\nTeam rules\n</file>'
    assert ' <file name="AGENTS.md">\nTeam rules\n </file>' in in_prompt
