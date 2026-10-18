"""Skill package paging, activation payloads, and read failures."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from difflib import get_close_matches
from pathlib import Path, PurePosixPath

from core.skills import resolve_package_resource
from core.skills.skill_validator import split_skill_document
from core.skills.skills import (
    format_skill_activation_context,
    scan_skill_resources,
)
from core.tools._read_text import ReadPosition, render_text_window
from core.tools.shell import format_shell_env_usage
from core.tools.tools import (
    JsonObject,
    tool_failure,
    tool_success,
)
from core.utils.file_status import is_file_strict

# Marks a Skill in a background Run's list that the Run cannot change.
SKILL_READ_ONLY_MARKS = {
    "pinned": "read-only here: pinned by the user",
    "unknown": "read-only here: its history cannot be read",
}
# Opens a background Run's load of a Skill it cannot change. ``{mark}`` is a
# read-only mark or ``read-only here`` for a Skill that is not the Agent's own.
SKILL_READ_ONLY_NOTE = (
    "Skill '{name}' is {mark}. Do not change it; name a needed change in your reply."
)

# A name whose Skill was deleted into the archive. ``{date}`` is YYYY-MM-DD.
SKILL_ARCHIVED_MESSAGE = (
    "Skill '{name}' was {reason} on {date} and cannot be loaded. The user can restore it "
    "in the Skill controls. Call skill without arguments to list the available Skills."
)
SKILL_ABSORBED_NOTE = (
    "Skill '{name}' was merged into Skill '{target}' on {date}; these are the instructions "
    "of '{target}'."
)
SKILL_ABSORBED_FILE_MESSAGE = (
    "Skill '{name}' was merged into Skill '{target}' on {date} and has no files of its own "
    "anymore. Load '{target}' with skill and read its files instead."
)
_ARCHIVE_REASONS = {
    "deleted": "deleted",
    "inactive": "retired after a long time without use",
    "published": "made a global Skill",
}

SKILL_TOOL_NAME = "skill"
SKILL_TOOL_DESCRIPTION = "List available Skills, load one Skill, or read one file from it."
SKILL_STATUS_LOADED = "loaded"
SKILL_STATUS_ALREADY_ACTIVE = "already_active"
SKILL_STATUS_FILE_LOADED = "file_loaded"
SKILL_STATUS_CONTINUED = "continued"
# OpenClaw-compatible marker skill authors may use in the body to reference bundled
# files (e.g. ``python {baseDir}/scripts/run.py``); replaced with the absolute skill
# directory at activation time.
SKILL_BASE_DIR_MARKER = "{baseDir}"
SKILL_RESOURCE_FILES_GUIDANCE = (
    "Paths are relative to the Skill directory below. Read text with skill using this name "
    "and file_path when needed for the task. "
    "Use scripts and assets as directed by the instructions."
)
# Leads a Skill's instructions that exceed one ``read``-sized page (50 KB or
# 2000 lines); the page ends with the exact call that continues it.
SKILL_PARTIAL_INSTRUCTIONS_NOTE = (
    "[Only the first part of these instructions is below. Before you follow them, "
    "read the rest with the call at the end.]"
)
# Invocation marks a Model copies from a user's `/name` or `$name` Skill request.
_SKILL_NAME_MARKS = "/$@"


def clean_skill_file_path(path: str) -> str:
    """Remove quoting, leading ``./`` and Windows separators from a package path."""
    text = path.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"', "`"):
        text = text[1:-1].strip()
    text = text.replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text


def skill_name_key(name: str) -> str:
    """Comparison key that ignores case, separators, quotes and invocation marks."""
    text = name.strip().strip("'\"`").lstrip(_SKILL_NAME_MARKS)
    return re.sub(r"[\s_-]+", "-", text.casefold()).strip("-")


def load_skill_content(
    skill_name: str,
    skill_file: Path,
    *,
    env_keys: Sequence[str] = (),
) -> JsonObject:
    """Load the instruction body and activation metadata for one Skill file.

    Instructions longer than one page (50 KB or 2000 lines, as ``read`` pages a
    file) load as their first page, led by ``SKILL_PARTIAL_INSTRUCTIONS_NOTE`` and
    ended by the ``skill`` call that continues them. Both the Tool result and the
    Session's Skill context carry only that page.
    """
    skill_directory = skill_file.resolve().parent
    directory = skill_directory.as_posix()
    body, partial = _skill_instructions_window(skill_name, skill_file, ReadPosition(1))
    if partial:
        body = f"{SKILL_PARTIAL_INSTRUCTIONS_NOTE}\n\n{body}"
    resources = scan_skill_resources(skill_directory)
    presented_resources = [_present_resource_path(resource, directory) for resource in resources]
    environment_access = ""
    if env_keys:
        environment_access = format_shell_env_usage(
            env_keys,
            intro=(
                "Loading this Skill makes these additional environment credentials "
                "available to shell commands."
            ),
        )
    activation_content = format_skill_activation_context(
        skill_name,
        body,
        resource_files=presented_resources,
        resource_guidance=SKILL_RESOURCE_FILES_GUIDANCE,
        resource_directory=directory,
        environment_access=environment_access,
    )
    result: JsonObject = {
        "content": body,
        "activation_content": activation_content,
    }
    if presented_resources:
        result["resource_files"] = {
            "guidance": SKILL_RESOURCE_FILES_GUIDANCE,
            "directory": directory,
            "files": presented_resources,
        }
    if environment_access:
        result["environment_access"] = environment_access
    return result


def _load_skill_content_with_env(
    skill_name: str,
    skill_file: Path,
    env_keys: Sequence[str],
) -> JsonObject:
    return load_skill_content(skill_name, skill_file, env_keys=env_keys)


def load_skill_instructions_page(skill_name: str, skill_file: Path, position: ReadPosition) -> str:
    """Return the page of a Skill's instructions that starts at *position*."""
    page, _ = _skill_instructions_window(skill_name, skill_file, position)
    return page


