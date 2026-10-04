"""Reconstruct file contents from a Session's Tool history for ``replay``.

A Session's Tool calls are simulated forward in call order. A file's content
becomes known from a complete ``read`` result (gutters stripped; ``read`` shows
CRLF and CR as LF, so its line endings are unknown), from an ``apply_patch``
call that writes the whole file (Add File, or ``path`` with ``content``), or
from the content a read-before-write refusal shows. A recorded successful call
advances the files it names by dispatching it again, through the current
engine, on the simulated contents; the dispatch must end with the recorded
status. Later evidence checks the simulation: ``read`` results (whole or
windowed), the changed regions a successful result shows with gutters, the
excerpts a failure shows, and line counts. Evidence that agrees confirms every
call made on that file since the last check; evidence that contradicts drops
those calls and resynchronizes from a complete view, or leaves the file
unknown. Shell commands, other Sessions and external writes change files
unseen; they show up only as contradictions, or as a result note that the
file changed after the Session last read it.

Paths are keyed per Session: absolute paths by their normalized lowercase
form, relative ones under the Session's working directory when absolute and
relative spellings of the same file reveal it, else by themselves.
"""

from __future__ import annotations

import json
import posixpath
import re
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.providers.adapter import tool_result_text
from core.tools._line_diff import line_opcodes
from core.tools._patch_requests import normalize_patch_arguments, patch_operations
from core.tools._patch_syntax import _HEADER, _MOVE_TO, _clean_path
from core.tools._read_arguments import normalize_read_arguments
from core.tools.apply_patch import patch_targets
from core.tools.arguments import split_text_lines
from core.tools.syntax_check import warning_for_edited_file

# Stands for the replay root in rewritten absolute paths; ``run`` substitutes it.
ROOT_TOKEN = "{replay-root}"
# Recorded failures that depend on the machine, not on the call and the file text.
ENVIRONMENTAL_CODES = frozenset({"file_write_error", "file_modified_since_read", "file_changed"})
# A later edit this close (in Tool calls) that revises added lines marks a suspect success.
SUSPECT_WINDOW = 6
_FILE_TOOLS = ("read", "apply_patch", "edit", "write")
_FULL_STATUSES = frozenset({"applied", "unchanged"})

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

_DRIVE = re.compile(r"[A-Za-z]:/")
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
    r"it changed after this Session last read it), so it was not replaced\."
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
    status = data.get("status")
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


# --- Paths ---------------------------------------------------------------------------


def _normal(name: str) -> str | None:
    """A path spelling with ``/`` separators and no ``.``/``..`` steps, or None if unusable."""
    text = name.strip().replace("\\", "/")
    if not text or "\x00" in text or text.startswith(("~", "//")):
        return None
    absolute = bool(_DRIVE.match(text)) or text.startswith("/")
    text = posixpath.normpath(text)
    if text in {".", "/"} or _DRIVE.fullmatch(text + "/"):
        return None
    if not absolute and (text == ".." or text.startswith("../") or ":" in text):
        return None
    return text


def _is_absolute(normal: str) -> bool:
    return bool(_DRIVE.match(normal)) or normal.startswith("/")


@dataclass(frozen=True, slots=True)
class SessionPaths:
    """Path keys and replay locations of one Session's file names."""

    cwd: str | None

    def key(self, name: str) -> str | None:
        normal = _normal(name)
        if normal is None:
            return None
        if _is_absolute(normal):
            return normal.lower()
        return f"{self.cwd}/{normal.lower()}" if self.cwd else f"./{normal.lower()}"

    def workspace(self, name: str) -> str | None:
        """Where the replay keeps the file, relative to its root (``cwd/...`` or ``abs/...``)."""
        normal = _normal(name)
        if normal is None:
            return None
        if not _is_absolute(normal):
            return f"cwd/{normal}"
        if self.cwd and normal.lower().startswith(self.cwd + "/"):
            return f"cwd/{normal[len(self.cwd) + 1 :]}"
        if _DRIVE.match(normal):
            return f"abs/{normal[0].lower()}/{normal[3:]}"
        return f"abs/_{normal}"


