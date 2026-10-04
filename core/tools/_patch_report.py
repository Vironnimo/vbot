"""Model-facing apply_patch results: what changed, what failed, and what to send next.

A result is plain text in ``data.content`` under a ``status`` of ``applied``,
``partial`` or ``unchanged``. Each changed file reads as one section: an Update
shows its changed regions as they are now, numbered like ``read`` output, so the
next patch can reuse those lines directly. Failures show the closest current
text with line numbers. A call in which nothing applied is a failure envelope
with the same failure text.

The user reads the call through its display instead: the changed files' diffs
and one notice per note, warning and failed change, without the text the next
call needs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from core.tools._change_preview import _PREVIEW_CONTEXT_LINES, _change_preview
from core.tools._line_diff import line_opcodes
from core.tools.arguments import LINE_NUMBER_GUTTER_SEPARATOR, split_text_lines
from core.tools.model_names import model_tool_name
from core.tools.syntax_check import warning_for_edited_file, warning_for_written_file
from core.tools.tools import JsonObject, ToolContext, tool_failure, tool_success

_FAILED = {"failed", "skipped", "partial"}
_NOT_FOUND_CODES = {"text_not_found", "context_not_found", "line_numbered_content"}
_AMBIGUOUS_CODES = {"ambiguous_match", "ambiguous_context"}


@dataclass
class _FileReport:
    """One file's net effect in a call, as the Model reads it."""

    label: str
    kind: str  # created, replaced, updated, deleted or moved
    destination: str | None = None
    preview: list[str] = field(default_factory=list)
    line_count: int = 0
    notes: list[str] = field(default_factory=list)
    syntax_warning: str | None = None


def _gutter(number: int, text: str) -> str:
    return f"{number}{LINE_NUMBER_GUTTER_SEPARATOR} {text}"


def _after_preview(before: str, after: str) -> list[str]:
    """Return the changed regions of ``after`` with line numbers."""
    old_lines = split_text_lines(before, keepends=True)
    new_lines = split_text_lines(after, keepends=True)
    old_starts, new_starts = [0], [0]
    for line in old_lines:
        old_starts.append(old_starts[-1] + len(line))
    for line in new_lines:
        new_starts.append(new_starts[-1] + len(line))
    changes: list[list[int]] = []
    for tag, i1, i2, j1, j2 in line_opcodes(old_lines, new_lines):
        if tag == "equal":
            continue
        # Changes whose shown surroundings would touch or overlap read as one region.
        if changes and j1 - changes[-1][3] <= 2 * _PREVIEW_CONTEXT_LINES:
            changes[-1][1], changes[-1][3] = i2, j2
        else:
            changes.append([i1, i2, j1, j2])
    before_spans, after_spans = [], []
    for i1, i2, j1, j2 in changes:
        a, b, c, d = old_starts[i1], old_starts[i2], new_starts[j1], new_starts[j2]
        # Locate the changed characters inside a long line, not its first 240 characters.
        while a < b and c < d and before[a] == after[c]:
            a += 1
            c += 1
        while b > a and d > c and before[b - 1] == after[d - 1]:
            b -= 1
            d -= 1
        before_spans.append((a, b))
        after_spans.append((c, d))
    regions, omitted_regions = _change_preview(
        before, after, tuple(before_spans), tuple(after_spans)
    )
    lines: list[str] = []
    places = "place" if omitted_regions == 1 else "places"
    for index, region in enumerate(regions):
        if index:
            lines.append(
                f"-- ({omitted_regions} more changed {places} not shown)"
                if omitted_regions
                else "--"
            )
        shown = list(region["after"])
        omitted = region.get("after_omitted_lines", 0)
        if omitted:
            middle = len(shown) // 2
            shown[middle:middle] = [f"[{omitted} lines not shown]"]
        lines.extend(shown)
    return lines


