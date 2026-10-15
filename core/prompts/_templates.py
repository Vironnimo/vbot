"""Single-pass prompt templates and bounded automatic file inclusion."""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal

from core.prompts._block_types import (
    MAPPING_PROXY_EMPTY,
    BlockProducer,
    BlockRenderContext,
    PromptError,
    ToolAvailability,
)
from core.tools import READ_TOOL_NAME, model_tool_name
from core.utils.log_conditions import LoggedConditions
from core.utils.logging import get_logger
from core.utils.paths import model_path

_LOGGER = get_logger("prompts")
# Unknown markers the process has already logged.
_TEMPLATE_CONDITIONS = LoggedConditions()
# Missing, unreadable or oversized prompt files the process has already logged.
_FILE_CONDITIONS = LoggedConditions()

# ``{generated:NAME}`` injects a producer's output into an editable text block.
# An unknown NAME renders to "" and warns (fail-soft, like a missing include).
GENERATED_PATTERN = re.compile(r"\{generated:([^{}]+)\}")
# ``{include:filename}`` pulls a flat, safe workspace file into a text block.
INCLUDE_PATTERN = re.compile(r"\{include:([^{}]+)\}")


def _render_generated_marker(
    name: str,
    producers: Mapping[str, BlockProducer],
    context: BlockRenderContext,
) -> str:
    """Return one ``{generated:NAME}`` producer's output, fail-soft.

    Unknown markers and failing producers render to ``""`` and log a warning —
    mirroring a missing ``{include:…}``; neither is a :class:`PromptError`. An
    unknown marker warns once per process, not on every build.
    """
    producer = producers.get(name)
    if producer is None:
        # Every build meets the same template: warn once per marker, not per build.
        if _TEMPLATE_CONDITIONS.started(("generated", name)):
            _LOGGER.warning("Skipped unknown generated marker (marker=%s)", name)
        return ""
    try:
        return producer(context)
    except Exception as exc:  # noqa: BLE001 - one producer drops, never the Run
        _LOGGER.warning(
            "Skipping generated marker {generated:%s}; producer failed: %s",
            name,
            exc,
        )
        return ""


def _render_workspace_include(
    filename: str,
    workspace: str,
    on_read: Callable[[Path], None] | None,
    tool_available: ToolAvailability,
) -> str:
    """Return one ``{include:filename}`` expansion, fail-soft.

    An empty workspace means "no includes": the marker is dropped with no read and
    no warning — it must never resolve against ``Path("")`` (= ``Path(".")``), which
    would read SOUL.md/USER.md from the server's process CWD. A safe flat filename
    resolves under the workspace and renders through :func:`render_prompt_file`:
    ``<file>``-wrapped, an oversized file as its notice, a missing **or**
    unreadable file dropped with a warning (a prompt file never aborts a run —
    user decision). An **unsafe** include path raises :class:`PromptError` — that
    is a malformed directive, not a readability issue.
    """
    if not workspace:
        return ""
    validate_workspace_include(filename)
    rendered = render_prompt_file(
        Path(workspace) / filename,
        filename,
        tool_available=tool_available,
        on_read=on_read,
        warn_missing=True,
    )
    return rendered or ""


def expand_block_template(
    text: str,
    context: BlockRenderContext,
    *,
    producers: Mapping[str, BlockProducer],
    replacements: Mapping[str, str] = MAPPING_PROXY_EMPTY,
) -> str:
    """Expand an editable text block's template in exactly one pass.

    The template is tokenized once for all three marker kinds — ``{generated:NAME}``
    (producers), ``{include:filename}`` (workspace files) and the exact build-time
    ``replacements`` keys (runtime variables such as ``{model}``) — and every match
    is substituted by its expansion **verbatim**. Inserted text is never scanned
    again: a Producer's output (Skill descriptions, Memory entries, Tool list), an
    included file's content, or a replacement value that itself contains
    ``{include:…}``, ``{generated:…}`` or ``{model}`` stays literal. Only the
    author-controlled template can direct expansion, so user- or Agent-authored data
    can neither read workspace files, fail the build with an unsafe include, nor
    have runtime values substituted into it.

    Marker semantics are unchanged: generated markers and includes are fail-soft
    (see :func:`_render_generated_marker`/:func:`_render_workspace_include`); an
    unsafe include path in the template raises :class:`PromptError`; unrelated
    ``{…}`` stays literal. ``context.read_observer`` receives every inlined file.
    """
    placeholders = [placeholder for placeholder in replacements if placeholder]
    alternatives = [GENERATED_PATTERN.pattern, INCLUDE_PATTERN.pattern]
    alternatives.extend(re.escape(placeholder) for placeholder in placeholders)
    pattern = re.compile("|".join(alternatives))
    workspace = context.agent.workspace

    def replace(match: re.Match[str]) -> str:
        generated_name, include_name = match.group(1), match.group(2)
        if generated_name is not None:
            return _render_generated_marker(generated_name.strip(), producers, context)
        if include_name is not None:
            return _render_workspace_include(
                include_name.strip(), workspace, context.read_observer, context.tool_available
            )
        return replacements[match.group(0)]

    return pattern.sub(replace, text)


