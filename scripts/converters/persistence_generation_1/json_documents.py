"""Generation 1 conversion of the durable JSON documents in a data directory.

Every document gets a top-level ``format_version`` 1. Documents that were JSON
arrays become objects with a named array (Cron and Bootstrap ``jobs``, Calendar
``events``, prompt layout ``entries``, MCP ``connections``), the Skill policy and
Terminal documents move from ``version`` to ``format_version``, and the legacy
data Generation 1 no longer tolerates is normalized:

- Cron jobs lose the ignored per-job ``timezone`` and get the name vBot derived
  from their prompt at load time.
- A Project without ``allowed_tools`` gets the default Tool whitelist the
  pre-Generation-1 application applied to it, frozen here.
- An Identity Agent's retired ``allowed_tools`` becomes ``tool_access``, or is
  dropped when ``tool_access`` already exists.
- The retired ``grep`` and ``glob`` Tools become ``search_files`` in Agent Tool
  access, Project Tool whitelists and Project Agent overrides, without widening
  any policy (see ``_tool_access``).
- A Channel's retired ``owner_user_ids`` is dropped.
- Attachment sidecars (``artifacts/attachments/<id>.json``) lose the stored
  ``file_path``, which vBot derives from the data directory, and the retired
  ``text_content`` cache; speech artifact sidecars only get ``format_version``.

The MCP ``connections.json`` moves from ``mcp/`` into the MCP Extension's state
directory ``extension-data/mcp/``; the old file retires.

Unknown fields and invalid collection entries are carried over unchanged; the
application reports them. A document that already has ``format_version`` 1 is
not rewritten. A document that cannot be converted without guessing is left
unconverted and reported, so the application refuses it until it is repaired;
a moved document moves either way, since the application reads only its new
location.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.json_documents import FORMAT_VERSION_FIELD, render_json_document
from scripts.converters.persistence_generation_1._context import ConversionContext
from scripts.converters.persistence_generation_1._tool_access import (
    ToolAccessConversionError,
    consolidate_search_ceiling,
    consolidate_search_policy,
    convert_legacy_allowed_tools,
)

AREA = "json_documents"
FORMAT_VERSION = 1

# The pre-Generation-1 name derivation for a Cron job saved without a name,
# frozen here so the converted names match what vBot showed for those jobs.
_CRON_JOB_NAME_MAX_LENGTH = 80
_MARKDOWN_PREFIX_PATTERN = re.compile(r"^(?:(?:#{1,6}|>|[-*+])\s+|\d+[.)]\s+|\[[ xX]\]\s*)+")
_POLICY_LEGACY_VERSION = 2
# The Tool whitelist the pre-Generation-1 application applied to a Project
# without ``allowed_tools`` (its ``PROJECT_DEFAULT_ALLOWED_TOOLS`` from 0.4.4
# until the field became required; 0.4.0 to 0.4.3 used the retired ``write``,
# ``edit``, ``glob`` and ``grep`` for the same access). Frozen here, so a later
# change of the application default never changes what conversion grants.
_LEGACY_PROJECT_ALLOWED_TOOLS = (
    "read",
    "apply_patch",
    "search_files",
    "bash",
    "process",
    "terminal",
    "web_fetch",
    "web_search",
    "status",
    "subagent",
    "skill",
)
_TERMINAL_LEGACY_VERSION = 1


class _UnconvertibleError(Exception):
    """Raised when a document cannot be converted without guessing."""


@dataclass(frozen=True, slots=True)
class _Document:
    kind: str
    convert: Callable[[Any, _Notes], dict[str, Any]]
    # The Generation 1 location of a document that moved; its old file retires.
    target: str | None = None


@dataclass(frozen=True, slots=True)
class _Notes:
    """Report sink for one document."""

    context: ConversionContext
    relative: str

    def count(self, key: str) -> None:
        self.context.report.count(AREA, key)

    def approximate(self, reason: str) -> None:
        self.context.report.skip(AREA, self.relative, reason)


def convert(context: ConversionContext) -> None:
    """Stage the Generation 1 form of every JSON document in the source data directory."""
    for pattern, document in _DOCUMENTS:
        for path in sorted(context.source.glob(pattern)):
            if path.is_file():
                _convert_file(context, document, path)


def _convert_file(context: ConversionContext, document: _Document, path: Path) -> None:
    relative = path.relative_to(context.source).as_posix()
    target = document.target or relative
    moved = target != relative
    if moved and context.source_path(target).exists():
        context.report.skip(AREA, relative, f"left in place: {target} already exists")
        return
    body = _converted(context, document, path, relative)
    if body is not None:
        context.staged(target).write_text(
            render_json_document(body, version=FORMAT_VERSION), encoding="utf-8"
        )
        context.report.count(AREA, document.kind)
    elif moved:
        context.staged(target).write_bytes(path.read_bytes())
    if moved:
        context.retire(relative)
        context.report.count(AREA, "relocated")


def _converted(
    context: ConversionContext, document: _Document, path: Path, relative: str
) -> dict[str, Any] | None:
    """Return the converted body, or ``None`` for a document that stays as it is."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        context.report.skip(AREA, relative, f"left unconverted: unreadable JSON ({error})")
        return None
    if isinstance(value, dict) and FORMAT_VERSION_FIELD in value:
        if value[FORMAT_VERSION_FIELD] == FORMAT_VERSION:
            context.report.count(AREA, "already_current")
        else:
            context.report.skip(
                AREA,
                relative,
                f"left unconverted: unexpected format_version {value[FORMAT_VERSION_FIELD]!r}",
            )
        return None
    try:
        return document.convert(value, _Notes(context, relative))
    except _UnconvertibleError as error:
        context.report.skip(AREA, relative, f"left unconverted: {error}")
        return None


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _UnconvertibleError(f"expected a JSON object, got {type(value).__name__}")
    return dict(value)