def call_paths(name: str, arguments: Any) -> list[str]:
    """The file names a read or file-edit call gives, as spelled."""
    if name == "read":
        try:
            normalized = normalize_read_arguments(arguments)
        except Exception:
            return []
        path = normalized.get("path") if isinstance(normalized, dict) else None
        return [path] if isinstance(path, str) else []
    try:
        return patch_targets(arguments)
    except Exception:
        return _header_paths(arguments)


def _header_paths(value: Any) -> list[str]:
    """File names in V4A header lines, for patches the parser refuses."""
    if isinstance(value, dict):
        return [path for item in value.values() for path in _header_paths(item)]
    if isinstance(value, list):
        return [path for item in value for path in _header_paths(item)]
    if not isinstance(value, str):
        return []
    paths: list[str] = []
    for line in value.split("\n"):
        line = line.rstrip("\r")
        if header := _HEADER.fullmatch(line):
            source, _arrow, destination = _clean_path(header[2]).partition(" -> ")
            paths.extend(_clean_path(part) for part in (source, destination) if part.strip())
        elif move := _MOVE_TO.fullmatch(line):
            paths.append(_clean_path(move[1]))
    return [path for path in paths if path]


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


def infer_cwd(calls: list[HistoryCall]) -> str | None:
    """The Session's working directory, when absolute and relative spellings reveal it.

    Strong evidence: a result labels a file relative to the working directory
    that the call named absolutely, or the reverse. Otherwise at least two
    relative names with a folder must each end an absolute name the same way.
    """
    strong: Counter[str] = Counter()
    absolute: set[str] = set()
    relative: set[str] = set()
    for call in calls:
        if call.name not in _FILE_TOOLS:
            continue
        names = [n for n in map(_normal, call_paths(call.name, call.arguments())) if n]
        for normal in names:
            (absolute if _is_absolute(normal) else relative).add(normal.lower())
        if call.name != "apply_patch":
            continue
        for label in filter(None, map(_normal, result_labels(call.envelope()))):
            for normal in names:
                first, second = normal.lower(), label.lower()
                if _is_absolute(second) and not _is_absolute(first):
                    first, second = second, first
                mixed = _is_absolute(first) and not _is_absolute(second)
                if mixed and first.endswith("/" + second):
                    strong[first[: -len(second) - 1]] += 1
    if strong:
        return strong.most_common(1)[0][0]
    weak: Counter[str] = Counter()
    for name in relative:
        if "/" not in name:
            continue
        for full in absolute:
            if full.endswith("/" + name):
                weak[full[: -len(name) - 1]] += 1
    if weak:
        best, votes = weak.most_common(1)[0]
        if votes >= 2:
            return best
    return None


def rewrite_paths(value: Any, replacements: dict[str, str]) -> Any:
    """Replace file names in path fields and patch header lines; content stays literal."""
    if not replacements:
        return value
    if isinstance(value, dict):
        return {key: rewrite_paths(item, replacements) for key, item in value.items()}
    if isinstance(value, list):
        return [rewrite_paths(item, replacements) for item in value]
    if not isinstance(value, str):
        return value
    if value.strip() in replacements:
        return value.replace(value.strip(), replacements[value.strip()])
    lines = value.split("\n")
    changed = False
    ordered = sorted(replacements.items(), key=lambda item: -len(item[0]))
    for index, line in enumerate(lines):
        bare = line.rstrip("\r")
        if not (
            _HEADER.fullmatch(bare)
            or _MOVE_TO.fullmatch(bare)
            or bare.startswith(("--- ", "+++ ", "diff --git ", "rename from ", "rename to "))
        ):
            continue
        new = bare
        for old, replacement in ordered:
            pattern = rf"(?<![^\s:'\"`>]){re.escape(old)}(?=$|[\s'\"`]|\s*->)"
            new = re.sub(pattern, _literal(replacement), new)
        if new != bare:
            lines[index] = new + line[len(bare) :]
            changed = True
    return "\n".join(lines) if changed else value