def file_report(
    path: Path,
    label: str,
    kind: str,
    before: str | None,
    after: str | None,
    *,
    destination: str | None = None,
) -> _FileReport:
    """Describe one file's net effect; ``None`` text means absent or not text."""
    report = _FileReport(label, kind, destination)
    if kind == "deleted":
        return report
    if kind in {"created", "replaced"}:
        text = after or ""
        report.line_count = len(split_text_lines(text))
        warning = warning_for_written_file(path, text) if after is not None else None
        report.syntax_warning = warning
        return report
    if before is None or after is None or before == after:
        return report
    report.preview = _after_preview(before, after)
    report.syntax_warning = warning_for_edited_file(path, before, after)
    return report


def _file_text(report: _FileReport) -> str:
    plural = "" if report.line_count == 1 else "s"
    size = f"{report.line_count} line{plural}" if report.line_count else "empty"
    if report.kind == "created":
        lines = [f"Created {report.label} ({size})."]
    elif report.kind == "replaced":
        lines = [f"Replaced the content of {report.label} ({size})."]
    elif report.kind == "deleted":
        lines = [f"Deleted {report.label}."]
    elif report.kind == "moved" and report.preview:
        lines = [f"Updated {report.label} and moved it to {report.destination}:", *report.preview]
    elif report.kind == "moved":
        lines = [f"Moved {report.label} to {report.destination}."]
    else:
        lines = [f"Updated {report.label}:", *report.preview]
    lines.extend(f"Note: {note}" for note in dict.fromkeys(report.notes))
    if report.syntax_warning:
        lines.append(f"Warning: {report.syntax_warning}")
    return "\n".join(lines)


def _excerpts(candidates: list[JsonObject], path: str | None = None) -> list[str]:
    """Render file excerpts with line numbers; touching or overlapping ones merge.

    Read continuations cover only text that no excerpt shows.
    """
    groups: list[dict[int, str]] = []
    complete: set[int] = set()
    for candidate in sorted(candidates, key=lambda item: int(item["line"])):
        start = int(candidate["line"])
        lines = dict(enumerate(candidate["text"].split("\n"), start))
        cut = {line for line, character in _continuations(candidate) if character > 1}
        complete.update(set(lines) - cut)
        if groups and start <= max(groups[-1]) + 1:
            # Every excerpt line starts at its line's start; the longest shows the most.
            for number, text in lines.items():
                groups[-1][number] = max(groups[-1].get(number, ""), text, key=len)
        else:
            groups.append(lines)
    rendered: list[str] = []
    for index, group in enumerate(groups):
        if index:
            rendered.append("--")
        rendered.extend(_gutter(number, group[number]) for number in sorted(group))
    shown = {number: len(text) for group in groups for number, text in group.items()}
    windows = []
    for candidate in candidates:
        for (line, character), item in zip(
            _continuations(candidate), candidate.get("continuations", []), strict=True
        ):
            end = line + int(item["limit"])
            while line < end and line in complete:
                line, character = line + 1, 1
            if line < end:
                windows.append((line, max(character, shown.get(line, 0) + 1), end))
    continuations: list[tuple[int, int, int]] = []
    for line, character, end in sorted(windows):
        if continuations and line < continuations[-1][2]:
            first, first_character, last_end = continuations[-1]
            continuations[-1] = (first, first_character, max(last_end, end))
        else:
            continuations.append((line, character, end))
    if path and continuations:
        read = model_tool_name("read")
        calls = [
            f"{read}(path={json.dumps(path, ensure_ascii=False)}, "
            f'offset="{line}:{character}", limit={end - line})'
            for line, character, end in continuations
        ]
        rendered.append(
            f"Excerpt truncated. If {read} is available, continue with "
            + "; ".join(calls)
            + "; otherwise use another file reader."
        )
    elif continuations or any(
        candidate.get("truncated") and not candidate.get("continuations")
        for candidate in candidates
    ):
        rendered.append("Excerpt truncated.")
    return rendered


def _continuations(candidate: JsonObject) -> list[tuple[int, int]]:
    """Return a candidate's read continuations as (line, character) positions."""
    positions = []
    for item in candidate.get("continuations", []):
        line, _, character = str(item["offset"]).partition(":")
        positions.append((int(line), int(character or 1)))
    return positions


