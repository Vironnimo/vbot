"""Learning-system texts under evaluation: current texts, text packs and the brief seam.

A text pack is a directory holding exactly the Agent-facing texts that steer
Memory and Skill learning, so two arms of an A/B evaluation differ only in them:

- the ``memory:guidance``, ``core:skills`` and ``core:skill_maintenance`` System
  Prompt blocks (``blocks/*.md``),
- the ``memory``, ``skill`` and ``skill_manage`` Tool descriptions
  (``tools/<name>/description.md``) and their parameter descriptions
  (``tools/<name>/parameters.json``, JSON-pointer path to text),
- every prompt fragment the Reflection review briefs, the ``/learn`` brief and
  the Librarian brief are assembled from (``fragments/<name>``, the names in
  ``core.prompts.briefs.BRIEF_FRAGMENT_NAMES``).

``export-pack`` writes the current texts of this checkout; ``--text-pack DIR``
replaces exactly these texts in a run and nothing else. Production assembles
the briefs from the run's fragments, so a pack changes brief wording, never
brief composition.
"""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.provider_probe.common import PROJECT_ROOT

TEXT_PACK_FORMAT = 2

# Pack file stem -> System Prompt block id.
BLOCK_TEXTS: dict[str, str] = {
    "memory_guidance": "memory:guidance",
    "skills": "core:skills",
    "skill_maintenance": "core:skill_maintenance",
}
TOOL_TEXTS: tuple[str, ...] = ("memory", "skill", "skill_manage")


def brief_fragment_names() -> tuple[str, ...]:
    """Return every prompt fragment production assembles the learning briefs from."""
    from core.prompts.briefs import BRIEF_FRAGMENT_NAMES

    return tuple(sorted(BRIEF_FRAGMENT_NAMES))


@dataclass(frozen=True)
class ToolTexts:
    """One Tool's description and its parameter descriptions by JSON-pointer path."""

    description: str
    parameters: dict[str, str]


@dataclass(frozen=True)
class LearningTexts:
    """The effective learning texts of one run."""

    blocks: dict[str, str]
    tools: dict[str, ToolTexts]
    fragments: dict[str, str]

    def items(self) -> Iterator[tuple[str, str]]:
        """Yield ``(text id, text)`` for every text, in a stable order."""
        for block_id in BLOCK_TEXTS.values():
            yield f"block:{block_id}", self.blocks[block_id]
        for name in TOOL_TEXTS:
            texts = self.tools[name]
            yield f"tool:{name}:description", texts.description
            for pointer in sorted(texts.parameters):
                yield f"tool:{name}:{pointer}", texts.parameters[pointer]
        for name in sorted(self.fragments):
            yield f"fragment:{name}", self.fragments[name]

    def digests(self) -> dict[str, str]:
        """Return a short SHA-256 per text id, so reports show which texts differ."""
        return {text_id: _digest(text) for text_id, text in self.items()}


@dataclass(frozen=True)
class TextPack:
    """A loaded text pack, before it is merged with a checkout's current texts."""

    path: Path
    manifest: dict[str, Any]
    blocks: dict[str, str]
    tools: dict[str, ToolTexts]
    fragments: dict[str, str]


@dataclass(frozen=True)
class AppliedTexts:
    """The texts a run uses, which of them a pack changed, and pack warnings."""

    texts: LearningTexts
    changed: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _description_paths(node: Any, pointer: str = "") -> Iterator[tuple[str, str]]:
    """Yield every string ``description`` inside a JSON schema with its pointer path."""
    if isinstance(node, dict):
        for key, value in node.items():
            path = f"{pointer}/{str(key).replace('~', '~0').replace('/', '~1')}"
            if key == "description" and isinstance(value, str):
                yield path, value
            else:
                yield from _description_paths(value, path)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _description_paths(value, f"{pointer}/{index}")


def _set_pointer(node: Any, pointer: str, value: str) -> None:
    parts = [part.replace("~1", "/").replace("~0", "~") for part in pointer.split("/")[1:]]
    for part in parts[:-1]:
        node = node[int(part)] if isinstance(node, list) else node[part]
    node[parts[-1]] = value


