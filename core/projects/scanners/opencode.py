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
- ``model`` taken **1:1** (``<provider>/<model-id>``), never rewritten; may be
  empty. Model existence/configuration is judged later by the resolver, not here.
- ``body`` = the file body after the front matter, **verbatim** (opaque text;
  ``{...}`` is not expanded here).
- ``tools`` and ``skills`` = ``["*"]`` for every agent.

Everything else in the front matter stays out in v1.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from core.projects.paths import slugify_agent_id
from core.projects.scanners.base import ALLOW_ALL, DetectedFile, ScannedAgent
from core.utils.logging import get_logger

_LOGGER = get_logger("projects")

# OpenCode's known, fixed location relative to the project root. Scanned
# non-recursively — only direct ``*.md`` files here, never subdirectories.
OPENCODE_AGENTS_SUBPATH = (".opencode", "agents")
OPENCODE_FORMAT_KEY = "opencode"
_AGENT_FILE_GLOB = "*.md"
_FRONT_MATTER_DELIMITER = "---"


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
            body=body,
            source_format=OPENCODE_FORMAT_KEY,
            source_path=path,
            tools=(ALLOW_ALL,),
            skills=(ALLOW_ALL,),
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