def _literal(text: str) -> Callable[[re.Match[str]], str]:
    """A ``re.sub`` replacement that inserts ``text`` as it is, backslashes included."""
    return lambda _match: text


# --- Simulation -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Dispatched:
    """One call dispatched in a scratch workspace: its outcome and the files after it."""

    ok: bool
    status: str
    error_code: str | None
    text: str
    files: dict[str, str | None]


type Dispatch = Callable[[Any, dict[str, str | None], set[str]], Awaitable[Dispatched]]


@dataclass(slots=True)
class _File:
    known: bool = False
    content: str | None = None
    source: str = ""
    # Whether the line endings are the file's own; ``read`` shows every line break as LF.
    exact: bool = True
    # Simulated changes since the content was last observed.
    steps: int = 0
    stamped: bool = False
    pending: list[_Case] = field(default_factory=list)
    # Read windows of the unknown file since its last change, until they cover it.
    pieces: View | None = None


@dataclass(slots=True)
class _Case:
    record: dict[str, Any]
    index: int
    run_id: str | None
    keys: dict[str, str]
    checks: dict[str, str] = field(default_factory=dict)
    contradicted: bool = False
    awaiting: set[str] = field(default_factory=set)
    eventual: dict[str, str | None] = field(default_factory=dict)
    between: int = 0
    own: dict[str, str | None] | None = None
    flags: set[str] = field(default_factory=set)


@dataclass(slots=True)
class _Watch:
    case: _Case
    index: int
    after: list[str]
    added: set[int]


@dataclass(slots=True)
class SessionStats:
    calls: int = 0
    cases: int = 0
    skipped: Counter[str] = field(default_factory=Counter)
    targets: Counter[str] = field(default_factory=Counter)
    verified: Counter[str] = field(default_factory=Counter)
    contradictions: int = 0
    # Recorded successes on fully known files that the current engine ends differently.
    mismatches: Counter[str] = field(default_factory=Counter)

    def merge(self, other: SessionStats) -> None:
        self.calls += other.calls
        self.cases += other.cases
        self.skipped.update(other.skipped)
        self.targets.update(other.targets)
        self.verified.update(other.verified)
        self.contradictions += other.contradictions
        self.mismatches.update(other.mismatches)