def expand_workspace_includes(
    text: str,
    workspace: str,
    *,
    tool_available: ToolAvailability,
    on_read: Callable[[Path], None] | None = None,
) -> str:
    """Replace every ``{include:filename}`` with the workspace file, fail-soft.

    The include-only form of :func:`expand_block_template`, used for the SOUL data
    block. Included content is inserted verbatim and never scanned again. See
    :func:`_render_workspace_include` for the empty-workspace, missing/unreadable
    and unsafe-path rules, and :func:`render_prompt_file` for the size limit.

    ``on_read``, when given, is called with the resolved absolute path of every file
    whose content is actually inlined (never for a missing/unreadable/oversized
    one), so a caller can register the inlined file as read-before-write.
    """
    return INCLUDE_PATTERN.sub(
        lambda match: _render_workspace_include(
            match.group(1).strip(), workspace, on_read, tool_available
        ),
        text,
    )


# Largest text file vBot inlines whole into a Model's Context without being asked
# per request: an ``@``-mentioned file, each Project auto-load file and each
# workspace include (SOUL included). About 32k tokens: far above real instruction
# files, so it only stops a runaway file (a generated dump, a misconfigured path)
# from flooding the Context of every request.
INLINE_FILE_MAX_BYTES = 128 * 1024

# Stands in for a file above ``INLINE_FILE_MAX_BYTES`` inside its ``<file>`` frame,
# so the Agent learns that the file exists and why its content is missing.
# ``{read_hint}`` is ``FILE_READ_HINT`` when the Agent can call ``read``, else "".
FILE_TOO_LARGE_NOTICE = (
    "[Not included: this file has {size:,} bytes, more than the {limit:,}-byte limit "
    "for automatically loaded files.{read_hint}]"
)
# Names the file's absolute path: the Agent's relative paths can resolve against
# another directory than the one the file was loaded from.
FILE_READ_HINT = (
    " When your work needs its content, read `{path}` with the `{read_tool}` Tool, "
    "which returns large files in pages."
)


@dataclass(frozen=True)
class PromptFileIssue:
    """Why an automatic file load omitted content; the caller owns its presentation."""

    path: Path
    name: str
    reason: Literal["missing", "access_denied", "not_file", "encoding", "unreadable", "too_large"]


