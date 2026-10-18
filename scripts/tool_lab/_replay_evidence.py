"""Decode recorded Tool history and the file evidence in its results."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.providers.adapter import tool_result_text
from core.tools._patch_report import edit_outcome
from core.tools.arguments import split_text_lines

_HISTORY_SQL = """
SELECT e.session_key, s.session_id, s.agent_id, r.run_id, e.seq, c.ordinal, e.created_at, e.model,
       c.name, c.result_ok, COALESCE(c.error_code, c.rejection_code) AS error_code,
       CASE WHEN c.name IN ('read', 'apply_patch', 'edit', 'write') THEN p.arguments_json END
         AS arguments_json,
       CASE WHEN c.name IN ('read', 'apply_patch', 'edit', 'write') THEN rt.content END
         AS result
FROM tool_calls AS c
JOIN entries AS e ON e.entry_key = c.entry_key
JOIN sessions AS s ON s.session_key = e.session_key
JOIN tool_call_payloads AS p ON p.call_key = c.call_key
LEFT JOIN runs AS r ON r.run_key = e.run_key
LEFT JOIN entry_text AS rt ON rt.entry_key = c.result_entry_key
WHERE e.session_key IN (
    SELECT DISTINCT e2.session_key FROM tool_calls AS c2
    JOIN entries AS e2 ON e2.entry_key = c2.entry_key
    WHERE c2.name = 'apply_patch')