class SessionReplay:
    """Simulate one Session's file history and collect its replayable apply_patch calls."""

    def __init__(
        self,
        calls: list[HistoryCall],
        dispatch: Dispatch,
        *,
        admit: Callable[[HistoryCall], bool],
    ) -> None:
        self._calls = calls
        self._dispatch = dispatch
        self._admit = admit
        self.paths = SessionPaths(infer_cwd(calls))
        self._files: dict[str, _File] = {}
        self._cases: list[_Case] = []
        self._awaiting: list[_Case] = []
        self._watches: dict[str, list[_Watch]] = defaultdict(list)
        self.stats = SessionStats()

    async def run(self) -> list[dict[str, Any]]:
        for index, call in enumerate(self._calls):
            if call.name == "read":
                self._read(call)
            elif call.name == "apply_patch":
                await self._patch(index, call)
            elif call.name in {"edit", "write"} and call.ok:
                await self._legacy(index, call)
        return self._records()

    # Observations ------------------------------------------------------------------

    def _file(self, key: str) -> _File:
        return self._files.setdefault(key, _File())

    def _observe(self, key: str, view: View, source: str) -> str | None:
        """Check the file against a view; return the kind of check when they agree."""
        if view.garbled():
            return None
        state = self._file(key)
        kind = "full" if view.full else "window"
        if state.known and view.agrees(state.content):
            for case in state.pending:
                case.checks.setdefault(key, kind)
            state.pending.clear()
            if view.full:
                state.steps = 0
            return kind
        if state.known:
            self.stats.contradictions += 1
        self._lose(key)
        if view.content is not None:
            content = view.content + ("\n" if view.ending_unknown else "")
            state.known, state.content, state.source, state.steps = True, content, source, 0
            state.exact = False
        elif view.absent:
            state.known, state.content, state.source, state.steps = True, None, source, 0
            state.exact = True
        elif source == "read":
            self._piece_together(state, view)
        return None

    def _piece_together(self, state: _File, view: View) -> None:
        """Collect read windows of an unknown file; know it once they cover every line."""
        if view.total is None or not view.lines:
            return
        pieces = state.pieces
        if pieces is None or pieces.total != view.total:
            pieces = state.pieces = View(total=view.total)
        for number, entry in view.lines.items():
            if pieces.lines.setdefault(number, entry) != entry:
                pieces = state.pieces = View(total=view.total, lines=dict(view.lines))
                break
        if view.final_newline is not None:
            pieces.final_newline = view.final_newline
        content = pieces.complete_lines()
        if content is not None:
            state.known, state.content, state.source, state.steps = True, content, "reads", 0
            state.exact = False
            state.pieces = None

    def _lose(self, key: str) -> None:
        """Drop the calls made on this file since its last check; its content is unknown."""
        state = self._file(key)
        for case in state.pending:
            case.contradicted = True
        state.pending.clear()
        state.known, state.content = False, None

    def _read(self, call: HistoryCall) -> None:
        names = call_paths("read", call.arguments())
        key = self.paths.key(names[0]) if names else None
        if key is None:
            return
        envelope = call.envelope()
        if envelope is None or not envelope["ok"]:
            if call.error_code == "file_not_found":
                self._observe(key, View(absent=True), "read")
            return
        data = envelope.get("data")
        text = data.get("content") if isinstance(data, dict) else None
        view = read_view(text) if isinstance(text, str) else None
        if view is not None:
            self._observe(key, view, "read")
            self._file(key).stamped = True

    # apply_patch -----------------------------------------------------------------

    async def _patch(self, index: int, call: HistoryCall) -> None:
        admitted = self._admit(call)
        if admitted:
            self.stats.calls += 1
        arguments = call.arguments()
        result = recorded(call)
        if not isinstance(arguments, dict):
            if admitted:
                self.stats.skipped["unreadable arguments"] += 1
            return
        names = call_paths("apply_patch", arguments)
        keys = {name: self.paths.key(name) for name in names}
        known_keys = {key for key in keys.values() if key is not None}
        evidence = patch_evidence(call.envelope())
        resolve = self._label_resolver(keys)
        # What the result shows of the files as they were when the call ran.
        agreed: dict[str, str] = {}
        for label, view in evidence.before.items():
            if (key := resolve(label)) is not None and not (view.absent and key not in self._files):
                kind = self._observe(key, view, "guard" if label in evidence.shown else "result")
                if kind:
                    agreed[key] = kind
        if len(known_keys) == 1 and evidence.excerpts.says_anything():
            key = next(iter(known_keys))
            if kind := self._observe(key, evidence.excerpts, "result"):
                agreed.setdefault(key, kind)
        for label in evidence.stale:
            if (key := resolve(label)) is not None:
                self._lose(key)
                self._file(key).pieces = None
                self.stats.contradictions += 1
        guarded = {key for label in evidence.guarded if (key := resolve(label)) is not None}
        replaced = {key for label in evidence.replaced if (key := resolve(label)) is not None}
        case = None
        if admitted:
            case = self._case(index, call, result, arguments, keys, guarded, replaced)
        if case is not None:
            case.checks.update((key, kind) for key, kind in agreed.items() if key in case.keys)
        befores = {key: self._file(key).content for key in known_keys if self._file(key).known}
        if result.ok:
            await self._advance(index, call, arguments, keys, result, case, befores)
        for key in known_keys:
            state = self._file(key)
            if (result.ok and state.known and state.content is not None) or key in guarded:
                state.stamped = True
        for label, view in evidence.after.items():
            if (key := resolve(label)) is not None and view.says_anything():
                self._observe(key, view, "result")

    def _label_resolver(self, keys: dict[str, str | None]) -> Callable[[str], str | None]:
        values = {key for key in keys.values() if key is not None}

        def resolve(label: str) -> str | None:
            key = self.paths.key(label)
            if key in values:
                return key
            normal = _normal(label)
            if normal is None:
                return None
            lower = normal.lower()
            matches = {
                value
                for value in values
                if value.endswith("/" + lower) or lower.endswith("/" + value.removeprefix("./"))
            }
            return matches.pop() if len(matches) == 1 else None

        return resolve

    def _case(
        self,
        index: int,
        call: HistoryCall,
        result: Recorded,
        arguments: dict[str, Any],
        keys: dict[str, str | None],
        guarded: set[str],
        replaced: set[str],
    ) -> _Case | None:
        if result.error_code in ENVIRONMENTAL_CODES:
            self.stats.skipped["environmental failure"] += 1
            return None
        if any(key is None for key in keys.values()):
            self.stats.skipped["unsupported path"] += 1
            return None
        first = self._first_actions(arguments)
        workspace: dict[str, str] = {}
        for name, key in keys.items():
            assert key is not None
            state = self._files.get(key)
            if state is None or not state.known:
                missing = first.get(key) == "add" and key not in replaced
                if not missing and not (result.error_code == "file_not_found" and len(keys) == 1):
                    self.stats.skipped["unknown file state"] += 1
                    return None
                self._files[key] = _File(known=True, source="assumed absent")
            location = self.paths.workspace(name)
            assert location is not None
            workspace.setdefault(key, location)
        replacements = self._replacements(keys, workspace)
        replay_arguments = rewrite_paths(arguments, replacements)
        if not self._rewritten(replay_arguments, set(workspace.values())):
            self.stats.skipped["path rewrite"] += 1
            return None
        files = []
        for key, location in workspace.items():
            state = self._files[key]
            files.append(
                {
                    "path": location,
                    "content": state.content,
                    "read": key not in guarded and (state.stamped or bool(result.ok)),
                    "source": state.source,
                    "endings": "exact" if state.exact else "lf_assumed",
                    "steps": state.steps,
                }
            )
        record = {
            "id": f"{call.session_key}-{call.seq}-{call.ordinal}",
            "session": call.session_id,
            "run": call.run_id,
            "model": call.model,
            "created_at": call.created_at,
            "tool": call.name,
            "arguments": arguments,
            "replay_arguments": replay_arguments,
            "files": files,
            "recorded": result.as_json(),
        }
        case = _Case(record, index, call.run_id, workspace)
        for key in workspace:
            self._files[key].pending.append(case)
        if result.status in {"failed", "partial"}:
            case.awaiting = set(workspace)
            self._awaiting.append(case)
        self._cases.append(case)
        return case

    def _replacements(
        self, keys: dict[str, str | None], workspace: dict[str, str]
    ) -> dict[str, str]:
        replacements: dict[str, str] = {}
        for name, key in keys.items():
            normal = _normal(name)
            if key is None or normal is None or not _is_absolute(normal):
                continue
            location = workspace[key]
            if "\\" in name and "/" not in name:
                replacements[name] = ROOT_TOKEN + "\\" + location.replace("/", "\\")
            else:
                replacements[name] = f"{ROOT_TOKEN}/{location}"
        return replacements

    def _rewritten(self, arguments: dict[str, Any], expected: set[str]) -> bool:
        """Whether every file the rewritten call names is one of the replay's files."""
        found = set()
        for name in call_paths("apply_patch", arguments):
            if name.startswith(ROOT_TOKEN):
                found.add(name[len(ROOT_TOKEN) + 1 :].replace("\\", "/"))
            else:
                normal = _normal(name)
                if normal is None or _is_absolute(normal):
                    return False
                found.add(f"cwd/{normal}")
        return {path.lower() for path in found} == {path.lower() for path in expected}

    def _first_actions(self, arguments: Any) -> dict[str, str]:
        """For each file key, how the call first touches it: add, delete, move, update, into."""
        try:
            normalized = normalize_patch_arguments(arguments)
            operations = patch_operations(normalized) if isinstance(normalized, dict) else []
        except Exception:
            return {}
        first: dict[str, str] = {}
        for operation in operations:
            touched = ((operation.path, operation.action), (operation.destination, "into"))
            for name, action in touched:
                key = self.paths.key(name) if name else None
                if key is not None:
                    first.setdefault(key, action)
        return first

    async def _advance(
        self,
        index: int,
        call: HistoryCall,
        arguments: dict[str, Any],
        keys: dict[str, str | None],
        result: Recorded,
        case: _Case | None,
        befores: dict[str, str | None],
    ) -> None:
        """Apply a recorded successful call to the simulated files with the current engine."""
        workspace: dict[str, str] = {}
        for name, key in keys.items():
            location = self.paths.workspace(name)
            if key is not None and location is not None:
                workspace.setdefault(key, location)
        if len(workspace) < len(set(keys.values())):
            for key in workspace:
                self._lose(key)
            return
        first = self._first_actions(arguments)
        for key in workspace:
            self._file(key).pieces = None
        # Files the call creates or removes need no known content beforehand.
        derivable = {
            key
            for key in workspace
            if self._file(key).known or first.get(key) in {"add", "delete", "move"}
        }
        if not derivable:
            for key in workspace:
                self._lose(key)
            self._resolve(index, call, set(workspace), resolved=False)
            return
        replacements = self._replacements(keys, workspace)
        replay_arguments = rewrite_paths(arguments, replacements)
        files: dict[str, str | None] = {}
        for key, location in workspace.items():
            state = self._file(key)
            # A file the call deletes or moves away existed; its content does not matter.
            placeholder = "" if first.get(key) in {"delete", "move"} else None
            files[location] = state.content if state.known else placeholder
        stamped = {location for location, content in files.items() if content is not None}
        try:
            outcome = await self._dispatch(replay_arguments, files, stamped)
        except UnicodeEncodeError:
            outcome = Dispatched(False, "failed", "unwritable", "", {})
        all_known = all(self._file(key).known for key in workspace)
        if outcome.status != result.status and (all_known or not outcome.ok):
            if all_known:
                kind = f"{result.status} -> {outcome.status} {outcome.error_code or ''}"
                self.stats.mismatches[kind.strip()] += 1
            for key in workspace:
                self._lose(key)
            self._resolve(index, call, set(workspace), resolved=False)
            return
        # A file moved here takes the content of its source, which must be known.
        sources_known = all(self._file(k).known for k in workspace if first.get(k) != "into")
        afters: dict[str, str | None] = {}
        for key, location in workspace.items():
            state = self._file(key)
            if key in derivable or (first.get(key) == "into" and sources_known):
                after = outcome.files.get(location)
                if after != state.content or not state.known:
                    state.steps += 1
                state.exact = state.exact or not state.known
                state.known, state.content, state.source = True, after, "simulated"
                afters[key] = after
            else:
                self._lose(key)
        if case is not None and result.status in _FULL_STATUSES and set(case.keys) <= set(afters):
            case.own = {case.keys[key]: afters[key] for key in case.keys}
            self._watch(index, case, befores, afters)
        self._check_watches(index, set(workspace), befores, afters)
        # After a partial success, a later one also holds changes the failed call never meant.
        self._resolve(index, call, set(afters), resolved=result.status in _FULL_STATUSES)

    async def _legacy(self, index: int, call: HistoryCall) -> None:
        """Advance files through a call of the archived edit or write Tool."""
        arguments = call.arguments()
        if not isinstance(arguments, dict):
            return
        names = call_paths(call.name, arguments)
        keys = {name: self.paths.key(name) for name in names}
        befores = {k: self._file(k).content for k in keys.values() if k and self._file(k).known}
        legacy = Recorded(True, "applied", None, "", ())
        await self._advance(index, call, arguments, keys, legacy, None, befores)
        for key in keys.values():
            if key is not None and self._file(key).known:
                self._file(key).stamped = True

    # Targets and suspicion flags -----------------------------------------------------

    def _resolve(self, index: int, call: HistoryCall, keys: set[str], *, resolved: bool) -> None:
        """Give earlier failed calls of the Run the content after this success as target.

        With ``resolved`` false the calls lose their claim on these files instead.
        """
        for case in self._awaiting:
            if case.run_id != call.run_id or case.index >= index:
                continue
            for key in case.awaiting & keys:
                state = self._file(key)
                if resolved and state.known:
                    case.eventual[case.keys[key]] = state.content
                    case.between = max(case.between, index - case.index - 1)
                case.awaiting.discard(key)
        self._awaiting = [case for case in self._awaiting if case.awaiting]

    def _watch(
        self,
        index: int,
        case: _Case,
        befores: dict[str, str | None],
        afters: dict[str, str | None],
    ) -> None:
        for key, location in case.keys.items():
            before, after = befores.get(key), afters.get(key)
            if before is None or after is None or before == after:
                continue
            if Path(location).suffix.lower() in {".py", ".json"}:
                warning = warning_for_edited_file(Path(location), before, after)
                if warning and warning.startswith("Syntax check failed after this edit"):
                    case.flags.add("S")
            old, new = split_text_lines(before), split_text_lines(after)
            added = {
                j
                for tag, _i1, _i2, j1, j2 in line_opcodes(old, new)
                if tag in {"insert", "replace"}
                for j in range(j1, j2)
            }
            if added:
                self._watches[key].append(_Watch(case, index, new, added))

    def _check_watches(
        self,
        index: int,
        keys: set[str],
        befores: dict[str, str | None],
        afters: dict[str, str | None],
    ) -> None:
        """Flag the last successes on these files whose added lines this call revises."""
        for key in keys:
            watches = self._watches.get(key)
            if not watches:
                continue
            before, after = befores.get(key), afters.get(key)
            remaining = []
            for watch in watches:
                if watch.index == index or index - watch.index > SUSPECT_WINDOW:
                    if watch.index == index:
                        remaining.append(watch)
                    continue
                if before is None or after is None:
                    continue
                old = split_text_lines(before)
                mapping = _line_map(watch.after, old)
                removed = {
                    i
                    for tag, i1, i2, _j1, _j2 in line_opcodes(old, split_text_lines(after))
                    if tag in {"delete", "replace"}
                    for i in range(i1, i2)
                }
                revised = {line for line in watch.added if mapping.get(line) in removed}
                if revised:
                    watch.case.flags.add("H2")
                if any(_repeats_neighbor(watch.after, line) for line in revised):
                    watch.case.flags.add("H1")
            self._watches[key] = remaining

    # Output ----------------------------------------------------------------------

    def _records(self) -> list[dict[str, Any]]:
        records = []
        for case in self._cases:
            if case.contradicted:
                self.stats.skipped["contradicted by later evidence"] += 1
                continue
            record = case.record
            checks = [case.checks.get(key) for key in case.keys]
            if checks and all(check == "full" for check in checks):
                verified = "full"
            elif checks and all(checks):
                verified = "window"
            else:
                verified = "none"
            record["verified"] = verified
            record["target"] = None
            if case.own is not None:
                record["target"] = {"label": "self", "files": case.own, "flags": sorted(case.flags)}
            elif case.eventual:
                record["target"] = {
                    "label": "eventual",
                    "files": case.eventual,
                    "between": case.between,
                }
            self.stats.cases += 1
            self.stats.verified[verified] += 1
            self.stats.targets[record["target"]["label"] if record["target"] else "none"] += 1
            records.append(record)
        return records


def _line_map(old: list[str], new: list[str]) -> dict[int, int]:
    """Map line indexes of ``old`` to the equal lines of ``new``."""
    if old == new:
        return {index: index for index in range(len(old))}
    mapping: dict[int, int] = {}
    for tag, i1, i2, j1, _j2 in line_opcodes(old, new):
        if tag == "equal":
            mapping.update((i1 + offset, j1 + offset) for offset in range(i2 - i1))
    return mapping


def _repeats_neighbor(lines: list[str], index: int) -> bool:
    text = lines[index].strip()
    if not text:
        return False
    return any(
        0 <= other < len(lines) and lines[other].strip() == text for other in (index - 1, index + 1)
    )
