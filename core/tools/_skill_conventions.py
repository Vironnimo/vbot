"""Authoring hints for a Skill that ``skill_manage`` just wrote.

The conventions are those of the bundled ``skill-writing`` Skill. A hint
never blocks or changes a write; it tells the Agent which convention its text
breaks and how to fix it. Only cheap checks with few false alarms run: across
the Agent-written Skills they were calibrated on, every flagged date was an
incident note. They are not loader validation (``skill_validator``), which
reports whether any package from any source loads, and would flag imported
Skills that follow other conventions.

A write that replaced a whole file is judged on all of that file; a patch only
on what it introduced, so changing one line of an older Skill does not ask the
Agent to rework the rest.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from core.skills.skill_validator import (
    MAX_SKILL_DESCRIPTION_LENGTH,
    SKILL_DESCRIPTION_ADVISED_LENGTH,
    SKILL_MD_ADVISED_LENGTH,
    parse_skill_front_matter,
    split_skill_document,
)
from core.skills.skills import SKILL_FILENAME as SKILL_MD
from core.utils.text_pages import TEXT_PAGE_MAX_BYTES, TEXT_PAGE_MAX_LINES

# At most this many hints per write; the next write shows the rest.
HINT_LIMIT = 3

SKILL_MANAGE_HINT_NO_SITUATION = (
    'The description does not say when to load the Skill. Add "Use when" and the situations '
    "that call for it, in the words of a user's request."
)
SKILL_MANAGE_HINT_LONG_DESCRIPTION = (
    "The description has {length} characters; every request lists it. Shorten it to under "
    f'{SKILL_DESCRIPTION_ADVISED_LENGTH}: what the Skill does, then "Use when" and the '
    "situations, with details moved into the instructions."
)
SKILL_MANAGE_HINT_UNNAMED_FILE = (
    "No other file of the Skill names {path}, so Agents following SKILL.md never {verb} it. "
    "Add a sentence to SKILL.md that says when to {verb} it."
)
SKILL_MANAGE_HINT_LONG_SKILL_MD = (
    f"SKILL.md has {{length}} characters. Keep it under {SKILL_MD_ADVISED_LENGTH:,}: move "
    "material that only some tasks need into a file under references/ and say in SKILL.md "
    "when to read it."
)
SKILL_MANAGE_HINT_HISTORY = (
    "{path} contains dates or change identifiers, such as {example}. Delete them and state "
    "each rule with its reason, not when or where it was learned."
)
SKILL_MANAGE_HINT_EMPHASIS = (
    "{path} stresses rules in capitals, such as {example}. Write them in normal case, with "
    "must or never only for real requirements."
)

# A description names a situation with one of these words (English and German).
_SITUATION = re.compile(
    r"\b(?:when|whenever|if|use (?:it |this (?:skill )?)?for|wenn|sobald|falls)\b", re.IGNORECASE
)
_CODE = re.compile(r"```.*?```|~~~.*?~~~|`[^`\n]*`", re.DOTALL)
# ISO dates, issue and ticket numbers, and commit hashes: when and where a rule was
# learned, not the rule.
_HISTORY = re.compile(
    r"\b(?:19|20)\d\d-[01]\d-[0-3]\d\b"
    r"|\b(?:PR|pull request|issue|ticket|bug)\s*#\d+\b"
    r"|\bcommit [0-9a-f]{7,40}\b",
    re.IGNORECASE,
)
_EMPHASIS = re.compile(
    r"\b(?:IMPORTANT|CRITICAL|MUST|NEVER|ALWAYS|MANDATORY|WICHTIG|NIEMALS|IMMER)\b"
)
# Support files that SKILL.md points to; nested and helper files often have no
# pointer of their own.
_POINTED_DIRECTORIES = {"references": "read", "scripts": "run"}


@dataclass(frozen=True)
class _Finding:
    # Identifies the finding across the states before and after a write.
    key: tuple[str, str]
    text: str


def skill_manage_hints(
    files: Mapping[str, str | None], path: str, before: str | None, *, whole: bool
) -> list[str]:
    """Return the hints for a write of ``path``.

    ``files`` maps each package-relative path of the Skill after the write to
    its text with LF line endings (``None`` when it is not read as text).
    ``before`` is the text of ``path`` before the write (``None`` when it was
    absent); ``whole`` says the write replaced the whole file.
    """
    after = _findings(files)
    if whole:
        reported = [finding for finding in after if _concerns(finding, path)]
    else:
        earlier = dict(files)
        if before is None:
            earlier.pop(path, None)
        else:
            earlier[path] = before
        known = {finding.key for finding in _findings(earlier)}
        reported = [finding for finding in after if finding.key not in known]
    return [finding.text for finding in reported[:HINT_LIMIT]]


def _concerns(finding: _Finding, path: str) -> bool:
    """Whether the text of ``path`` decides ``finding``.

    SKILL.md decides on the description, its own size and prose, and on which
    support files it names.
    """
    kind, subject = finding.key
    return subject == path or (path == SKILL_MD and kind == "unnamed_file")


def _findings(files: Mapping[str, str | None]) -> list[_Finding]:
    """Every convention the Skill breaks, the most consequential first."""
    findings: list[_Finding] = []
    document = files.get(SKILL_MD)
    if document is not None:
        findings.extend(_description_findings(document))
    findings.extend(_unnamed_file_findings(files))
    if (
        document is not None
        and _fits_one_page(document)
        and len(document) > (SKILL_MD_ADVISED_LENGTH)
    ):
        findings.append(
            _Finding(
                ("long_skill_md", SKILL_MD),
                SKILL_MANAGE_HINT_LONG_SKILL_MD.format(length=f"{len(document):,}"),
            )
        )
    for path, text in files.items():
        if text is not None and (path == SKILL_MD or _is_reference_text(path)):
            findings.extend(_prose_findings(path, text))
    return findings


def _description_findings(document: str) -> list[_Finding]:
    front_matter, _, _ = split_skill_document(document)
    fields, _ = parse_skill_front_matter(front_matter)
    value = fields.get("description") if isinstance(fields, dict) else None
    description = " ".join(str(value).split()) if value is not None else ""
    if not description:
        return []
    findings: list[_Finding] = []
    if not _SITUATION.search(description):
        findings.append(_Finding(("no_situation", SKILL_MD), SKILL_MANAGE_HINT_NO_SITUATION))
    # Over the specification's limit the loader warns already.
    if SKILL_DESCRIPTION_ADVISED_LENGTH < len(description) <= MAX_SKILL_DESCRIPTION_LENGTH:
        findings.append(
            _Finding(
                ("long_description", SKILL_MD),
                SKILL_MANAGE_HINT_LONG_DESCRIPTION.format(length=len(description)),
            )
        )
    return findings


def _unnamed_file_findings(files: Mapping[str, str | None]) -> list[_Finding]:
    findings: list[_Finding] = []
    for path in sorted(files):
        directory, _, base = path.partition("/")
        verb = _POINTED_DIRECTORIES.get(directory)
        if verb is None or "/" in base or base.startswith(("_", ".", "test_")):
            continue
        stem = base.rsplit(".", 1)[0]
        named = re.compile(rf"(?<![\w.-]){re.escape(base)}(?![\w-])")
        imported = re.compile(rf"\b(?:import|from)\s+{re.escape(stem)}\b")
        if any(
            text is not None
            and (named.search(text) or (directory == "scripts" and imported.search(text)))
            for other, text in files.items()
            if other != path
        ):
            continue
        findings.append(
            _Finding(
                ("unnamed_file", path), SKILL_MANAGE_HINT_UNNAMED_FILE.format(path=path, verb=verb)
            )
        )
    return findings


def _prose_findings(path: str, text: str) -> list[_Finding]:
    prose = _CODE.sub("", text)
    findings: list[_Finding] = []
    history = _HISTORY.search(prose)
    if history is not None:
        findings.append(
            _Finding(
                ("history", path),
                SKILL_MANAGE_HINT_HISTORY.format(path=path, example=history.group(0)),
            )
        )
    emphasis = _EMPHASIS.search(prose)
    if emphasis is not None:
        findings.append(
            _Finding(
                ("emphasis", path),
                SKILL_MANAGE_HINT_EMPHASIS.format(path=path, example=emphasis.group(0)),
            )
        )
    return findings


def _is_reference_text(path: str) -> bool:
    return path.startswith("references/") and path.endswith(".md")


def _fits_one_page(document: str) -> bool:
    """Whether the instructions fit one page; beyond it the loader warns already."""
    instructions = split_skill_document(document)[1].strip()
    return (
        len(instructions.encode("utf-8")) <= TEXT_PAGE_MAX_BYTES
        and len(instructions.splitlines()) <= TEXT_PAGE_MAX_LINES
    )
