"""Render SOUL, Memory, and Project context without choosing Agent Tool policy."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

from core.memory import DEFAULT_MEMORY_PROMPT_MODE, read_memory_files
from core.prompts._types import (
    SOUL_FRAMING,
    SOUL_INCLUDE_MARKER,
    MemoryPromptProvider,
    ProjectContextSkill,
    ProjectPromptContext,
    PromptAgent,
    PromptFragmentReader,
)
from core.prompts.blocks import (
    PromptFileIssue,
    ToolAvailability,
    apply_replacements,
    expand_workspace_includes,
    render_prompt_file,
)
from core.tools.availability import MEMORY_TOOL_NAME, memory_tool_enabled
from core.tools.file_state import FileRead
from core.tools.model_names import model_tool_name
from core.utils.paths import model_path


def render_soul(
    agent: PromptAgent,
    tool_available: ToolAvailability,
    on_read: Callable[[FileRead], None] | None,
) -> str:
    rendered = expand_workspace_includes(
        SOUL_INCLUDE_MARKER,
        agent.workspace,
        tool_available=tool_available,
        on_read=on_read,
    )
    if not rendered:
        return ""
    return f"{SOUL_FRAMING}\n\n{rendered}"


def render_working_project(
    storage: PromptFragmentReader,
    project_context: ProjectPromptContext,
    tool_available: ToolAvailability,
    on_read: Callable[[FileRead], None] | None,
) -> str:
    project_files = [
        _indent_project_file_frame(block)
        for block in _project_file_blocks(project_context, tool_available, on_read)
    ]
    template = storage.read_prompt_fragment("working_project.md")
    return apply_replacements(
        template,
        {
            "{project_id}": project_context.project_id,
            "{project_name}": " ".join(project_context.project_name.split()),
            "{project_workspace}": model_path(project_context.cwd),
            "{project_files}": "\n\n".join(project_files),
        },
    ).strip()


def render_project_files(
    project_context: ProjectPromptContext | None,
    *,
    tool_available: ToolAvailability,
    on_read: Callable[[FileRead], None] | None = None,
    on_issue: Callable[[PromptFileIssue], None] | None = None,
) -> str:
    """Render the project's auto-loaded files as ``<file>``-wrapped blocks.

    The ``auto_load`` files in list order. AGENTS.md is no longer special — it
    is seeded as the first entry at project creation, so the list is the single
    source of what loads. Lazy: returns ``""`` when there is no project context
    or no readable file, so the placeholder collapses. *tool_available* tells
    which Tools the receiving Agent can call; a notice for an oversized file
    names ``read`` only when it can. See :func:`_project_file_blocks` for paths
    and :func:`render_prompt_file` for the size limit and fail-soft rules.

    ``on_read``, when given, receives the resolved path and pre-read metadata of every
    file actually inlined, so the caller can stamp it as read-before-write —
    used both for prompt assembly and the explicit ``project`` Tool.
    ``on_issue`` reports omissions so the Tool can explain partial loads.
    """
    if project_context is None:
        return ""
    return "\n".join(_project_file_blocks(project_context, tool_available, on_read, on_issue))


def render_project_skills(
    project_name: str,
    skills: Sequence[ProjectContextSkill],
    *,
    tool_available: ToolAvailability,
) -> str:
    """Render a Project's Skills in its explicit Tool-result context section.

    The current prompt-epoch System Prompt catalog remains unchanged. The explicit
    Project Context names each Skill and tells the Identity Agent to activate it
    through the ordinary ``skill`` Tool, whose Run-local resolver follows the
    latest successful Project Tool Result. Returns ``""`` when the Project has
    no Skills, so no empty section is emitted.
    """
    if not skills:
        return ""
    guidance = " - load one by name with the `skill` Tool" if tool_available("skill") else ""
    lines = [f"Skills from Project '{project_name}'{guidance}:"]
    lines.extend(
        f"- {skill.name}: {skill.description}"
        for skill in sorted(skills, key=lambda item: item.name)
    )
    return "\n".join(lines)


def _project_file_blocks(
    project_context: ProjectPromptContext,
    tool_available: ToolAvailability,
    on_read: Callable[[FileRead], None] | None,
    on_issue: Callable[[PromptFileIssue], None] | None = None,
) -> list[str]:
    """Return each existing auto-load file as a ``<file>`` block, in list order.

    The Working Project renderer and the explicit-Tool renderer share this, so
    path and fail-soft behavior cannot drift. Each path is used **as the user
    wrote it** in the project's auto-load list: a relative path resolves against
    the project ``cwd`` at any subfolder depth, an absolute path is read as-is.
    There is deliberately **no location restriction** — the auto-load list is
    the user's own config naming the user's own files (project philosophy:
    maximum agency, minimal restrictions). Missing files are omitted (the
    seeded AGENTS.md may not exist yet); the optional issue observer lets
    callers explain omissions. Every other rule is :func:`render_prompt_file`'s.
    """
    blocks: list[str] = []
    for filename in project_context.auto_load:
        block = render_prompt_file(
            project_context.cwd / filename,
            model_path(filename),
            tool_available=tool_available,
            on_read=on_read,
            on_issue=on_issue,
        )
        if block is not None:
            blocks.append(block)
    return blocks


def render_memory_files(
    provider: MemoryPromptProvider,
    agent: PromptAgent,
    tool_available: ToolAvailability,
    on_read: Callable[[FileRead], None] | None,
) -> str:
    mode = getattr(agent, "memory_prompt_mode", DEFAULT_MEMORY_PROMPT_MODE)
    workspace = agent.workspace
    if not workspace:
        return ""
    # Asked only when the mode renders Memory: resolving the Tool set is not free.
    memory_tool = (
        model_tool_name(MEMORY_TOOL_NAME)
        if memory_tool_enabled(mode) and tool_available(MEMORY_TOOL_NAME)
        else None
    )
    return read_memory_files(
        Path(workspace), mode, provider=provider, memory_tool=memory_tool, on_read=on_read
    )


def _indent_project_file_frame(block: str) -> str:
    """Indent only a Project file's wrapper tags, preserving its content exactly."""
    opening, separator, remainder = block.partition("\n")
    if not separator:
        return f" {block}"
    content, closing_separator, closing = remainder.rpartition("\n")
    if not closing_separator:
        return f" {opening}\n{remainder}"
    return f" {opening}\n{content}\n {closing}"
