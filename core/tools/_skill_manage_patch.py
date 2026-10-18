"""Skill package edit application and tolerant patch matching."""

from __future__ import annotations

import re
from pathlib import Path

from core.skills.authoring import (
    SkillAuthoringError,
    SkillAuthoringService,
    SkillReference,
    SkillWriter,
    SkillWriteResult,
)
from core.skills.skills import (
    RESOURCE_DIRECTORIES,
    SKILL_FILENAME,
    find_skill_package_dir,
)
from core.tools._skill_manage_calls import _Call, _read_hint, _RefusalError
from core.tools.copy_match import copy_warnings, replace_copied
from core.tools.fuzzy_match import (
    AmbiguousFuzzyMatch,
    FuzzyReplacement,
    find_closest_candidates,
    preserve_typography,
    replace_fuzzy,
)

# Results of a delete, which moves the Skill into the archive of its home.
SKILL_MANAGE_DELETED = (
    "Deleted Skill '{name}'. Its files are kept in the archive, where the user can restore it."
)
SKILL_MANAGE_ABSORBED = (
    "Deleted Skill '{name}'; its instructions now live in Skill '{target}'. Its files are "
    "kept in the archive, where the user can restore it."
)
_PREVIEW_LIMIT = 400

# Typographic glyphs a Model may type where a file has the plain form. Mirrors the
# patch folds of ``fuzzy_match`` so a folded match also folds its replacement.
_TYPOGRAPHY = {
    "“": '"',
    "”": '"',
    "‘": "'",
    "’": "'",
    "–": "-",
    "—": "--",
    "−": "-",
    "…": "...",
    " ": " ",
}
_JSON_ESCAPE = re.compile(r'\\(["\\/nrt])')
_JSON_ESCAPES = {'"': '"', "\\": "\\", "/": "/", "n": "\n", "r": "\r", "t": "\t"}


def _apply(
    authoring: SkillAuthoringService,
    target_root: Path,
    call: _Call,
    writer: SkillWriter,
    followed: tuple[SkillReference, ...] = (),
) -> tuple[SkillWriteResult, str]:
    name = call.name
    if call.action == "create":
        result = authoring.create(target_root, name, call.content or "", writer=writer)
        return result, f"Created Skill '{name}'."
    if call.action == "edit":
        result = authoring.edit(target_root, name, call.content or "", writer=writer)
        return result, f"Replaced SKILL.md of Skill '{name}'."
    if call.action == "patch":
        return _patch(authoring, target_root, call, writer)
    file_path = call.file_path or ""
    if call.action == "write_file":
        result = authoring.write_file(
            target_root, name, file_path, call.content or "", writer=writer
        )
        return result, f"Wrote {file_path} of Skill '{name}'."
    if call.action == "remove_file":
        result = authoring.remove_file(target_root, name, file_path, writer=writer)
        return result, f"Removed {file_path} from Skill '{name}'."
    if call.absorbed_into is not None:
        result = authoring.delete(
            target_root, name, writer=writer, absorbed_into=call.absorbed_into, followed=followed
        )
        return result, SKILL_MANAGE_ABSORBED.format(name=name, target=call.absorbed_into)
    result = authoring.delete(target_root, name, writer=writer)
    return result, SKILL_MANAGE_DELETED.format(name=name)


def _patch(
    authoring: SkillAuthoringService, target_root: Path, call: _Call, writer: SkillWriter
) -> tuple[SkillWriteResult, str]:
    """Patch the named file, else SKILL.md or the one support file holding old_string.

    Without file_path, a patch whose old_string is not in SKILL.md but matches
    once in exactly one support file changes that file, with a note: the text
    names its place. A named file_path is never exchanged for another file;
    there, and when the text is in several places, the miss names the file.
    """
    file_path = call.file_path or SKILL_FILENAME
    try:
        return _patch_file(authoring, target_root, call, writer, file_path)
    except _RefusalError as refusal:
        if refusal.code != "text_not_found" or file_path != SKILL_FILENAME:
            raise
        holders = _support_files_holding(authoring, target_root, call)
        if len(holders) != 1:
            raise
        holder, once = holders[0]
        if call.file_path is None and once:
            patched = _patch_file(authoring, target_root, call, writer, holder)
            call.notes.append(
                f"old_string was not in SKILL.md but in {holder}, the only file of the Skill "
                "that holds it, so that file was patched."
            )
            return patched
        hint = f'The text is in {holder}; to patch it there, add "file_path": "{holder}".'
        raise _RefusalError(refusal.code, f"{refusal.message}\n{hint}") from None


def _patch_file(
    authoring: SkillAuthoringService,
    target_root: Path,
    call: _Call,
    writer: SkillWriter,
    file_path: str,
) -> tuple[SkillWriteResult, str]:
    found: list[FuzzyReplacement] = []
    unchanged: list[bool] = []

    def edit(current: str) -> str:
        replacement, notes = _replace(current, call, file_path)
        found.append(replacement)
        unchanged.append(replacement.new_content == current)
        call.notes.extend(notes)
        return replacement.new_content

    result = authoring.rewrite(target_root, call.name, file_path, edit, writer=writer)
    replacement = found[0]
    where = f"{file_path} of Skill '{call.name}'"
    if unchanged[0]:
        return result, (
            f"{where} already reads as new_string at line {replacement.first_changed_line}; "
            "nothing changed."
        )
    if replacement.replacements > 1:
        return result, f"Replaced {replacement.replacements} occurrences in {where}."
    return result, f"Patched {where} at line {replacement.first_changed_line}."