def current_texts(
    definitions: Sequence[Mapping[str, Any]],
    blocks: Sequence[Mapping[str, Any]],
    read_fragment: Any,
) -> LearningTexts:
    """Collect the texts a Runtime currently uses.

    ``definitions`` are the Agent's provider Tool definitions, ``blocks`` the
    prompt block listing (``SystemPromptManager.list_blocks``) and
    ``read_fragment`` the storage reader production assembles briefs from
    (``StorageManager.read_prompt_fragment``).
    """
    by_id = {str(block["id"]): block for block in blocks}
    block_texts: dict[str, str] = {}
    for block_id in BLOCK_TEXTS.values():
        text = by_id.get(block_id, {}).get("text")
        if not isinstance(text, str):
            raise RuntimeError(f"Prompt block {block_id} has no editable text")
        block_texts[block_id] = text
    by_name = {str(definition["name"]): definition for definition in definitions}
    tools: dict[str, ToolTexts] = {}
    for name in TOOL_TEXTS:
        definition = by_name.get(name)
        if definition is None:
            raise RuntimeError(f"The evaluation Agent is not offered the {name} Tool")
        tools[name] = ToolTexts(
            description=str(definition.get("description") or ""),
            parameters=dict(_description_paths(definition.get("parameters") or {})),
        )
    fragments = {name: str(read_fragment(name)) for name in brief_fragment_names()}
    return LearningTexts(blocks=block_texts, tools=tools, fragments=fragments)


class _FragmentTexts:
    """Serve brief fragments from a run's texts, as Storage serves fragment files."""

    def __init__(self, fragments: Mapping[str, str]) -> None:
        self._fragments = fragments

    def read_prompt_fragment(self, fragment_name: str) -> str:
        try:
            return self._fragments[fragment_name]
        except KeyError:
            raise KeyError(f"No text for brief fragment {fragment_name}") from None


def brief_text(
    scope: str,
    case: Mapping[str, Any],
    texts: LearningTexts,
    *,
    candidates: Sequence[Any] = (),
) -> str:
    """Return the instruction the Model receives for one case in one scope.

    This is the single seam between the harness and production brief assembly:
    it calls production ``reflection_brief`` (a review without user focus, as
    the cadence trigger starts it), ``learn_brief`` with the case's request, or
    ``librarian_brief`` for the evaluated Agent's Skills listing ``candidates``
    (production ``LibrarianCandidate``s), reading ``texts.fragments`` in place of
    Storage.
    """
    from core.prompts.briefs import learn_brief, librarian_brief, reflection_brief

    fragments = _FragmentTexts(texts.fragments)
    if scope == "learn":
        return learn_brief(fragments, case.get("learn_request"))
    if scope == "librarian":
        from scripts.provider_probe.learning_fixture import EVAL_AGENT_ID

        return librarian_brief(
            fragments, candidates, agent_id=EVAL_AGENT_ID, agent_name=EVAL_AGENT_ID
        )
    if scope not in ("memory", "skill", "combined"):
        raise ValueError(f"Unknown evaluation scope: {scope}")
    return reflection_brief(fragments, scope)  # type: ignore[arg-type]


def apply_tool_texts(
    definitions: Sequence[Mapping[str, Any]], texts: LearningTexts
) -> list[dict[str, Any]]:
    """Return copies of ``definitions`` carrying ``texts``' Tool descriptions."""
    applied: list[dict[str, Any]] = []
    for definition in definitions:
        copied = copy.deepcopy(dict(definition))
        tool_texts = texts.tools.get(str(copied.get("name")))
        if tool_texts is not None:
            copied["description"] = tool_texts.description
            parameters = copied.get("parameters") or {}
            current = dict(_description_paths(parameters))
            for pointer, text in tool_texts.parameters.items():
                if pointer in current:
                    _set_pointer(parameters, pointer, text)
        applied.append(copied)
    return applied


def _pack_files() -> Iterator[tuple[str, str]]:
    """Yield ``(relative path, kind)`` of every text file a pack holds."""
    for stem in BLOCK_TEXTS:
        yield f"blocks/{stem}.md", "block"
    for name in TOOL_TEXTS:
        yield f"tools/{name}/description.md", "tool"
        yield f"tools/{name}/parameters.json", "parameters"


def checkout_commit() -> str | None:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except OSError, subprocess.SubprocessError:
        return None
    return completed.stdout.strip() or None


def write_text_pack(texts: LearningTexts, directory: Path) -> Path:
    """Write ``texts`` as a text pack into an empty or new ``directory``."""
    if directory.exists() and any(directory.iterdir()):
        raise ValueError(f"Text pack directory is not empty: {directory}")
    for stem, block_id in BLOCK_TEXTS.items():
        _write(directory / "blocks" / f"{stem}.md", texts.blocks[block_id])
    for name in TOOL_TEXTS:
        tool_texts = texts.tools[name]
        _write(directory / "tools" / name / "description.md", tool_texts.description)
        _write(
            directory / "tools" / name / "parameters.json",
            json.dumps(tool_texts.parameters, indent=2, ensure_ascii=False) + "\n",
        )
    for name, text in texts.fragments.items():
        _write(directory / "fragments" / name, text)
    manifest = {
        "format": TEXT_PACK_FORMAT,
        "exported_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "exported_from_commit": checkout_commit(),
        "notes": (
            "Edit any file to build an A/B arm. fragments/ holds every prompt fragment "
            "the review and /learn briefs are assembled from; production joins them per "
            "scope (core/prompts/briefs.py) and appends each case's /learn request. "
            "Keep {generated:...} markers in the block texts."
        ),
        "texts": texts.digests(),
    }
    _write(directory / "manifest.json", json.dumps(manifest, indent=2) + "\n")
    return directory


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8", newline="\n")


