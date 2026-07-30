"""System Prompt assembly and core data-block tests."""

from .prompts_test_support import (
    MEMORY_PROMPT_MODE_AGENT,
    MEMORY_PROMPT_MODE_AGENT_USER,
    MEMORY_PROMPT_MODE_OFF,
    SOUL_FRAMING,
    ChannelConfig,
    Path,
    ProjectPromptContext,
    PromptError,
    StubChannels,
    StubSkill,
    StubSkills,
    StubStorage,
    StubTools,
    _agent,
    _manager,
    logging,
    pytest,
    validate_workspace_include,
)
from .prompts_test_support import workspace as workspace


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
            ChannelConfig(
                id="tg-private",
                platform="telegram",
                agent_id="coder",
                allowed_chat_ids=["8506476339"],
                token_env_var="TELEGRAM_BOT_TOKEN",
                enabled=True,
            ),
            ChannelConfig(
                id="tg-group",
                platform="telegram",
                agent_id="coder",
                allowed_chat_ids=["111", "222"],
                token_env_var="TELEGRAM_GROUP_TOKEN",
                enabled=True,
            ),
            ChannelConfig(
                id="other-agent-channel",
                platform="telegram",
                agent_id="other-agent",
                allowed_chat_ids=["333"],
                token_env_var="OTHER_TOKEN",
                enabled=True,
            ),
        ]
    )
    manager = _manager(tmp_path, tools=tools, skills=skills, channels=channels)
    agent = _agent(workspace, allowed_tools=["read_file"], allowed_skills=["agent-cli"])

    prompt = manager.build_system_prompt(agent)

    # Shared Runtime and Identity Environment blocks (variables filled).
    assert "- Server hostname: `test-host`" in prompt
    assert "- Operating system: `test-os`" in prompt
    assert "- vBot version: `0.1.0`" in prompt
    assert f"- vBot root: `{(tmp_path / 'app').resolve()}`" in prompt
    assert f"- vBot data root: `{(tmp_path / 'data').resolve()}`" in prompt
    assert "- Model: `openai/gpt-5.2`" in prompt
    assert f"{workspace}" in prompt
    assert "- Configured thinking effort: `high`" in prompt
    assert "- Current date (UTC): `2026-05-04`" in prompt
    assert "## Working Project" not in prompt
    # Tools block: call-style guidance only. The full name/description list lives
    # in the opt-in core:tools_list block, which ships disabled — the provider tool
    # definitions already carry every description.
    assert "## Tool Call Style" in prompt
    assert (
        "When multiple tool calls are independent and all required arguments are already known, "
        "issue them together in the same response. Keep calls sequential when one depends on "
        "another's result or when the calls may conflict."
    ) in prompt
    assert "## Available Tools" not in prompt
    assert "- read_file: Read a workspace file" not in prompt
    assert "shell" not in prompt
    # Channels block (only this agent's enabled Channel configs).
    assert "## Channels" in prompt
    assert "- tg-private: telegram (default target available)" in prompt
    assert "- tg-group: telegram (explicit target required)" in prompt
    assert "You can send messages and files through these configured channels:" in prompt
    assert (
        "Use `channel_send` for proactive outbound messages and whenever you send a file "
        "through a channel."
    ) in prompt
    assert (
        "Do not use `channel_send` for normal text-only replies to channel-originated turns; "
        "those replies are routed automatically."
    ) in prompt
    assert ("Put every file path in `file_paths`; never send file Markdown to a channel.") in prompt
    assert "other-agent-channel" not in prompt
    # Skills block.
    assert "<name>agent-cli</name>" in prompt
    assert "<description>Delegate coding tasks</description>" in prompt
    assert "news" not in prompt
    # Data blocks: SOUL + memory entries.
    assert "Soul text" in prompt
    assert "- Memory text" in prompt
    assert "- User text" in prompt
    assert '<file name="SOUL.md">' in prompt
    # Memory renders under scope headings, not a <file> wrapper (tool-owned content).
    assert "# Agent Memory" in prompt
    assert "# User Profile" in prompt
    assert '<file name="MEMORY.md">' not in prompt
    assert '<file name="USER.md">' not in prompt
    # No leftover placeholders / no "- None" / clean normalization.
    assert "{" not in prompt
    assert "- None" not in prompt
    assert prompt == prompt.strip()
    assert "\n\n\n" not in prompt
    # Order: SOUL < memory < runtime < identity environment < tools < channels < skills.
    order = [
        "Soul text",
        "<memory>",
        "## Runtime",
        "## Identity Environment",
        "## Tool Call Style",
        "## Channels",
    ]
    positions = [prompt.index(section) for section in order]
    assert positions == sorted(positions)
    assert prompt.index("## Channels") < prompt.index("## Available Skills")
    # Same agent allowlist drives prompt tools and gate 2's memory-tool check.
    assert tools.prompt_allowlist_calls[0] == ["read_file"]
    assert skills.allowlist == ["agent-cli"]