def _difference_text(difference: JsonObject) -> list[str]:
    source = "old_string" if difference["source"] == "old_string" else "the patch"
    # A line between + lines without a + prefix reads as unchanged; the report
    # names that reading, since the copy is often an added line missing its +.
    unprefixed = (
        [
            "That patch line has no + prefix, so it must already be in the file there; "
            "if it is new, start it with +."
        ]
        if difference.get("unprefixed")
        else []
    )
    if not difference.get("truncated"):
        return [
            f"First difference, line {difference['line']}: the file has "
            f"{difference['file']!r} where {source} has {difference['copy']!r}.",
            *unprefixed,
        ]
    lines = [
        f"First difference, file line {difference['line']}, "
        f"character {difference['character']}; copied line {difference['copy_line']}, "
        f"character {difference['copy_character']} (excerpts truncated):"
    ]
    for key, label in (("file", "File"), ("copy", source.capitalize())):
        start = difference[f"{key}_start"]
        end = start + len(difference[key]) - 1
        lines.append(
            f"{label} characters {start}-{end}: {difference[key]!r}"
            if difference[key]
            else f"{label} character {start}: end of line."
        )
    return [*lines, *unprefixed]


def _read_hint(error: JsonObject) -> str:
    """Name the read call that shows the file, after a space; empty without a path."""
    if not error.get("path_label"):
        return ""
    return f' {model_tool_name("read")}(path="{error["path_label"]}") shows its current content.'


def failure_text(error: JsonObject) -> str:
    """Render one failed change with the current text the next call needs."""
    lines = [str(error["message"])]
    code = error.get("code")
    candidates = error.get("candidates") or []
    if code in _NOT_FOUND_CODES or code == "eof_not_found":
        if candidates:
            if len(candidates) == 1:
                first = int(candidates[0]["line"])
                last = first + candidates[0]["text"].count("\n")
                where = f"line {first}" if first == last else f"lines {first}-{last}"
                lines.append(f"The closest text in the file, {where}:")
            else:
                lines.append("The closest texts in the file:")
            lines.extend(_excerpts(candidates, error.get("path_label")))
            difference = error.get("difference")
            if difference:
                lines.extend(_difference_text(difference))
        elif error.get("part_of"):
            part = error["part_of"]
            text = part["text"] if len(part["text"]) <= 80 else part["text"][:77] + "..."
            single = part["lines"].startswith("line ")
            whole = f"all of {part['lines']}" if single else "all of the line you mean"
            lines.append(
                f"The patch line {text!r} is only part of "
                f"{part['lines']}. Each patch line is a whole line, so copy {whole}:"
            )
            lines.extend(_excerpts(part["excerpts"], error.get("path_label")))
        elif error.get("absent"):
            absent = error["absent"]
            text = absent["text"] if len(absent["text"]) <= 80 else absent["text"][:77] + "..."
            if absent["removed"]:
                cause = "A - line names a line to remove, so it must match a line of the file."
            elif absent.get("beside_additions"):
                cause = (
                    "That patch line has no + prefix, so it must already be in the file; "
                    "if it is new, start it with +."
                )
            else:
                cause = "A line without + or - is unchanged, so it must match a line of the file."
            lines.append(f"The patch line {text!r} is not in the file. {cause}{_read_hint(error)}")
        elif error.get("path_label"):
            lines.append(f"No similar text is in the file;{_read_hint(error)}")
        present = error.get("already_present")
        if present:
            lines.append(
                f"The new text already occurs at line {present}; if this change was made "
                "earlier, nothing more is needed."
            )
    elif code in _AMBIGUOUS_CODES and candidates:
        lines.append("Where it occurs:")
        lines.extend(_excerpts(candidates, error.get("path_label")))
    if error.get("content"):
        lines.append(str(error["content"]))
    if error.get("completed_paths"):
        done = "Already done: " + ", ".join(error["completed_paths"]) + "."
        if error.get("pending_paths"):
            done += " Not done: " + ", ".join(error["pending_paths"]) + "."
        lines.append(done)
    if error.get("attempts_made"):
        lines.append(f"The write was attempted {error['attempts_made']} times.")
    return "\n".join(lines)