def load_text_pack(directory: Path) -> TextPack:
    """Read a text pack; every block and Tool text file must be present.

    Fragments are read as the pack holds them; merging decides which of them a
    brief reads, since the fragment set follows the checkout.
    """
    manifest_path = directory / "manifest.json"
    manifest = (
        json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
    )
    if manifest.get("format", TEXT_PACK_FORMAT) != TEXT_PACK_FORMAT:
        raise ValueError(
            f"Unsupported text pack format {manifest.get('format')!r}; "
            f"export a format {TEXT_PACK_FORMAT} pack with export-pack"
        )
    missing = [
        relative for relative, _kind in _pack_files() if not (directory / relative).is_file()
    ]
    if not (directory / "fragments").is_dir():
        missing.append("fragments/")
    if missing:
        raise ValueError(f"Text pack {directory} lacks: {', '.join(missing)}")

    def read(relative: str) -> str:
        return (directory / relative).read_text(encoding="utf-8")

    blocks = {block_id: read(f"blocks/{stem}.md").strip() for stem, block_id in BLOCK_TEXTS.items()}
    tools: dict[str, ToolTexts] = {}
    for name in TOOL_TEXTS:
        parameters = json.loads(read(f"tools/{name}/parameters.json"))
        if not isinstance(parameters, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in parameters.items()
        ):
            raise ValueError(f"tools/{name}/parameters.json must map paths to text")
        tools[name] = ToolTexts(
            description=read(f"tools/{name}/description.md").strip(), parameters=parameters
        )
    fragments = {
        path.name: path.read_text(encoding="utf-8").strip()
        for path in sorted((directory / "fragments").iterdir())
        if path.is_file()
    }
    return TextPack(
        path=directory, manifest=manifest, blocks=blocks, tools=tools, fragments=fragments
    )


def merge_text_pack(current: LearningTexts, pack: TextPack | None) -> AppliedTexts:
    """Replace ``current`` texts by a pack's, reporting what changed and what did not fit.

    A pack text equal to the current one apart from surrounding whitespace keeps
    the current text, so a pack exported from this checkout reproduces it
    exactly. A parameter path the current schema lacks is ignored with a
    warning; a current path the pack lacks keeps its production text. Fragments
    follow the same rule: a pack fragment no brief reads is ignored, and a
    fragment the pack lacks keeps its production text, each with a warning.
    """
    if pack is None:
        return AppliedTexts(current)
    warnings: list[str] = []

    def pick(current_text: str, pack_text: str) -> str:
        return current_text if current_text.strip() == pack_text.strip() else pack_text

    blocks: dict[str, str] = {}
    for block_id, text in current.blocks.items():
        blocks[block_id] = pick(text, pack.blocks[block_id])
        for marker in _markers(text):
            if marker not in blocks[block_id]:
                warnings.append(f"block:{block_id} lacks {marker}; its generated content vanishes")
    tools: dict[str, ToolTexts] = {}
    for name, tool_texts in current.tools.items():
        packed = pack.tools[name]
        parameters = dict(tool_texts.parameters)
        for pointer, text in packed.parameters.items():
            if pointer in parameters:
                parameters[pointer] = pick(parameters[pointer], text)
            else:
                warnings.append(f"tool:{name}:{pointer} is not in the current schema; ignored")
        for pointer in tool_texts.parameters.keys() - packed.parameters.keys():
            warnings.append(f"tool:{name}:{pointer} is not in the pack; production text kept")
        tools[name] = ToolTexts(
            description=pick(tool_texts.description, packed.description), parameters=parameters
        )
    fragments: dict[str, str] = {}
    for name, text in current.fragments.items():
        if name in pack.fragments:
            fragments[name] = pick(text, pack.fragments[name])
        else:
            fragments[name] = text
            warnings.append(f"fragment:{name} is not in the pack; production text kept")
    for name in sorted(pack.fragments.keys() - current.fragments.keys()):
        warnings.append(f"fragment:{name} is read by no brief; ignored")
    merged = LearningTexts(blocks=blocks, tools=tools, fragments=fragments)
    before = current.digests()
    changed = [text_id for text_id, digest in merged.digests().items() if before[text_id] != digest]
    return AppliedTexts(merged, changed=changed, warnings=warnings)


def _markers(text: str) -> list[str]:
    markers: list[str] = []
    start = text.find("{generated:")
    while start != -1:
        end = text.find("}", start)
        if end == -1:
            break
        markers.append(text[start : end + 1])
        start = text.find("{generated:", end)
    return markers
