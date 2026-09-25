"""Model-facing apply_patch results: what changed, what failed, and what to send next.

A result is plain text in ``data.content`` under a ``status`` of ``applied``,
``partial`` or ``unchanged``. Each changed file reads as one section: an Update
shows its changed regions as they are now, numbered like ``read`` output, so the
next patch can reuse those lines directly. Failures show the closest current
text with line numbers. A call in which nothing applied is a failure envelope
with the same failure text.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

from core.tools._change_preview import _PREVIEW_CONTEXT_LINES, _change_preview
from core.tools.arguments import LINE_NUMBER_GUTTER_SEPARATOR, split_text_lines
from core.tools.model_names import model_tool_name
from core.tools.syntax_check import warning_for_edited_file, warning_for_written_file
from core.tools.tools import JsonObject, tool_failure, tool_success

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


def _after_preview(before: str, after: str) -> tuple[list[str], int, int]:
    """Return the changed regions of ``after`` with line numbers, and line counts."""
    old_lines = split_text_lines(before, keepends=True)
    new_lines = split_text_lines(after, keepends=True)
    old_starts, new_starts = [0], [0]
    for line in old_lines:
        old_starts.append(old_starts[-1] + len(line))
    for line in new_lines:
        new_starts.append(new_starts[-1] + len(line))
    changes: list[list[int]] = []
    added = removed = 0
    for tag, i1, i2, j1, j2 in SequenceMatcher(
        None, old_lines, new_lines, autojunk=False
    ).get_opcodes():
        if tag == "equal":
            continue
        added += j2 - j1
        removed += i2 - i1
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
    return lines, added, removed


def file_report(
    path: Path,
    label: str,
    kind: str,
    before: str | None,
    after: str | None,
    *,
    destination: str | None = None,
) -> tuple[_FileReport, int, int]:
    """Describe one file's net effect; ``None`` text means absent or not text.

    Returns the report and the added and removed line counts.
    """
    report = _FileReport(label, kind, destination)
    old_count = len(split_text_lines(before)) if before is not None else 0
    if kind == "deleted":
        return report, 0, old_count
    if kind in {"created", "replaced"}:
        text = after or ""
        report.line_count = len(split_text_lines(text))
        warning = warning_for_written_file(path, text) if after is not None else None
        report.syntax_warning = warning
        return report, report.line_count, old_count
    if before is None or after is None or before == after:
        return report, 0, 0
    report.preview, added, removed = _after_preview(before, after)
    report.syntax_warning = warning_for_edited_file(path, before, after)
    return report, added, removed


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


def _excerpts(candidates: list[JsonObject]) -> list[str]:
    """Render file excerpts with line numbers; touching or overlapping ones merge."""
    groups: list[dict[int, str]] = []
    for candidate in sorted(candidates, key=lambda item: int(item["line"])):
        start = int(candidate["line"])
        lines = dict(enumerate(candidate["text"].split("\n"), start))
        if groups and start <= max(groups[-1]) + 1:
            groups[-1].update(lines)
        else:
            groups.append(lines)
    rendered: list[str] = []
    for index, group in enumerate(groups):
        if index:
            rendered.append("--")
        rendered.extend(_gutter(number, group[number]) for number in sorted(group))
    return rendered


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
            lines.extend(_excerpts(candidates))
            difference = error.get("difference")
            if difference:
                lines.append(
                    f"First difference, line {difference['line']}: the file has "
                    f"{difference['file']!r} where the patch has {difference['patch']!r}."
                )
        elif error.get("path_label"):
            read = model_tool_name("read")
            lines.append(
                f'No similar text is in the file; {read}(path="{error["path_label"]}") '
                "shows its current content."
            )
        present = error.get("already_present")
        if present:
            lines.append(
                f"The new text already occurs at line {present}; if this change was made "
                "earlier, nothing more is needed."
            )
    elif code in _AMBIGUOUS_CODES and candidates:
        lines.append("Where it occurs:")
        lines.extend(_excerpts(candidates))
    if error.get("content"):
        lines.append(str(error["content"]))
    if error.get("completed_paths"):
        done = "Already done: " + ", ".join(error["completed_paths"]) + "."
        if error.get("pending_paths"):
            done += " Not done: " + ", ".join(error["pending_paths"]) + "."
        lines.append(done)
    if error.get("attempts_made"):
        lines.append(f"The file was busy; {error['attempts_made']} attempts were made.")
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


def patch_result(
    files: list[_FileReport], entries: list[JsonObject], cancelled: list[str]
) -> JsonObject:
    """Return the Tool Result envelope for a completed apply_patch call."""
    failed = [entry for entry in entries if entry["status"] in _FAILED]
    no_ops = [entry for entry in entries if entry["status"] in {"unchanged", "already_applied"}]
    if failed and not files and len(failed) == len(entries):
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
    # Changes that leave no net effect, such as a line changed and changed back.
    notes = [
        f"The changes to {path} cancel each other out, so it is the same as before."
        for path in cancelled
    ]
    if files or failed:
        notes.extend(_no_op_text(entry) for entry in no_ops)
    else:
        # Say once per file why nothing was written.
        shown: dict[str, str] = {}
        for entry in no_ops:
            shown.setdefault(entry["path"], _no_op_text(entry))
        if shown:
            notes.append(" ".join(shown.values()) + ("" if cancelled else " No file was changed."))
    sections.extend(notes)
    sections.extend(_entry_text(entry) for entry in failed)
    status = "partial" if failed else "applied" if files else "unchanged"
    return tool_success({"status": status, "content": "\n".join(sections)})


__all__ = ["failure_text", "file_report", "patch_result"]