def _entry_text(entry: JsonObject) -> str:
    status = entry["status"]
    prefix = {"failed": "Failed", "skipped": "Skipped", "partial": "Incomplete"}[status]
    return f"{prefix}: {failure_text(entry['error'])}"


def _no_op_text(entry: JsonObject) -> str:
    if entry["status"] == "unchanged":
        return f"{entry['where']}: the new text equals the current text; nothing to change."
    if entry["action"] == "add":
        return f"{entry['where']} already has this content."
    if entry["action"] == "move":
        return f"{entry['where']} already has that name."
    return f"{entry['where']} already contains this change."


def _partial_lead(entries: list[JsonObject], failed: list[JsonObject]) -> str:
    total = len(entries)
    done = total - len(failed)
    incomplete = sum(entry["status"] == "partial" for entry in failed)
    missed = len(failed) - incomplete
    if incomplete:
        counts = f"{done} of {total} changes fully applied; {incomplete} only in part"
        if missed:
            counts += f"; {missed} not at all"
    else:
        counts = f"{done} of {total} changes applied; {missed} did not"
    return (
        f"{counts}. The applied changes stay in place: resend only the changes listed "
        "below as not done, based on the current text shown here."
    )


def _record_notices(
    context: ToolContext,
    files: list[_FileReport],
    notes: list[str],
    failed: list[JsonObject],
) -> None:
    """Record the user's notices: per-file notes and warnings, the call's notes, failures."""
    for report in files:
        for note in dict.fromkeys(report.notes):
            context.add_display_notice("info", note, subject=report.label)
        if report.syntax_warning:
            context.add_display_notice("warning", report.syntax_warning, subject=report.label)
    for note in notes:
        context.add_display_notice("info", note)
    # A failure message names the file and change it is about.
    for entry in failed:
        context.add_display_notice("error", str(entry["error"]["message"]))


def patch_result(
    context: ToolContext,
    files: list[_FileReport],
    entries: list[JsonObject],
    cancelled: list[str],
) -> JsonObject:
    """Return the Tool Result envelope for a completed apply_patch call."""
    failed = [entry for entry in entries if entry["status"] in _FAILED]
    no_ops = [entry for entry in entries if entry["status"] in {"unchanged", "already_applied"}]
    if failed and not files and len(failed) == len(entries):
        _record_notices(context, [], [], failed)
        if len(failed) == 1:
            error = failed[0]["error"]
            return tool_failure(
                error["code"],
                failure_text(error) + "\nNo file was changed.",
                retryable=error.get("retryable"),
                attempts_made=error.get("attempts_made"),
            )
        return tool_failure(
            "all_changes_failed",
            "\n".join(
                [
                    f"None of the {len(failed)} changes was applied; no file was changed.",
                    *(_entry_text(entry) for entry in failed),
                ]
            ),
        )
    sections: list[str] = []
    if failed:
        sections.append(_partial_lead(entries, failed))
    sections.extend(_file_text(report) for report in files)
    notes: list[str] = []
    # Changes that leave no net effect, such as a line changed and changed back.
    notes.extend(
        f"The changes to {path} cancel each other out, so it is the same as before."
        for path in cancelled
    )
    if files or failed:
        notes.extend(_no_op_text(entry) for entry in no_ops)
    else:
        # Say once per file why nothing was written.
        shown: dict[str, str] = {}
        for entry in no_ops:
            shown.setdefault(entry["path"], _no_op_text(entry))
        if shown:
            notes.append(" ".join(shown.values()) + ("" if cancelled else " No file was changed."))
    _record_notices(context, files, notes, failed)
    sections.extend(notes)
    sections.extend(_entry_text(entry) for entry in failed)
    status = "partial" if failed else "applied" if files else "unchanged"
    return tool_success({"status": status, "content": "\n".join(sections)})


__all__ = ["failure_text", "file_report", "patch_result"]