def _array(value: Any) -> list[Any]:
    if not isinstance(value, list):
        raise _UnconvertibleError(f"expected a JSON array, got {type(value).__name__}")
    return list(value)


def _versioned(value: Any, legacy_version: int) -> dict[str, Any]:
    document = _object(value)
    version = document.pop("version", None)
    if version != legacy_version or isinstance(version, bool):
        raise _UnconvertibleError(f"expected version {legacy_version}, got {version!r}")
    return document


def _plain(value: Any, notes: _Notes) -> dict[str, Any]:
    return _object(value)


def _named_array(collection: str) -> Callable[[Any, _Notes], dict[str, Any]]:
    def convert(value: Any, notes: _Notes) -> dict[str, Any]:
        return {collection: _array(value)}

    return convert


def _agent(value: Any, notes: _Notes) -> dict[str, Any]:
    agent = _object(value)
    if "allowed_tools" in agent:
        legacy = agent.pop("allowed_tools")
        if "tool_access" in agent:
            notes.count("agent_allowed_tools_dropped")
            notes.approximate("retired allowed_tools dropped; the existing tool_access applies")
        else:
            try:
                policy = convert_legacy_allowed_tools(legacy)
            except ToolAccessConversionError as error:
                raise _UnconvertibleError(str(error)) from error
            agent["tool_access"] = policy.to_dict()
            notes.count("agent_allowed_tools_converted")
    if "tool_access" in agent:
        agent["tool_access"] = _search_policy(agent["tool_access"], "tool_access", notes)
    return agent


def _project(value: Any, notes: _Notes) -> dict[str, Any]:
    project = _object(value)
    if project.get("allowed_tools") is None:
        project["allowed_tools"] = list(_LEGACY_PROJECT_ALLOWED_TOOLS)
        notes.count("project_allowed_tools_filled")
    elif isinstance(project["allowed_tools"], list):
        ceiling, narrowed = consolidate_search_ceiling(project["allowed_tools"])
        if ceiling != project["allowed_tools"]:
            project["allowed_tools"] = ceiling
            notes.count("search_tools_consolidated")
        if narrowed is not None:
            notes.approximate(f"allowed_tools: {narrowed}")
    overrides = project.get("overrides")
    if isinstance(overrides, dict):
        project["overrides"] = {
            agent_id: _override(agent_id, override, notes)
            for agent_id, override in overrides.items()
        }
    return project


def _override(agent_id: str, override: Any, notes: _Notes) -> Any:
    if not isinstance(override, dict) or "tool_access" not in override:
        return override
    policy = _search_policy(override["tool_access"], f"overrides.{agent_id}.tool_access", notes)
    return {**override, "tool_access": policy}