def _replace(current: str, call: _Call, file_path: str) -> tuple[FuzzyReplacement, list[str]]:
    old = _lf(call.old_string or "")
    new = _lf(call.new_string or "")
    notes: list[str] = []
    copied = False
    found = _find(current, old, new, call.replace_all)
    if found is None:
        decoded_old, decoded_new = _json_unescaped(old), _json_unescaped(new)
        if decoded_old != old:
            decoded = _find(current, decoded_old, decoded_new, call.replace_all)
            if decoded is not None:
                old, new, found = decoded_old, decoded_new, decoded
                notes.append(
                    "old_string and new_string arrived with an extra level of JSON escaping "
                    '(such as \\n or \\"); they were applied unescaped.'
                )
    if found is None and not call.replace_all:
        # Old text copied with errors; not when new text shows the edit already made.
        present = new.strip() and new != old and _find(current, new, new, True) is not None
        found = None if present else replace_copied(current, old, new)
        if isinstance(found, FuzzyReplacement):
            notes.extend(copy_warnings(found))
            return found, notes
        copied = True
    if found is None:
        raise _RefusalError(
            "text_not_found", _not_found_message(current, old, call.name, file_path)
        )
    if isinstance(found, AmbiguousFuzzyMatch):
        lines = ", ".join(str(line) for line in dict.fromkeys(found.line_numbers))
        where = f"{file_path} of Skill '{call.name}' (lines {lines}); nothing changed."
        raise _RefusalError(
            "ambiguous_match",
            f"old_string does not match exactly and resembles {found.occurrences} places in "
            f"{where} Copy the current text of the one to change into old_string, with enough "
            "surrounding text to tell it apart."
            if copied
            else f"old_string matches {found.occurrences} places in {where} Include more "
            "surrounding text so it matches once, or set replace_all to true to change every "
            "occurrence.",
        )
    if found.strategy != "exact":
        adjusted = _matching_typography(current, found, old, new)
        if adjusted != new:
            refound = _find(current, old, adjusted, call.replace_all)
            if isinstance(refound, FuzzyReplacement):
                found = refound
    return found, notes


def _find(
    current: str, old: str, new: str, replace_all: bool
) -> FuzzyReplacement | AmbiguousFuzzyMatch | None:
    return replace_fuzzy(current, old, new, replace_all=replace_all, typographic=True)


def _matching_typography(current: str, found: FuzzyReplacement, old: str, new: str) -> str:
    """Write the file's own quote and dash glyphs into a normalized match's replacement."""
    start, end = found.before_spans[0]
    region = current[start:end]
    for glyph, plain in _TYPOGRAPHY.items():
        if glyph in new and glyph in old and glyph not in region:
            new = new.replace(glyph, plain)
    region_lines, old_lines, new_lines = region.split("\n"), old.split("\n"), new.split("\n")
    if found.replacements == 1 and len(region_lines) == len(old_lines) == len(new_lines):
        new = "\n".join(
            preserve_typography(actual, locator, replacement)
            for actual, locator, replacement in zip(region_lines, old_lines, new_lines, strict=True)
        )
    return new


def _not_found_message(current: str, old: str, name: str, file_path: str) -> str:
    lines = [f"old_string was not found in {file_path} of Skill '{name}'; nothing changed."]
    candidates = find_closest_candidates(current, old)[:2]
    for candidate in candidates:
        text = candidate.text[:_PREVIEW_LIMIT]
        more = " ..." if candidate.truncated or len(candidate.text) > _PREVIEW_LIMIT else ""
        lines.append(f"Closest text at line {candidate.line_number}:\n{text}{more}")
    lines.append(
        f"Copy the exact current text into old_string. {_read_hint(name, file_path)}"
        if candidates
        else _read_hint(name, file_path)
    )
    return "\n".join(lines)


def _support_files_holding(
    authoring: SkillAuthoringService, target_root: Path, call: _Call
) -> list[tuple[str, bool]]:
    """The support files old_string matches in, each with whether it matches once."""
    package = find_skill_package_dir(target_root, call.name)
    if package is None:
        return []
    old = _lf(call.old_string or "")
    holders: list[tuple[str, bool]] = []
    for directory in RESOURCE_DIRECTORIES:
        # Path.walk never enters a link, Windows junctions included.
        walk = (package / directory).walk()
        for path in sorted(base / name for base, _, names in walk for name in names):
            if not path.is_file():
                continue
            relative = path.relative_to(package).as_posix()
            try:
                text = authoring.read_text(target_root, call.name, relative)
            except SkillAuthoringError, OSError:
                continue
            found = _find(text, old, old, False)
            if found is not None:
                holders.append((relative, isinstance(found, FuzzyReplacement)))
    return holders


def _lf(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _json_unescaped(text: str) -> str:
    return _JSON_ESCAPE.sub(lambda match: _JSON_ESCAPES[match.group(1)], text)