def _skill_instructions_window(
    skill_name: str, skill_file: Path, position: ReadPosition
) -> tuple[str, bool]:
    """Page the instruction body exactly as activation presents it.

    Line numbers count lines of the body after its frontmatter, with ``{baseDir}``
    replaced, so an activation's continuation offset addresses the same text.
    """
    directory = skill_file.resolve().parent.as_posix()
    body = _read_skill_body(skill_name, skill_file).replace(SKILL_BASE_DIR_MARKER, directory)
    return render_text_window(
        body,
        position.line,
        None,
        number=False,
        start_character=position.character,
        continuation=_continuation_call(skill_name),
    )


def _continuation_call(skill_name: str, file_path: str | None = None) -> str:
    """Name the ``skill`` call that continues a page; ``{offset}`` stays a placeholder."""
    fields = [f"name={json.dumps(skill_name, ensure_ascii=False)}"]
    if file_path is not None:
        fields.append(f"file_path={json.dumps(file_path, ensure_ascii=False)}")
    fields.append("offset={offset}")
    return f"Continue with {SKILL_TOOL_NAME}({', '.join(fields)})."


def load_skill_file(
    skill_name: str,
    skill_file: Path,
    file_path: str,
    position: ReadPosition | None = None,
) -> JsonObject:
    """Read one page of a UTF-8 package file by skill-relative path.

    A file longer than one page (50 KB or 2000 lines) returns the page that starts
    at *position* (default: the first line), ended by the ``skill`` call that
    continues it.
    """
    file_path = _package_relative_path(file_path, skill_name, skill_file.parent)
    normalized = file_path.replace("\\", "/")
    skill_directory = skill_file.resolve().parent
    try:
        candidate = resolve_package_resource(skill_directory, normalized)
    except ValueError as error:
        raise ValueError(_invalid_file_message(skill_name, file_path)) from error
    if not is_file_strict(candidate):
        raise ValueError(_missing_file_message(skill_name, skill_directory, normalized))
    try:
        content = candidate.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(
            f"Skill '{skill_name}' file '{normalized}' is not UTF-8 text. "
            "skill reads UTF-8 text only. For a binary asset, use the file at "
            f"'{candidate.as_posix()}' as directed by the Skill; "
            "for a text file, save it as UTF-8 before reading it again."
        ) from error
    start = position or ReadPosition(1)
    page, _ = render_text_window(
        content,
        start.line,
        None,
        number=False,
        start_character=start.character,
        continuation=_continuation_call(skill_name, normalized),
    )
    return {"name": skill_name, "file_path": normalized, "content": page}


def _loaded_skill_result(skill_name: str, loaded: JsonObject, notes: list[str]) -> JsonObject:
    """Success envelope of a fresh activation — the tool result IS the content carrier.

    The raw SKILL.md instruction body rides in ``data.content`` while optional
    resource files and environment access stay in sibling fields. The sessions
    domain parses this envelope shape (``skill_tool_activation``) for dedup,
    statistics, and post-compaction re-injection — keep
    ``name``/``status``/``content`` stable.
    """
    data: JsonObject = {"name": skill_name, "status": SKILL_STATUS_LOADED}
    # Notes precede the instructions, like in the other load results.
    if notes:
        data["note"] = " ".join(notes)
    data["content"] = loaded["content"]
    resource_files = loaded.get("resource_files")
    if isinstance(resource_files, dict):
        data["resource_files"] = resource_files
    environment_access = loaded.get("environment_access")
    if isinstance(environment_access, str) and environment_access:
        data["environment_access"] = environment_access
    return tool_success(data)