def _search_policy(policy: Any, field: str, notes: _Notes) -> Any:
    """Consolidate the retired search Tools of one policy; an invalid one stays as it is."""
    if not isinstance(policy, dict):
        return policy
    try:
        converted, narrowed = consolidate_search_policy(policy)
    except ValueError:
        # The application reports the invalid policy; nothing here can repair it.
        return policy
    if converted != policy:
        notes.count("search_tools_consolidated")
    if narrowed is not None:
        notes.approximate(f"{field}: {narrowed}")
    return converted


def _channel(value: Any, notes: _Notes) -> dict[str, Any]:
    channel = _object(value)
    if "owner_user_ids" in channel:
        del channel["owner_user_ids"]
        notes.count("channel_owner_user_ids_dropped")
        notes.approximate(
            "retired owner_user_ids dropped; configure group admins in the Channel access settings"
        )
    return channel


def _cron_jobs(value: Any, notes: _Notes) -> dict[str, Any]:
    jobs: list[Any] = []
    for job in _array(value):
        if isinstance(job, dict):
            job = dict(job)
            if "timezone" in job:
                del job["timezone"]
                notes.count("cron_timezones_dropped")
            if not job.get("name") and "prompt" in job:
                job["name"] = _derive_cron_job_name(job["prompt"])
                notes.count("cron_names_derived")
        jobs.append(job)
    return {"jobs": jobs}


def _derive_cron_job_name(prompt: object) -> str:
    for line in str(prompt).splitlines():
        collapsed_line = " ".join(line.split())
        if not collapsed_line:
            continue
        without_markdown = _MARKDOWN_PREFIX_PATTERN.sub("", collapsed_line).strip()
        if without_markdown:
            return without_markdown[:_CRON_JOB_NAME_MAX_LENGTH]
    return "Scheduled Run"


def _skill_policy(value: Any, notes: _Notes) -> dict[str, Any]:
    return _versioned(value, _POLICY_LEGACY_VERSION)


def _terminal_document(value: Any, notes: _Notes) -> dict[str, Any]:
    return _versioned(value, _TERMINAL_LEGACY_VERSION)


def _attachment_metadata(value: Any, notes: _Notes) -> dict[str, Any]:
    metadata = _object(value)
    for retired in ("file_path", "text_content"):
        if retired in metadata:
            del metadata[retired]
            notes.count(f"attachment_{retired}_dropped")
    return metadata


def _mcp_connections(value: Any, notes: _Notes) -> dict[str, Any]:
    connections = _array(value)
    for connection in connections:
        if isinstance(connection, dict) and "agents" in connection:
            notes.approximate(
                f"MCP connection {connection.get('id')!r} keeps its retired agents field as "
                "an ignored unknown field; grant access through each Agent's tool_access"
            )
    return {"connections": connections}


_DOCUMENTS: tuple[tuple[str, _Document], ...] = (
    ("settings.json", _Document("settings", _plain)),
    ("agents/*/agent.json", _Document("agents", _agent)),
    ("agents/order.json", _Document("agent_order", _plain)),
    ("agents/*/prompts/layout.json", _Document("prompt_layouts", _named_array("entries"))),
    ("prompts/layout.json", _Document("prompt_layouts", _named_array("entries"))),
    ("projects/*/project.json", _Document("projects", _project)),
    ("channels/*/channel.json", _Document("channels", _channel)),
    ("cron/jobs.json", _Document("cron_jobs", _cron_jobs)),
    ("bootstrap/jobs.json", _Document("bootstrap_jobs", _named_array("jobs"))),
    ("calendar/events.json", _Document("calendar_events", _named_array("events"))),
    ("calendar/actions.json", _Document("calendar_actions", _plain)),
    ("skills/policy.json", _Document("skill_policy", _skill_policy)),
    ("terminals/launch-history.json", _Document("terminal_documents", _terminal_document)),
    ("terminals/groups.json", _Document("terminal_documents", _terminal_document)),
    ("oauth/*.json", _Document("oauth_tokens", _plain)),
    ("artifacts/attachments/*.json", _Document("attachment_metadata", _attachment_metadata)),
    ("artifacts/speech/*.json", _Document("speech_artifact_metadata", _plain)),
    (
        "mcp/connections.json",
        _Document(
            "mcp_connections", _mcp_connections, target="extension-data/mcp/connections.json"
        ),
    ),
)