ORDER BY e.session_key, e.seq, c.ordinal
"""

_GUTTER = re.compile(r"(\d+)(?::(\d+))?\| (.*)", re.DOTALL)
_READ_HINT = re.compile(r"\[Showing lines \d+-\d+(?: of (\d+))?\.")
_BEYOND = re.compile(r"\[Offset \d+ is beyond end of file \((\d+) lines\)")
_EMPTY_READ = re.compile(r"\[.+ is empty\.\]")
_CONTINUES = " [line continues;"
_REPLACEMENT = "\ufffd"
_WRITTEN = re.compile(r"(Created|Replaced the content of) (.+) \((?:(\d+) lines?|empty)\)\.")
_DELETED = re.compile(r"Deleted (.+)\.")
_MOVED = re.compile(r"Moved (.+) to (.+)\.")
_UPDATED = re.compile(r"Updated (.+?)(?: and moved it to (.+))?:")
_GUARDED = re.compile(
    r"(?:Failed: |Error \(\w+\): )?(.+) already exists and (?:this Session has not read it|"
    r"was not read with read in this conversation|it changed after this Session last read it), "
    r"so it was not replaced\."
)
_STALE = re.compile(r"(.+) changed after this Session last read it; the change used its")
_EXCERPT_LEADS = (
    "The closest text",
    "Where it occurs:",
    "so copy all of",
    "The unchanged lines match",
    "The unchanged lines occur",
)


class ReplayError(ValueError):
    """The corpus or its output location cannot be used as given."""


@dataclass(frozen=True, slots=True)
class HistoryCall:
    """One Tool call of a Session, with what the replay needs of it."""

    session_key: int
    session_id: str
    agent_id: str
    run_id: str | None
    seq: int
    ordinal: int
    created_at: str
    model: str | None
    name: str
    ok: bool | None
    error_code: str | None
    arguments_json: str | None
    result: str | None

    def arguments(self) -> Any:
        try:
            return json.loads(self.arguments_json) if self.arguments_json else None
        except json.JSONDecodeError:
            return None

    def envelope(self) -> dict[str, Any] | None:
        try:
            value = json.loads(self.result) if self.result else None
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) and isinstance(value.get("ok"), bool) else None


def session_histories(database: Path) -> Iterator[list[HistoryCall]]:
    """Yield, per Session with an apply_patch call, all its Tool calls in order."""
    connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        current: list[HistoryCall] = []
        for row in connection.execute(_HISTORY_SQL):
            call = HistoryCall(
                session_key=row["session_key"],
                session_id=row["session_id"],
                agent_id=row["agent_id"],
                run_id=row["run_id"],
                seq=row["seq"],
                ordinal=row["ordinal"],
                created_at=row["created_at"],
                model=row["model"],
                name=row["name"],
                ok=None if row["result_ok"] is None else bool(row["result_ok"]),
                error_code=row["error_code"],
                arguments_json=row["arguments_json"],
                result=row["result"],
            )
            if current and current[-1].session_key != call.session_key:
                yield current
                current = []
            current.append(call)
        if current:
            yield current
    finally:
        connection.close()


# --- Recorded results -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Recorded:
    """How a call ended when it ran: ``status`` is applied, partial, unchanged or failed."""

    ok: bool | None
    status: str
    error_code: str | None
    text: str
    notes: tuple[str, ...]

    def as_json(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status,
            "error_code": self.error_code,
            "text": self.text,
            "notes": list(self.notes),
        }


def recorded(call: HistoryCall) -> Recorded:
    """Read a recorded result: plain-text ``content`` since 2026-09-26, structured before."""
    envelope = call.envelope()
    text = str(tool_result_text(call.result)) if call.result else ""
    if envelope is None or not envelope["ok"]:
        return Recorded(False if envelope else call.ok, "failed", call.error_code, text, ())
    data = envelope.get("data")
    data = data if isinstance(data, dict) else {}
    notes: list[str] = []
    if isinstance(data.get("content"), str):
        notes = [line[6:] for line in data["content"].split("\n") if line.startswith("Note: ")]
    for entry in data.get("files") or ():
        if isinstance(entry, dict):
            notes.extend(str(warning) for warning in entry.get("warnings") or ())
    # Results carry their outcome only in their text since 2026-10; earlier ones
    # recorded it as ``status``.
    status = data.get("status")
    if status is None and isinstance(data.get("content"), str):
        status = edit_outcome(data["content"])
    if status == "success":
        results = [entry for entry in data.get("results") or () if isinstance(entry, dict)]
        no_ops = {"unchanged", "already_applied"}
        quiet = results and all(entry.get("status") in no_ops for entry in results)
        status = "unchanged" if quiet else "applied"
    if status not in {"applied", "partial", "unchanged"}:
        status = "applied"
    return Recorded(True, str(status), None, text, tuple(notes))


# --- Views of a file ----------------------------------------------------------------


@dataclass(slots=True)
class View:
    """What one observation shows of a file at one moment.

    ``content`` is the whole text with LF line breaks; ``lines`` maps a line
    number to (first character shown, text shown, whether only a prefix shows).
    ``ending_unknown`` marks content shown without its final line break;
    ``final_newline`` tells, when known, whether the last line ends with one.
    """

    content: str | None = None
    absent: bool = False
    lines: dict[int, tuple[int, str, bool]] = field(default_factory=dict)
    total: int | None = None
    ending_unknown: bool = False
    final_newline: bool | None = None

    @property
    def full(self) -> bool:
        return self.content is not None or self.absent

    def says_anything(self) -> bool:
        return self.full or bool(self.lines) or self.total is not None

    def garbled(self) -> bool:
        """Whether it shows replacement characters: text that was not UTF-8."""
        if self.content is not None and _REPLACEMENT in self.content:
            return True
        return any(_REPLACEMENT in shown for _character, shown, _cut in self.lines.values())

    def complete_lines(self) -> str | None:
        """The whole text when the shown lines cover every line uncut, else None.

        Without word on the final line break, the text ends with one.
        """
        if self.total is None or len(self.lines) != self.total:
            return None
        shown = []
        for number in range(1, self.total + 1):
            entry = self.lines.get(number)
            if entry is None or entry[0] != 1 or entry[2]:
                return None
            shown.append(entry[1])
        return "\n".join(shown) + ("" if self.final_newline is False else "\n")

    def agrees(self, content: str | None) -> bool:
        if self.absent:
            return content is None
        if content is None:
            return False
        text = plain_text(content)
        if self.content is not None:
            if self.ending_unknown:
                return text.removesuffix("\n") == self.content.removesuffix("\n")
            return text == self.content
        lines = split_text_lines(text)
        if self.total is not None and len(lines) != self.total:
            return False
        if self.final_newline is not None and text.endswith("\n") != self.final_newline:
            return False
        for number, (character, shown, prefix) in self.lines.items():
            if number > len(lines):
                return False
            line = lines[number - 1][character - 1 :]
            if not (line.startswith(shown) if prefix else line == shown):
                return False
        return True

    def add_gutter(self, line: str, *, prefix: bool = False, clipped: bool = True) -> bool:
        """Add one ``N| text`` line; return whether it was one.

        ``clipped``: a trailing ``...`` marks a line cut at the preview width.
        """
        match = _GUTTER.fullmatch(line)
        if match is None:
            return False
        number, character, shown = int(match[1]), int(match[2] or 1), match[3]
        cut = prefix or character > 1
        if clipped and shown.endswith("..."):
            shown, cut = shown[:-3], True
        if _CONTINUES in shown:
            shown, cut = shown.split(_CONTINUES, 1)[0], True
        self.lines[number] = (character, shown, cut)
        return True


def plain_text(content: str) -> str:
    """Text as ``read`` shows it: no BOM, every line break a LF."""
    return content.removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n")


def read_view(text: str) -> View | None:
    """What a successful ``read`` result shows of its file, or None when it shows no text.

    ``read`` adds a hint only when it stops before the end of the file, so
    numbered lines without one run to the end. Every shown line keeps its own
    line break; a last line without one is followed by a blank line before a
    hint.
    """
    stripped = text.strip()
    if _EMPTY_READ.fullmatch(stripped):
        return View(content="")
    beyond = _BEYOND.match(stripped)
    if beyond:
        return View(total=int(beyond[1]))
    lines = text.split("\n")
    hint = _READ_HINT.match(lines[-1]) if lines else None
    view = View()
    if hint:
        view.total = int(hint[1]) if hint[1] else None
        lines = lines[:-1]
    blank = bool(lines) and lines[-1] == ""
    body = lines[:-1] if blank else lines
    numbers: list[int | None] = []
    for line in body:
        match = _GUTTER.fullmatch(line)
        numbers.append(int(match[1]) if match is not None and match[2] is None else None)
        view.add_gutter(line, clipped=False)
    first = numbers[0] if numbers else None
    if first is None or numbers != list(range(first, first + len(numbers))):
        return view if view.says_anything() else None
    last = first + len(numbers) - 1
    if not hint:
        if first == 1:
            shown = [view.lines[number][1] for number in range(1, last + 1)]
            return View(content="\n".join(shown) + ("\n" if blank else ""))
        view.total, view.final_newline = last, blank
    elif view.total == last:
        view.final_newline = not blank
    return view


# --- Evidence in apply_patch results ----------------------------------------------------


@dataclass(slots=True)
class PatchEvidence:
    """What a recorded apply_patch result shows, by the file labels it uses.

    ``excerpts`` are failure excerpts whose file is the call's only file.
    """

    before: dict[str, View] = field(default_factory=dict)
    after: dict[str, View] = field(default_factory=dict)
    excerpts: View = field(default_factory=View)
    guarded: set[str] = field(default_factory=set)
    shown: set[str] = field(default_factory=set)
    stale: set[str] = field(default_factory=set)
    replaced: set[str] = field(default_factory=set)


def patch_evidence(envelope: dict[str, Any] | None) -> PatchEvidence:
    evidence = PatchEvidence()
    if envelope is None:
        return evidence
    data = envelope.get("data")
    if envelope["ok"] and isinstance(data, dict) and isinstance(data.get("files"), list):
        _structured_evidence(data, evidence)
        return evidence
    if envelope["ok"] and isinstance(data, dict) and isinstance(data.get("content"), str):
        _text_evidence(data["content"], evidence)
    elif not envelope["ok"] and isinstance(envelope.get("error"), dict):
        error = envelope["error"]
        text = f"Error ({error.get('code')}): {error.get('message', '')}"
        if isinstance(error.get("content"), str):
            text += "\n" + error["content"]
        _text_evidence(text, evidence)
    return evidence


def _text_evidence(text: str, evidence: PatchEvidence) -> None:
    """Read a plain-text result: changed regions, written files, refusals, excerpts."""
    mode: str | None = None
    label = ""
    guard: list[str] = []

    def close_guard() -> None:
        if guard:
            numbers = [_GUTTER.fullmatch(line) for line in guard]
            if all(
                m is not None and m[2] is None and int(m[1]) == i for i, m in enumerate(numbers, 1)
            ):
                content = "\n".join(m[3] for m in numbers if m is not None)
                evidence.before[label] = View(content=content, ending_unknown=True)
                evidence.shown.add(label)
        guard.clear()

    for line in text.split("\n"):
        if mode is not None and _GUTTER.fullmatch(line):
            if mode == "guard":
                guard.append(line)
            elif mode == "after":
                evidence.after.setdefault(label, View()).add_gutter(line)
            else:
                evidence.excerpts.add_gutter(line, prefix=True)
            continue
        if mode in {"after", "excerpt"} and _is_separator(line):
            continue
        if mode == "guard":
            close_guard()
        mode = None
        if match := _UPDATED.fullmatch(line):
            mode, label = "after", match[2] or match[1]
        elif match := _WRITTEN.fullmatch(line):
            evidence.after[match[2]] = View(total=int(match[3] or 0))
            if match[1] == "Created":
                evidence.before[match[2]] = View(absent=True)
            else:
                evidence.replaced.add(match[2])
        elif (match := _DELETED.fullmatch(line)) or (match := _MOVED.fullmatch(line)):
            evidence.after[match[1]] = View(absent=True)
        elif line.startswith("Note: ") and (match := _STALE.match(line[6:])):
            evidence.stale.add(match[1])
        elif match := _GUARDED.match(line):
            evidence.guarded.add(match[1])
            if "Its current content follows" in line:
                mode, label = "guard", match[1]
        elif any(lead in line for lead in _EXCERPT_LEADS) and line.endswith(":"):
            mode = "excerpt"
    if mode == "guard":
        close_guard()


def _is_separator(line: str) -> bool:
    """A line between shown regions of a result: ``--`` or a note on lines left out."""
    shown_out = line.startswith("[") and "not shown]" in line
    return line == "--" or line.startswith("-- (") or shown_out


def _structured_evidence(data: dict[str, Any], evidence: PatchEvidence) -> None:
    """Read a structured result (before 2026-09-26): previews and failure candidates."""
    for entry in data["files"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            continue
        label = entry["path"]
        if entry.get("action") == "delete":
            evidence.after[label] = View(absent=True)
            continue
        for region in entry.get("preview") or ():
            if not isinstance(region, dict):
                continue
            for side, views in (("before", evidence.before), ("after", evidence.after)):
                if side == "before" and entry.get("action") == "add":
                    continue
                lines = [line for line in region.get(side) or () if isinstance(line, str)]
                start = region.get(f"{side}_line")
                cut = set(region.get(f"{side}_truncated_lines") or ())
                view = views.setdefault(label, View())
                for offset, line in enumerate(lines):
                    if isinstance(start, int):
                        number = start + offset
                        view.lines[number] = (1, line, number in cut)
                    else:
                        view.add_gutter(line)
    for result in data.get("results") or ():
        error = result.get("error") if isinstance(result, dict) else None
        if not isinstance(error, dict) or not isinstance(result.get("path"), str):
            continue
        for candidate in error.get("candidates") or ():
            if not isinstance(candidate, dict) or candidate.get("truncated"):
                continue
            first, text = candidate.get("line"), candidate.get("text")
            if isinstance(first, int) and isinstance(text, str):
                view = evidence.before.setdefault(result["path"], View())
                for offset, line in enumerate(text.split("\n")):
                    view.lines[first + offset] = (1, line, True)


def result_labels(envelope: dict[str, Any] | None) -> list[str]:
    """The file labels a successful apply_patch result names."""
    if envelope is None or not envelope["ok"] or not isinstance(envelope.get("data"), dict):
        return []
    data = envelope["data"]
    labels = [
        entry["path"]
        for key in ("files", "results")
        for entry in data.get(key) or ()
        if isinstance(entry, dict) and isinstance(entry.get("path"), str)
    ]
    if isinstance(data.get("content"), str):
        for line in data["content"].split("\n"):
            for pattern in (_UPDATED, _WRITTEN, _DELETED, _MOVED):
                if match := pattern.fullmatch(line):
                    labels.extend(
                        group
                        for group in match.groups()
                        if group
                        and not group.isdigit()
                        and not group.startswith(("Created", "Replaced"))
                    )
    return labels