def test_memory_block_renders_with_empty_memory_files(tmp_path: Path) -> None:
    # The guidance is the block's own text and the owner gate is "memory tool enabled",
    # so the block appears whenever memory_prompt_mode != off. With lazy file ownership,
    # not-yet-created files render their default "no entries" content (identical to an
    # empty on-disk file), so the framing is present from the first turn — and rendering
    # never creates the files.
    empty_workspace = tmp_path / "empty-ws"
    empty_workspace.mkdir()
    manager = _manager(tmp_path)
    agent = _agent(empty_workspace, memory_prompt_mode=MEMORY_PROMPT_MODE_AGENT_USER)

    prompt = manager.build_system_prompt(agent)

    assert "<memory>" in prompt
    assert "declarative facts" in prompt  # the guidance prose
    assert "# Agent Memory" in prompt
    assert "# User Profile" in prompt
    assert "No entries yet." in prompt
    assert not (empty_workspace / "MEMORY.md").exists()
    assert not (empty_workspace / "USER.md").exists()


def test_memory_block_absent_when_memory_off(workspace: Path, tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    agent = _agent(workspace, memory_prompt_mode=MEMORY_PROMPT_MODE_OFF)

    prompt = manager.build_system_prompt(agent)

    assert "Soul text" in prompt  # SOUL still renders
    assert "<memory>" not in prompt
    assert "Memory text" not in prompt
    assert "User text" not in prompt


def test_memory_block_includes_only_agent_memory(workspace: Path, tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    agent = _agent(workspace, memory_prompt_mode=MEMORY_PROMPT_MODE_AGENT)

    prompt = manager.build_system_prompt(agent)

    assert "<memory>" in prompt
    assert "# Agent Memory" in prompt
    assert "- Memory text" in prompt
    assert "# User Profile" not in prompt
    assert "User text" not in prompt


def test_channel_less_agent_has_no_channels_block(workspace: Path, tmp_path: Path) -> None:
    # Adapted from the old "renders - None" test: with no enabled Channels the whole
    # block gates out (owner "channel"), it does not render "- None".
    manager = _manager(tmp_path, channels=StubChannels([]))
    agent = _agent(workspace, allowed_tools=["read_file"])

    prompt = manager.build_system_prompt(agent)

    assert "## Channels" not in prompt
    assert "- None" not in prompt


def test_disabled_channel_does_not_enable_channels_block(
    workspace: Path,
    tmp_path: Path,
) -> None:
    channels = StubChannels(
        [
            ChannelConfig(
                id="tg-disabled",
                platform="telegram",
                agent_id="coder",
                allowed_chat_ids=["8506476339"],
                token_env_var="TELEGRAM_BOT_TOKEN",
                enabled=False,
            )
        ]
    )
    manager = _manager(tmp_path, channels=channels)

    prompt = manager.build_system_prompt(_agent(workspace))

    assert "## Channels" not in prompt
    assert "tg-disabled" not in prompt


def test_channels_block_absent_without_channel_registry(workspace: Path, tmp_path: Path) -> None:
    manager = _manager(tmp_path, channels=None)
    agent = _agent(workspace)

    prompt = manager.build_system_prompt(agent)

    assert "## Channels" not in prompt


def test_soul_block_collapses_without_workspace_file(tmp_path: Path) -> None:
    # A config agent has workspace "" → SOUL block collapses (gate 3); no decoy
    # SOUL from the process CWD is read.
    manager = _manager(tmp_path)
    agent = _agent("", memory_prompt_mode=MEMORY_PROMPT_MODE_OFF)

    prompt = manager.build_system_prompt(agent)

    assert '<file name="SOUL.md">' not in prompt


def test_config_agent_body_renders_verbatim(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    agent = _agent("", memory_prompt_mode=MEMORY_PROMPT_MODE_OFF)

    prompt = manager.build_system_prompt(agent, agent_body="You are the orchestrator.")

    assert "You are the orchestrator." in prompt


def test_config_agent_body_with_braces_is_not_expanded(tmp_path: Path) -> None:
    # Plan risk "Body-Wörtlichkeit": the agent body is a data block, never expanded —
    # a "{...}" inside it (even a real vBot placeholder name) survives verbatim.
    manager = _manager(tmp_path)
    agent = _agent("", memory_prompt_mode=MEMORY_PROMPT_MODE_OFF)
    body = (
        "Use {server_hostname} and {include:SOUL.md} and "
        "{generated:tool_list} literally; also {custom}."
    )

    prompt = manager.build_system_prompt(agent, agent_body=body)

    assert body in prompt


def test_legacy_identity_environment_placeholders_are_not_resolved(tmp_path: Path) -> None:
    legacy = "Legacy {host} {app_version} {agent_workspace} {app_dir}"
    storage = StubStorage(
        {
            "identity_runtime.md": legacy,
            "runtime.md": "",
        }
    )
    manager = _manager(tmp_path, storage=storage)
    agent = _agent(tmp_path / "empty-workspace", memory_prompt_mode=MEMORY_PROMPT_MODE_OFF)

    prompt = manager.build_system_prompt(agent)

    assert prompt == legacy


def test_legacy_runtime_environment_placeholders_are_not_resolved(tmp_path: Path) -> None:
    legacy = "Legacy {os} {current_date}"
    storage = StubStorage(
        {
            "identity_runtime.md": "",
            "runtime.md": legacy,
        }
    )
    manager = _manager(tmp_path, storage=storage)
    agent = _agent("", memory_prompt_mode=MEMORY_PROMPT_MODE_OFF)

    prompt = manager.build_system_prompt(agent)

    assert prompt == legacy


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

    assert "- Configured thinking effort: `provider default`" in prompt


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

    assert "## Runtime Environment" in prompt
    assert "- Operating system: `test-os`" in prompt
    assert "## Working Project" in prompt
    assert "- Project: `vBot`" in prompt
    assert "- Project ID: `vbot`" in prompt
    assert f"- Your Project Workspace: `{repo}`" in prompt
    assert "<project_context>" in prompt
    assert "<project_context " not in prompt
    assert "## Identity Environment" not in prompt
    assert "- Server hostname:" not in prompt
    assert "vBot version:" not in prompt
    assert "Identity and Memory Workspace:" not in prompt
    assert "vBot root:" not in prompt
    assert "vBot data root:" not in prompt


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

    assert "## Identity Environment" in prompt
    assert f"- Your Identity and Memory Workspace: `{workspace}`" in prompt
    assert "## Working Project" in prompt
    assert f"- Your Project Workspace: `{repo}`" in prompt


def test_project_files_render_in_order_after_memory(workspace: Path, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "AGENTS.md").write_text("Team rules", encoding="utf-8")
    (repo / "CONTEXT.md").write_text("Project context", encoding="utf-8")
    manager = _manager(tmp_path)
    agent = _agent(workspace, memory_prompt_mode=MEMORY_PROMPT_MODE_AGENT)
    context = ProjectPromptContext.from_project(
        "vbot",
        "vBot",
        repo,
        ["AGENTS.md", "CONTEXT.md"],
    )

    prompt = manager.build_system_prompt(agent, project_context=context)

    assert ' <file name="AGENTS.md">\nTeam rules\n </file>' in prompt
    assert ' <file name="CONTEXT.md">\nProject context\n </file>' in prompt
    # Default layout: memory before Working Project; AGENTS.md before CONTEXT.md.
    assert prompt.index("<memory>") < prompt.index("AGENTS.md")
    assert prompt.index("AGENTS.md") < prompt.index("CONTEXT.md")


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
        f"- Your Project Workspace: `{repo}`\n\n"
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

    assert "- Project: `Research & Development`" in snapshot
    assert f"- Your Project Workspace: `{repo}`" in snapshot
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


def test_project_files_collapse_without_context(workspace: Path, tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    agent = _agent(workspace, memory_prompt_mode=MEMORY_PROMPT_MODE_AGENT)

    prompt = manager.build_system_prompt(agent, project_context=None)

    # No project block; the identity content is unaffected.
    assert "Soul text" in prompt
    assert "<memory>" in prompt


def test_build_system_prompt_reports_auto_injected_files_via_read_paths(
    workspace: Path, tmp_path: Path
) -> None:
    # Every file whose content reaches the prompt — SOUL, the pinned-memory files,
    # and the project's readable auto-load files — is reported so the chat loop can
    # stamp it read-before-write. A configured-but-absent auto-load file is not.
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "AGENTS.md").write_text("Team rules", encoding="utf-8")
    (repo / "CONTEXT.md").write_text("Project context", encoding="utf-8")
    manager = _manager(tmp_path)
    agent = _agent(workspace, memory_prompt_mode=MEMORY_PROMPT_MODE_AGENT_USER)
    context = ProjectPromptContext.from_project(
        "vbot",
        "vBot",
        repo,
        ["AGENTS.md", "MISSING.md", "CONTEXT.md"],
    )
    read_paths: list[Path] = []

    manager.build_system_prompt(agent, project_context=context, read_paths=read_paths)

    assert set(read_paths) == {
        (workspace / "SOUL.md").resolve(),
        (workspace / "MEMORY.md").resolve(),
        (workspace / "USER.md").resolve(),
        (repo / "AGENTS.md").resolve(),
        (repo / "CONTEXT.md").resolve(),
    }


def test_build_system_prompt_read_paths_stays_none_by_default(
    workspace: Path, tmp_path: Path
) -> None:
    # Passing no read_paths (preview, tests) leaves the assembled text byte-identical
    # to a build that collects them — the observer is a pure side channel.
    manager = _manager(tmp_path)
    agent = _agent(workspace, memory_prompt_mode=MEMORY_PROMPT_MODE_AGENT_USER)
    read_paths: list[Path] = []

    without_sink = manager.build_system_prompt(agent)
    with_sink = manager.build_system_prompt(agent, read_paths=read_paths)

    assert without_sink == with_sink
    assert read_paths  # the observed build still collected the workspace files


def test_build_system_prompt_read_paths_empty_for_off_memory_config_agent(
    tmp_path: Path,
) -> None:
    # An empty-workspace config agent with memory off and no project reports nothing
    # — nothing is auto-injected, and the empty workspace never resolves to Path(".").
    manager = _manager(tmp_path)
    agent = _agent("", memory_prompt_mode=MEMORY_PROMPT_MODE_OFF)
    read_paths: list[Path] = []

    manager.build_system_prompt(agent, project_context=None, read_paths=read_paths)

    assert read_paths == []


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


def test_project_files_never_abort_run_on_unreadable_file(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "GOOD.md").write_text("Good doc", encoding="utf-8")
    (repo / "ADIR").mkdir()
    (repo / "BINARY.md").write_bytes(b"\xff\xfe\x00\x01 not utf-8")
    manager = _manager(tmp_path)
    agent = _agent(tmp_path / "empty-ws", memory_prompt_mode=MEMORY_PROMPT_MODE_OFF)
    context = ProjectPromptContext.from_project(
        "vbot",
        "vBot",
        repo,
        ["ADIR", "BINARY.md", "GOOD.md"],
    )

    with caplog.at_level(logging.WARNING):
        prompt = manager.build_system_prompt(agent, project_context=context)

    assert ' <file name="GOOD.md">\nGood doc\n </file>' in prompt
    assert '<file name="ADIR">' not in prompt
    assert '<file name="BINARY.md">' not in prompt
    assert "Skipping unreadable project file" in caplog.text


@pytest.mark.parametrize("filename", ["SOUL.md", "CUSTOM.md", "my-notes.txt", "notes.json"])
def test_validate_workspace_include_accepts_safe_flat_filenames(filename: str) -> None:
    validate_workspace_include(filename)  # should not raise


@pytest.mark.parametrize(
    "filename",
    ["../foo", "foo/bar", "/etc/passwd", "C:\\Windows\\system32\\cmd.exe"],
)
def test_validate_workspace_include_rejects_unsafe_paths(filename: str) -> None:
    with pytest.raises(PromptError, match="Unsafe workspace include"):
        validate_workspace_include(filename)


def test_soul_block_wraps_content_in_xml_file_tag(workspace: Path, tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    agent = _agent(workspace, memory_prompt_mode=MEMORY_PROMPT_MODE_OFF, allowed_tools=[])

    prompt = manager.build_system_prompt(agent)

    assert '<file name="SOUL.md">\nSoul text\n</file>' in prompt


def test_soul_block_prefixes_identity_framing_above_file_tag(
    workspace: Path, tmp_path: Path
) -> None:
    # SOUL is identity, not reference material: the framing line names it as such and
    # sits immediately above the file tag so the model reads it as its core contract.
    manager = _manager(tmp_path)
    agent = _agent(workspace, memory_prompt_mode=MEMORY_PROMPT_MODE_OFF, allowed_tools=[])

    prompt = manager.build_system_prompt(agent)

    assert f'{SOUL_FRAMING}\n\n<file name="SOUL.md">\nSoul text\n</file>' in prompt


def test_soul_framing_absent_when_block_gates_out(tmp_path: Path) -> None:
    # A config agent (workspace "") has no SOUL: the framing line must never render on
    # its own — it renders only as a prefix to a present SOUL, so it gates out with it.
    manager = _manager(tmp_path)
    agent = _agent("", memory_prompt_mode=MEMORY_PROMPT_MODE_OFF, allowed_tools=[])

    prompt = manager.build_system_prompt(agent)

    assert '<file name="SOUL.md">' not in prompt
    assert SOUL_FRAMING not in prompt


def test_soul_block_never_aborts_run_on_unreadable_file(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "SOUL.md").mkdir()  # a directory where the file is expected
    manager = _manager(tmp_path)
    agent = _agent(ws, memory_prompt_mode=MEMORY_PROMPT_MODE_OFF)

    with caplog.at_level(logging.WARNING):
        prompt = manager.build_system_prompt(agent)

    assert '<file name="SOUL.md">' not in prompt
    assert "Skipping unreadable workspace include" in caplog.text