def _loaded_skill_file_result(
    skill_name: str,
    file_path: str,
    content: str,
    notes: list[str],
) -> JsonObject:
    data: JsonObject = {
        "name": skill_name,
        "status": SKILL_STATUS_FILE_LOADED,
        "file_path": file_path,
    }
    if notes:
        data["note"] = " ".join(notes)
    data["content"] = content
    return tool_success(data)


def _continued_skill_result(skill_name: str, content: str, notes: list[str]) -> JsonObject:
    """A later page of a Skill's instructions; it activates nothing."""
    data: JsonObject = {"name": skill_name, "status": SKILL_STATUS_CONTINUED}
    if notes:
        data["note"] = " ".join(notes)
    data["content"] = content
    return tool_success(data)


def _already_active_result(skill_name: str, notes: list[str]) -> JsonObject:
    data: JsonObject = {
        "name": skill_name,
        "status": SKILL_STATUS_ALREADY_ACTIVE,
        "message": (
            f"Skill '{skill_name}' is already active in this session; "
            "its instructions are already in context."
        ),
    }
    if notes:
        data["note"] = " ".join(notes)
    return tool_success(data)


def _package_relative_path(file_path: str, skill_name: str, skill_directory: Path) -> str:
    """Map an absolute path inside the package, or a ``name/...`` path, to its relative path."""
    directory = skill_directory.resolve()
    if PurePosixPath(file_path).is_absolute() or re.match(r"^[A-Za-z]:/", file_path):
        try:
            return Path(file_path).resolve().relative_to(directory).as_posix()
        except OSError, ValueError:
            return file_path
    first, _, rest = file_path.partition("/")
    if (
        rest
        and skill_name_key(first) == skill_name_key(skill_name)
        and not (directory / file_path).is_file()
        and (directory / rest).is_file()
    ):
        return rest
    return file_path


def _read_skill_body(skill_name: str, skill_file: Path) -> str:
    try:
        content = skill_file.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(
            f"Skill '{skill_name}' SKILL.md is not UTF-8 text. "
            "Save SKILL.md as UTF-8 before loading it again."
        ) from error
    _, body, _ = split_skill_document(content)
    return body.strip()


def _invalid_file_message(skill_name: str, file_path: str) -> str:
    return (
        f"Cannot read '{file_path}' from Skill '{skill_name}': "
        "use a path from this Skill's resource list, relative to its directory, "
        "without '..' or internal files. To read its instructions, call "
        f'skill(name={json.dumps(skill_name)}, file_path="SKILL.md").'
    )


def _missing_file_message(skill_name: str, directory: Path, file_path: str) -> str:
    lead = f"Skill '{skill_name}' file not found: {file_path}."
    candidates = scan_skill_resources(directory)
    if is_file_strict(directory / "SKILL.md"):
        candidates.insert(0, "SKILL.md")
    suggestions = get_close_matches(file_path, candidates, n=3, cutoff=0.6)
    if len(suggestions) == 1:
        return (
            f"{lead} Did you mean '{suggestions[0]}'? Read it with "
            f"skill(name={json.dumps(skill_name)}, file_path={json.dumps(suggestions[0])})."
        )
    if suggestions:
        return (
            f"{lead} Similar files: {', '.join(suggestions)}. Use one as file_path with this name."
        )
    if candidates:
        shown = ", ".join(candidates[:8])
        more = " See this Skill's resource list for more." if len(candidates) > 8 else ""
        return f"{lead} Available files: {shown}. Use one as file_path with this name.{more}"
    return (
        f"{lead} No readable files were found in this package. "
        "Restore the Skill's files before retrying."
    )


def _skill_read_failure(skill_name: str, file_path: str, error: OSError) -> JsonObject:
    lead = f"Cannot read Skill '{skill_name}' file '{file_path}': "
    if getattr(error, "winerror", None) in {32, 33}:
        detail = "the file is locked. Close the program using it, then retry."
    elif isinstance(error, PermissionError):
        detail = "access was denied. Restore read access to the Skill's files before retrying."
    elif isinstance(error, FileNotFoundError):
        detail = (
            "the file is missing. Restore it or call skill without arguments "
            "to choose another Skill."
        )
    elif isinstance(error, IsADirectoryError):
        detail = "the path is a directory. Choose a file from the Skill's resource paths."
    else:
        detail = (
            "a filesystem error prevented the read. Retry once; "
            "if it persists, report the unreadable file to the user."
        )
    return tool_failure("skill_read_error", lead + detail)


def _present_resource_path(resource: str, directory: str) -> str:
    if PurePosixPath(resource).parts[0] == "scripts":
        return f"{directory}/{resource}"
    return resource