def render_prompt_file(
    path: Path,
    name: str,
    *,
    tool_available: ToolAvailability,
    on_read: Callable[[Path], None] | None = None,
    warn_missing: bool = False,
    on_issue: Callable[[PromptFileIssue], None] | None = None,
) -> str | None:
    """Return one file as a ``<file name="…">`` block for a Model's Context.

    The single reader for every file vBot loads into a Model's Context on its
    own: workspace ``{include:…}`` files (SOUL included) and the Project auto-load
    files of the Working Project block and the ``project`` Tool. *name* is the
    frame's ``name`` attribute.

    - A file above ``INLINE_FILE_MAX_BYTES`` keeps its frame but holds
      ``FILE_TOO_LARGE_NOTICE`` instead of its content; only the first
      ``INLINE_FILE_MAX_BYTES + 1`` bytes are ever read. The notice points to the
      ``read`` Tool only when *tool_available* says the Agent can call it.
    - A missing file returns ``None``; with *warn_missing* it also warns.
    - A file that cannot be read or is not UTF-8 text returns ``None`` and warns.
      A prompt file never aborts a Run (user decision).

    Each condition logs once when it starts or changes and once when the file
    loads whole again, never on every render, and never with file content.
    *on_read*, when given, receives the resolved absolute path only when the
    content is inlined whole, so the caller can stamp it as read-before-write.
    *on_issue* reports omitted content without changing fail-soft rendering.
    """
    # Keyed with the missing-file policy, so a file that is both included and
    # auto-loaded cannot flip one condition between logged and silent on every build.
    condition = (warn_missing, str(path))
    try:
        with path.open("rb") as handle:
            raw = handle.read(INLINE_FILE_MAX_BYTES + 1)
            size = max(os.fstat(handle.fileno()).st_size, len(raw))
        # Text mode's universal newlines, exactly as ``read_text`` applies them.
        content = (
            None
            if len(raw) > INLINE_FILE_MAX_BYTES
            else raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
        )
    except FileNotFoundError:
        if on_issue is not None:
            on_issue(PromptFileIssue(path, name, "missing"))
        if not warn_missing:
            # An optional file that does not exist yet is normal, not a condition.
            _FILE_CONDITIONS.ended(condition)
        elif _FILE_CONDITIONS.started(condition, "missing"):
            _LOGGER.warning("Skipped missing prompt file (path=%s)", path)
        return None
    except (OSError, ValueError) as exc:
        # Present but unreadable for ANY reason (locked, no permission, a
        # directory, binary/non-UTF-8, a malformed path).
        if _FILE_CONDITIONS.started(condition, type(exc).__name__):
            _LOGGER.warning("Skipped unreadable prompt file (path=%s): %s", path, exc)
        if on_issue is not None:
            reason: Literal["access_denied", "not_file", "encoding", "unreadable"] = "unreadable"
            if isinstance(exc, IsADirectoryError) or (
                isinstance(exc, PermissionError) and path.is_dir()
            ):
                reason = "not_file"
            elif isinstance(exc, PermissionError):
                reason = "access_denied"
            elif isinstance(exc, UnicodeError):
                reason = "encoding"
            on_issue(PromptFileIssue(path, name, reason))
        return None
    if content is None:
        if on_issue is not None:
            on_issue(PromptFileIssue(path, name, "too_large"))
        if _FILE_CONDITIONS.started(condition, "too_large"):
            _LOGGER.warning(
                "Replaced oversized prompt file with a notice (path=%s bytes=%d limit=%d)",
                path,
                size,
                INLINE_FILE_MAX_BYTES,
            )
        read_hint = (
            FILE_READ_HINT.format(
                path=model_path(os.path.abspath(path)),
                read_tool=model_tool_name(READ_TOOL_NAME),
            )
            if tool_available(READ_TOOL_NAME)
            else ""
        )
        notice = FILE_TOO_LARGE_NOTICE.format(
            size=size, limit=INLINE_FILE_MAX_BYTES, read_hint=read_hint
        )
        return wrap_include_file(name, notice)
    if _FILE_CONDITIONS.ended(condition):
        _LOGGER.info("Loaded prompt file whole again (path=%s)", path)
    if on_read is not None:
        on_read(path.resolve())
    return wrap_include_file(name, content)


def wrap_include_file(filename: str, content: str) -> str:
    """Wrap a file's content in the canonical ``<file name="…">`` block.

    The single wrap used by workspace includes, project files, and the
    ``memory_files`` producer, so the rendering paths cannot drift in framing.
    """
    return f'<file name="{filename}">\n{content}\n</file>'


def validate_workspace_include(filename: str) -> None:
    """Raise :class:`PromptError` unless *filename* is a safe flat name.

    Checks both POSIX and Windows semantics so separators and drive prefixes of
    either platform are rejected on any host (deployment is Linux, development is
    Windows). Only a bare filename in the workspace root is accepted.
    """
    posix_path = PurePosixPath(filename)
    windows_path = PureWindowsPath(filename)
    if (
        posix_path.name != filename
        or posix_path.is_absolute()
        or windows_path.name != filename
        or windows_path.is_absolute()
    ):
        raise PromptError(f"Unsafe workspace include: {filename}")


def apply_replacements(text: str, replacements: Mapping[str, str]) -> str:
    """Replace known prompt placeholders in *text* once, exactly, and non-recursively.

    This is the single substitution home for runtime variables and file-backed
    dynamic templates. Only exact mapping keys are replaced; unrelated ``{…}``
    remains literal. A replacement value is emitted verbatim and never scanned
    again, so inserted Project files or metadata cannot trigger another placeholder.
    """
    if not replacements:
        return text
    placeholders = [placeholder for placeholder in replacements if placeholder]
    if not placeholders:
        return text
    pattern = re.compile("|".join(re.escape(placeholder) for placeholder in placeholders))
    return pattern.sub(lambda match: replacements[match.group(0)], text)
