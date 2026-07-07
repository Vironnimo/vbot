"""Claude Code agent detector: reads ``.claude/agents/`` at the project root.

Claude Code stores one subagent per Markdown file under ``.claude/agents/`` —
subdirectories are allowed, so this detector scans **recursively within that
directory only** (still confined to the known location, never a repo tree walk).
Each file carries YAML front matter (``name`` and ``description`` per the docs,
plus optional ``tools`` / ``disallowedTools`` / ``model``) and a body that is the
agent's system prompt. Mapping to the uniform :class:`ScannedAgent` profile
(max-agency, fail-open — the same philosophy as the OpenCode detector):

- ``agent_id`` = the front-matter ``name`` **slugified** when it is a non-empty
  string (the canonical Claude Code identifier), else the filename stem; a name
  that cannot be slugified becomes a parse failure for the report.
- ``description`` from the front matter; missing → ``""`` (lenient, no reject).
- ``model`` is **always dropped** (``""``): Claude's vocabulary (aliases like
  ``sonnet``, bare Anthropic ids, ``inherit``) is never vBot's
  ``<provider>/<model-id>`` form, and dropping it avoids a BAD_MODEL finding on
  every Claude agent. The vBot-native way to pin a model is the per-agent
  override layer (``/model``). Claude agents carry no temperature/reasoning
  field either → ``temperature=None``, ``thinking_effort=None``.
- ``body`` = the file body after the front matter, **verbatim** (opaque text).
- ``denied_tools`` = the mapped vBot tools of every ``disallowedTools`` entry,
  plus — when a ``tools`` allow-list is present — the mapped vBot tools of every
  *mappable* Claude tool NOT named in it (omitted ``tools`` = inherit all).
  vBot tools with no Claude counterpart (e.g. ``status``) are never denied.
  Unknown Claude tool names are ignored; malformed shapes fail open and never
  crash the scan. ``tools``/``disallowedTools`` accept both a comma-separated
  string and a YAML list. Front-matter fields beyond these (``memory``,
  ``background``, …) are ignored.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from core.projects.paths import slugify_agent_id
from core.projects.scanners.base import (
    DetectedFile,
    ScannedAgent,
    parse_front_matter,
    split_front_matter,
    string_field,
)
from core.utils.logging import get_logger

_LOGGER = get_logger("projects")

# Claude Code's known, fixed location relative to the project root. Scanned
# recursively within this directory only (Claude Code allows subfolders), sorted
# stably by relative POSIX path — never a repo tree walk.
CLAUDE_AGENTS_SUBPATH = (".claude", "agents")
CLAUDE_FORMAT_KEY = "claude"
_AGENT_FILE_GLOB = "*.md"

# Claude tool name (normalized: trimmed, lowercased) → the vBot tools it maps to.
# ``Bash`` covers both bash and process (shell access grants both in vBot); every
# other mapped tool is 1:1. Claude tools without a vBot counterpart and vBot tools
# without a Claude counterpart (e.g. ``status``) are simply absent — an allow-list
# inversion can therefore never deny an unmappable vBot tool.
_CLAUDE_TOOL_MAP: dict[str, frozenset[str]] = {
    "read": frozenset({"read"}),
    "write": frozenset({"write"}),
    "edit": frozenset({"edit"}),
    "glob": frozenset({"glob"}),
    "grep": frozenset({"grep"}),
    "bash": frozenset({"bash", "process"}),
    "webfetch": frozenset({"web_fetch"}),
    "websearch": frozenset({"web_search"}),
    "task": frozenset({"subagent"}),
    "skill": frozenset({"skill"}),
}


class ClaudeDetector:
    """Detector for the Claude Code subagent format. One instance per scan is fine."""

    @property
    def format_key(self) -> str:
        """The stable format key used for precedence and reporting."""
        return CLAUDE_FORMAT_KEY

    def detect(self, project_root: Path) -> list[DetectedFile]:
        """Read ``.claude/agents/**/*.md`` under ``project_root``.

        Recursive within the known location only (Claude Code allows agent
        subfolders); results are **sorted stably by relative POSIX path** so the
        order is deterministic across hosts. A missing location yields an empty
        list. Each file becomes either a parsed :class:`ScannedAgent` or a parse
        failure (unslugifiable name / unreadable file) for the report.
        """
        agents_dir = project_root.joinpath(*CLAUDE_AGENTS_SUBPATH)
        if not agents_dir.is_dir():
            return []

        agent_files = sorted(
            (path for path in agents_dir.rglob(_AGENT_FILE_GLOB) if path.is_file()),
            key=lambda path: path.relative_to(agents_dir).as_posix(),
        )
        return [self._read_agent_file(path) for path in agent_files]

    def _read_agent_file(self, path: Path) -> DetectedFile:
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as error:
            _LOGGER.warning("Could not read Claude agent file %s: %s", path, error)
            return DetectedFile(
                source_path=path,
                raw_name=path.stem,
                error_reason=f"could not read agent file: {error}",
            )

        front_matter, body = split_front_matter(content)
        fields = parse_front_matter(front_matter, path)

        # The front-matter ``name`` is the canonical Claude Code identifier; the
        # filename stem is only the fallback when it is absent/empty/non-string.
        raw_name = string_field(fields.get("name")) or path.stem
        try:
            agent_id = slugify_agent_id(raw_name)
        except ValueError as error:
            return DetectedFile(source_path=path, raw_name=raw_name, error_reason=str(error))

        agent = ScannedAgent(
            agent_id=agent_id,
            display_name=raw_name,
            description=string_field(fields.get("description")),
            # Claude model values (aliases, Anthropic ids, ``inherit``) are never
            # vBot's ``<provider>/<model-id>`` form — always dropped (see module
            # docstring); the per-agent override layer is the vBot way to pin one.
            model="",
            temperature=None,
            thinking_effort=None,
            body=body,
            source_format=CLAUDE_FORMAT_KEY,
            source_path=path,
            denied_tools=_denied_tools(fields),
        )
        return DetectedFile(source_path=path, raw_name=raw_name, agent=agent)


def _denied_tools(fields: dict[str, Any]) -> frozenset[str]:
    """Return the vBot tools a Claude agent turns off, from its front matter.

    ``disallowedTools`` is a deny-list: each mappable entry denies its vBot tools.
    ``tools`` is an allow-list: when present, every *mappable* Claude tool NOT
    named in it is denied (omitted = inherit all → nothing denied). Both accept a
    comma-separated string or a YAML list; unknown names are ignored and malformed
    shapes fail open — a crafted agent file must never crash the scan.
    """
    denied: set[str] = set()

    disallowed = _tool_name_list(fields.get("disallowedTools"))
    if disallowed is not None:
        for name in disallowed:
            mapped = _CLAUDE_TOOL_MAP.get(name)
            if mapped is not None:
                denied.update(mapped)

    allowed = _tool_name_list(fields.get("tools"))
    if allowed is not None:
        allowed_names = set(allowed)
        for name, mapped in _CLAUDE_TOOL_MAP.items():
            if name not in allowed_names:
                denied.update(mapped)

    return frozenset(denied)


def _tool_name_list(value: Any) -> list[str] | None:
    """Parse a Claude tool-name field into normalized names, or ``None`` when absent.

    Accepts a comma-separated string or a YAML list (lenient — Claude Code's docs
    show the string form, hand-written files often use a list). Names are trimmed
    and lowercased; empty entries and non-string list items are dropped. An absent
    field, an empty/whitespace-only string, or any other shape returns ``None``
    ("nothing declared", fail open) — only an explicit YAML list may be empty,
    which for ``tools`` means "no tools allowed".
    """
    if isinstance(value, str):
        names = [item.strip().lower() for item in value.split(",")]
        cleaned = [name for name in names if name]
        return cleaned if cleaned else None
    if isinstance(value, list):
        return [item.strip().lower() for item in value if isinstance(item, str) and item.strip()]
    return None
