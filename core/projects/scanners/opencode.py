"""OpenCode agent detector: reads ``.opencode/agents/`` at the project root.

OpenCode stores one agent per Markdown file under ``.opencode/agents/`` with YAML
front matter (``description``, optional ``model``, ``temperature``, …) and a body
that is the agent's system prompt. This detector reads **that location only**,
**non-recursively** (no full-tree walk, no nested-repo pickup), and maps each
file to a :class:`ScannedAgent` per the v1 minimal rule (see add-projects.md →
"OpenCode-Agent lesen"):

- ``agent_id`` = filename stem **slugified** (``slugify_agent_id``); a name that
  cannot be slugified becomes a parse failure the report turns into a finding.
- ``description`` / ``temperature`` taken from the front matter.
- ``thinking_effort`` taken from the front matter's ``reasoningEffort`` (the
  OpenCode key), defensively normalized to vBot's effort ladder; an unknown or
  empty value becomes ``None`` so a foreign effort never crashes the scan.
- ``model`` taken **1:1** (``<provider>/<model-id>``), never rewritten; may be
  empty. Model existence/configuration is judged later by the resolver, not here.
- ``body`` = the file body after the front matter, **verbatim** (opaque text;
  ``{...}`` is not expanded here).
- ``denied_tools`` = the set of vBot tools this agent turns off, parsed from the
  front matter ``permission`` and ``tools`` maps (see :func:`_denied_tools`). Per
  the v1 max-agency reading, granularity is ignored: a tool is off only on an
  unambiguous full deny. The OpenCode **global** config file is out of scope — only
  this per-agent file is read.

Skills are not narrowed per agent in v1 (``permission.skill`` is out of scope), so
the profile carries no skill list; a config agent's skills are resolved from the
project. Everything else in the front matter stays out in v1.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from core.projects.paths import slugify_agent_id
from core.projects.scanners.base import DetectedFile, ScannedAgent
from core.settings import ALLOWED_THINKING_EFFORTS
from core.utils.logging import get_logger

_LOGGER = get_logger("projects")

# OpenCode's known, fixed location relative to the project root. Scanned
# non-recursively — only direct ``*.md`` files here, never subdirectories.
OPENCODE_AGENTS_SUBPATH = (".opencode", "agents")
OPENCODE_FORMAT_KEY = "opencode"
_AGENT_FILE_GLOB = "*.md"
_FRONT_MATTER_DELIMITER = "---"
_OPENCODE_DENY = "deny"

# OpenCode ``permission`` key → the vBot tools a full deny on that key turns off.
# ``edit`` covers both edit and write (no ``write`` permission key exists); ``bash``
# covers bash and process (OpenCode ``bash`` maps to both, grant/deny together);
# ``task`` governs the subagent tool. Keys without a vBot counterpart (``list``,
# ``lsp``, ``todowrite``, ``question``, ``external_directory``, ``doom_loop``,
# ``skill``) are absent here and therefore ignored.
_PERMISSION_DENY_MAP: dict[str, frozenset[str]] = {
    "edit": frozenset({"edit", "write"}),
    "bash": frozenset({"bash", "process"}),
    "read": frozenset({"read"}),
    "grep": frozenset({"grep"}),
    "glob": frozenset({"glob"}),
    "webfetch": frozenset({"web_fetch"}),
    "websearch": frozenset({"web_search"}),
    "task": frozenset({"subagent"}),
}

# OpenCode ``tools`` map name → the vBot tools a ``false`` entry turns off. Differs
# from the permission map only on edit/write: ``tools.edit`` and ``tools.write`` are
# separate names, so each denies its own vBot tool, whereas ``permission.edit``
# covers both. Unmapped tool names (``list``, ``lsp``, ``todowrite``,
# ``external_directory``) are ignored.
_TOOLS_DENY_MAP: dict[str, frozenset[str]] = {
    "edit": frozenset({"edit"}),
    "write": frozenset({"write"}),
    "bash": frozenset({"bash", "process"}),
    "read": frozenset({"read"}),
    "grep": frozenset({"grep"}),
    "glob": frozenset({"glob"}),
    "webfetch": frozenset({"web_fetch"}),
    "websearch": frozenset({"web_search"}),
    "task": frozenset({"subagent"}),
}


class OpenCodeDetector:
    """Detector for the OpenCode agent format. One instance per scan is fine."""

    @property
    def format_key(self) -> str:
        """The stable format key used for precedence and reporting."""
        return OPENCODE_FORMAT_KEY

    def detect(self, project_root: Path) -> list[DetectedFile]:
        """Read ``.opencode/agents/*.md`` under ``project_root``, non-recursively.

        Returns one :class:`DetectedFile` per ``*.md`` file, **sorted stably by
        filename** (never filesystem order). A missing location yields an empty
        list. Each file becomes either a parsed :class:`ScannedAgent` or a parse
        failure (unslugifiable name / unreadable file) for the report.
        """
        agents_dir = project_root.joinpath(*OPENCODE_AGENTS_SUBPATH)
        if not agents_dir.is_dir():
            return []

        # Sort by filename so the order is deterministic across hosts; only direct
        # children (glob, not rglob) so nested directories are never walked.
        agent_files = sorted(
            (path for path in agents_dir.glob(_AGENT_FILE_GLOB) if path.is_file()),
            key=lambda path: path.name,
        )
        return [self._read_agent_file(path) for path in agent_files]

    def _read_agent_file(self, path: Path) -> DetectedFile:
        raw_name = path.stem
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as error:
            _LOGGER.warning("Could not read OpenCode agent file %s: %s", path, error)
            return DetectedFile(
                source_path=path,
                raw_name=raw_name,
                error_reason=f"could not read agent file: {error}",
            )

        try:
            agent_id = slugify_agent_id(raw_name)
        except ValueError as error:
            return DetectedFile(source_path=path, raw_name=raw_name, error_reason=str(error))

        front_matter, body = _split_front_matter(content)
        fields = _parse_front_matter(front_matter, path)

        agent = ScannedAgent(
            agent_id=agent_id,
            display_name=raw_name,
            description=_string_field(fields.get("description")),
            model=_string_field(fields.get("model")),
            temperature=_temperature_field(fields.get("temperature")),
            thinking_effort=_thinking_effort_field(fields.get("reasoningEffort")),
            body=body,
            source_format=OPENCODE_FORMAT_KEY,
            source_path=path,
            denied_tools=_denied_tools(fields),
        )
        return DetectedFile(source_path=path, raw_name=raw_name, agent=agent)


def _split_front_matter(content: str) -> tuple[str, str]:
    """Split a Markdown file into (front matter, body), preserving the body verbatim.

    Recognizes a leading ``---`` fence and returns the text up to the closing
    ``---`` as front matter and everything after it as the body, **unchanged**
    (a single leading newline after the closing fence is dropped so the body does
    not start with a blank line, but its content — including any ``{...}`` — is
    otherwise untouched). A file without a proper fence has an empty front matter
    and the whole content as body.
    """
    lines = content.splitlines(keepends=True)
    if not lines or lines[0].strip() != _FRONT_MATTER_DELIMITER:
        return "", content

    for index in range(1, len(lines)):
        if lines[index].strip() == _FRONT_MATTER_DELIMITER:
            front_matter = "".join(lines[1:index])
            body = "".join(lines[index + 1 :])
            return front_matter, _strip_one_leading_newline(body)

    # Unterminated front matter: treat the whole file as body so nothing is lost.
    return "", content


def _strip_one_leading_newline(body: str) -> str:
    if body.startswith("\r\n"):
        return body[2:]
    if body.startswith("\n"):
        return body[1:]
    return body


def _parse_front_matter(front_matter: str, path: Path) -> dict[str, Any]:
    if not front_matter.strip():
        return {}
    try:
        loaded = yaml.safe_load(front_matter)
    except yaml.YAMLError as error:
        _LOGGER.warning("Invalid YAML front matter in %s: %s", path, error)
        return {}
    if not isinstance(loaded, dict):
        return {}
    return loaded


def _string_field(value: Any) -> str:
    """Return a trimmed string for a scalar field, or ``""`` for anything else."""
    if isinstance(value, str):
        return value.strip()
    return ""


def _temperature_field(value: Any) -> float | None:
    """Return a float temperature, or ``None`` when absent/non-numeric.

    Booleans are rejected (``bool`` is a subclass of ``int``) so a stray
    ``temperature: true`` does not become ``1.0``.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _thinking_effort_field(value: Any) -> str | None:
    """Return a known thinking-effort level, or ``None`` when absent/unknown.

    Defensively normalized (D3): OpenCode's ``reasoningEffort`` is matched
    case-insensitively and trimmed against vBot's effort ladder
    (:data:`ALLOWED_THINKING_EFFORTS`). A missing, empty, or foreign value yields
    ``None`` — the agent tier simply declares nothing and the resolver chain falls
    through to the project/global default — exactly as :func:`_temperature_field`
    drops an invalid temperature rather than raising. ``""`` is intentionally
    *not* propagated from the scan: an OpenCode agent that omits the key declares
    nothing, so "provider default" stays a project/global decision.
    """
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if not normalized or normalized not in ALLOWED_THINKING_EFFORTS:
        return None
    return normalized


def _denied_tools(fields: dict[str, Any]) -> frozenset[str]:
    """Return the vBot tools an OpenCode agent turns off, from its front matter.

    Reads both the ``permission`` map (deny-by-key, scalar or granular) and the
    ``tools`` map (deny-by-exception ``false``), unions their mapped vBot tools, and
    ignores everything without a vBot counterpart. Granularity is collapsed to one
    bit per the max-agency rule (:func:`_is_effective_full_deny`): a tool is denied
    only on an unambiguous full deny. Any foreign/unknown shape fails open (not a
    deny) and never raises — a malformed agent file must not crash the scan.
    """
    denied: set[str] = set()
    _collect_permission_denials(fields.get("permission"), denied)
    _collect_tools_denials(fields.get("tools"), denied)
    return frozenset(denied)


def _collect_permission_denials(permission: Any, denied: set[str]) -> None:
    if not isinstance(permission, dict):
        return
    for key, value in permission.items():
        mapped = _PERMISSION_DENY_MAP.get(key) if isinstance(key, str) else None
        if mapped is not None and _is_effective_full_deny(value):
            denied.update(mapped)


def _collect_tools_denials(tools: Any, denied: set[str]) -> None:
    if not isinstance(tools, dict):
        return
    for name, value in tools.items():
        mapped = _TOOLS_DENY_MAP.get(name) if isinstance(name, str) else None
        # Deny-by-exception: only an explicit boolean ``false`` turns a tool off.
        # ``true``, absence, or any non-bool value leaves it on (fail open). The
        # ``is False`` identity check keeps a stray ``0`` from counting as a deny.
        if mapped is not None and value is False:
            denied.update(mapped)


def _is_effective_full_deny(value: Any) -> bool:
    """Return whether a permission value is an *effective* full deny (collapse rule).

    The scalar ``"deny"`` denies. A granular map denies only when it has at least
    one entry and **every** entry is ``"deny"`` (no ``allow``/``ask`` anywhere) — a
    single non-deny entry, an empty map, or any non-string value makes it not a deny
    (fail open, max-agency). Comparison is case-insensitive and trimmed.
    """
    if isinstance(value, str):
        return value.strip().lower() == _OPENCODE_DENY
    if isinstance(value, dict):
        actions = list(value.values())
        if not actions or not all(isinstance(action, str) for action in actions):
            return False
        return all(action.strip().lower() == _OPENCODE_DENY for action in actions)
    return False
