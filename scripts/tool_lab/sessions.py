"""How Agents actually called the Tools: measurements from a copy of a sessions database.

The source is never opened for writing. It is copied with SQLite's online
backup API in short steps, so a running server keeps working. A
pre-Generation-1 copy is converted with the persistence converter, so this
module reads only the current schema. ``--work DIR`` keeps the prepared copy
for later runs; otherwise it is removed at the end.

The overview counts calls, failures, error codes and result sizes per Tool. A
Tool view adds the argument keys Agents sent (marking keys the current schema
lacks), clustered error messages with an example call, what followed each
failure in the same Run, repeated identical calls, and failure rates per Model.
Printed arguments and messages are clipped and pass through a secret filter.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import statistics
import tempfile
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from scripts.converters.persistence_generation_1.conversion import convert_data_directory

DATABASE_NAME = "sessions.db"
_BACKUP_PAGES_PER_STEP = 4000
_BACKUP_PAUSE_SECONDS = 0.02

_SECRET = re.compile(
    r"(?i)(sk-[A-Za-z0-9_\-]{8,}|gh[pous]_[A-Za-z0-9]{20,}|xox[abp]-[A-Za-z0-9\-]{10,}"
    r"|AIza[0-9A-Za-z_\-]{20,}|eyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{10,}"
    r"|(?:api[_-]?key|token|secret|password|bearer)[\"'=:\s]+[A-Za-z0-9_\-\.]{12,})"
)
_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"|“[^”]*”|„[^“]*“")
# A path starts a word: "arguments/mode" in a schema message is no path.
_PATH = re.compile(r"(?<![\w.])(?:[A-Za-z]:)?[\\/][^\s,;:'\"]+")
_NUMBER = re.compile(r"\d+")

_CALLS_SQL = """
SELECT c.call_key, c.name, c.status, c.result_ok, c.error_code, c.rejection_code,
       p.arguments_json, p.rejection_message,
       e.session_key, e.run_key, e.seq, c.ordinal, e.created_at, e.model,
       s.agent_id, s.project_id, s.is_subagent, r.run_kind,
       length(rt.content) AS result_chars,
       CASE WHEN json_valid(rt.content) THEN json_extract(rt.content, '$.error.message') END
         AS error_message,
       CASE WHEN json_valid(rt.content) THEN json_extract(rt.content, '$.data.status') END
         AS data_status
FROM tool_calls AS c
JOIN entries AS e ON e.entry_key = c.entry_key
JOIN sessions AS s ON s.session_key = e.session_key
JOIN tool_call_payloads AS p ON p.call_key = c.call_key
LEFT JOIN runs AS r ON r.run_key = e.run_key
LEFT JOIN entry_text AS rt ON rt.entry_key = c.result_entry_key
ORDER BY e.session_key, e.seq, c.ordinal
"""


class SessionsSourceError(ValueError):
    """The source cannot be read as a vBot sessions database."""


@dataclass(frozen=True, slots=True)
class CallRecord:
    name: str
    status: str
    ok: bool | None
    error_code: str | None
    error_message: str | None
    data_status: str | None
    arguments_json: str
    session: int
    run: int | None
    seq: int
    ordinal: int
    created_at: str
    model: str | None
    agent_id: str
    project_id: str
    subagent: bool
    run_kind: str | None
    result_chars: int

    @property
    def failed(self) -> bool:
        return self.ok is False or self.error_code is not None

    @property
    def run_scope(self) -> tuple[int, int | None]:
        return (self.session, self.run)

    def arguments(self) -> Any:
        try:
            return json.loads(self.arguments_json)
        except json.JSONDecodeError:
            return None


@dataclass(frozen=True, slots=True)
class Filters:
    since: str | None = None
    until: str | None = None
    model: str | None = None
    agent: str | None = None

    def admit(self, record: CallRecord) -> bool:
        if self.since and record.created_at < self.since:
            return False
        if self.until and record.created_at >= self.until:
            return False
        if self.model and self.model.lower() not in (record.model or "").lower():
            return False
        return not (self.agent and record.agent_id != self.agent)


@contextmanager
def prepared_database(source: Path, *, work: Path | None) -> Iterator[Path]:
    """Yield a current-schema copy of ``source`` (a sessions.db or a data directory)."""
    source = source / DATABASE_NAME if source.is_dir() else source
    if not source.is_file():
        raise SessionsSourceError(f"{source} does not exist")
    if work is not None and (work / DATABASE_NAME).is_file():
        yield work / DATABASE_NAME
        return
    if work is not None:
        work.mkdir(parents=True, exist_ok=True)
        yield _prepare(source, work)
        return
    with tempfile.TemporaryDirectory(prefix="vbot-tool-lab-sessions-") as tmp:
        yield _prepare(source, Path(tmp))


def _prepare(source: Path, work: Path) -> Path:
    target = work / DATABASE_NAME
    reader = sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)
    writer = sqlite3.connect(target)
    try:
        reader.backup(writer, pages=_BACKUP_PAGES_PER_STEP, sleep=_BACKUP_PAUSE_SECONDS)
    finally:
        writer.close()
        reader.close()
    shape = _shape(target)
    if shape == "legacy":
        # The converter works on data directories; the copy is one on its own.
        convert_data_directory(work)
        shutil.rmtree(work / "pre-generation-1", ignore_errors=True)
    elif shape != "current":
        raise SessionsSourceError(f"{source} is not a vBot sessions database")
    return target


def _shape(path: Path) -> str:
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        connection.close()
    if "entries" in tables and "tool_calls" in tables and "messages" not in tables:
        return "current"
    return "legacy" if "messages" in tables else "unknown"


def load_calls(database: Path) -> list[CallRecord]:
    connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        return [_record(row) for row in connection.execute(_CALLS_SQL)]
    finally:
        connection.close()


def _record(row: sqlite3.Row) -> CallRecord:
    message = row["error_message"] if row["error_message"] is not None else row["rejection_message"]
    return CallRecord(
        name=row["name"],
        status=row["status"],
        ok=None if row["result_ok"] is None else bool(row["result_ok"]),
        error_code=row["error_code"] or row["rejection_code"],
        error_message=message,
        data_status=row["data_status"] if isinstance(row["data_status"], str) else None,
        arguments_json=row["arguments_json"],
        session=row["session_key"],
        run=row["run_key"],
        seq=row["seq"],
        ordinal=row["ordinal"],
        created_at=row["created_at"],
        model=row["model"],
        agent_id=row["agent_id"],
        project_id=row["project_id"],
        subagent=bool(row["is_subagent"]),
        run_kind=row["run_kind"],
        result_chars=row["result_chars"] or 0,
    )


def overview(records: list[CallRecord]) -> str:
    if not records:
        return "No Tool calls match."
    first = min(record.created_at for record in records)[:10]
    last = max(record.created_at for record in records)[:10]
    sessions = len({record.session for record in records})
    lines = [
        f"{len(records)} Tool calls in {sessions} Sessions, {first} to {last}",
        "",
        f"  {'Tool':<22} {'calls':>7} {'failed':>7} {'fail%':>6} {'p50 chars':>9} "
        f"{'p90 chars':>9}  top errors",
    ]
    by_tool: dict[str, list[CallRecord]] = defaultdict(list)
    for record in records:
        by_tool[record.name].append(record)
    for name, calls in sorted(by_tool.items(), key=lambda item: -len(item[1])):
        failed = [call for call in calls if call.failed]
        sizes = sorted(call.result_chars for call in calls)
        codes = Counter(call.error_code or "failed" for call in failed).most_common(3)
        lines.append(
            f"  {name:<22} {len(calls):>7} {len(failed):>7} {_percent(len(failed), len(calls)):>6} "
            f"{_quantile(sizes, 0.5):>9} {_quantile(sizes, 0.9):>9}  "
            + ", ".join(f"{code} {count}" for code, count in codes)
        )
    return "\n".join(lines)


def tool_view(
    records: list[CallRecord],
    tool: str,
    *,
    schema_keys: frozenset[str] | None,
    by_argument: str | None = None,
    examples: int = 3,
) -> str:
    calls = [record for record in records if record.name == tool]
    if not calls:
        return f"No calls of {tool} match."
    failed = [call for call in calls if call.failed]
    lines = [
        f"{tool}: {len(calls)} calls, {len(failed)} failed ({_percent(len(failed), len(calls))})",
        "",
    ]
    lines.extend(_argument_keys(calls, schema_keys))
    lines.extend(_error_clusters(failed, examples))
    lines.extend(_after_failure(records, tool))
    lines.extend(_repeats(records, tool))
    lines.extend(_by_model(calls))
    if by_argument:
        lines.extend(_by_argument(calls, by_argument))
    partial = Counter(call.data_status for call in calls if call.ok and call.data_status)
    if partial:
        lines.append("Result data.status of successful calls:")
        lines.extend(f"  {status:<24} {count:>7}" for status, count in partial.most_common())
    return "\n".join(lines).rstrip()


def export(records: Iterable[CallRecord], path: Path) -> int:
    count = 0
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")
            count += 1
    return count


def _argument_keys(calls: list[CallRecord], schema_keys: frozenset[str] | None) -> list[str]:
    keys: Counter[str] = Counter()
    shapes: Counter[str] = Counter()
    for call in calls:
        arguments = call.arguments()
        if isinstance(arguments, dict):
            keys.update(arguments.keys())
        else:
            shapes[type(arguments).__name__] += 1
    lines = ["Argument keys (calls using each; ! = not in the current schema):"]
    for key, count in keys.most_common():
        marker = "!" if schema_keys is not None and key not in schema_keys else " "
        lines.append(f" {marker}{key:<28} {count:>7}")
    lines.extend(f"  arguments were {shape}: {count}" for shape, count in shapes.items())
    lines.append("")
    return lines


def _error_clusters(failed: list[CallRecord], examples: int) -> list[str]:
    if not failed:
        return []
    clusters: dict[tuple[str, str], list[CallRecord]] = defaultdict(list)
    for call in failed:
        clusters[(call.error_code or "failed", _normalized(call.error_message or ""))].append(call)
    lines = ["Failures by code and message shape:"]
    for (code, shape), members in sorted(clusters.items(), key=lambda item: -len(item[1]))[:15]:
        lines.append(f"  {len(members):>6}  {code}: {_clip(shape, 220)}")
        for member in members[:examples]:
            lines.append(f"          e.g. {_clip(_redact(member.arguments_json), 220)}")
            if member.error_message:
                lines.append(f"               -> {_clip(_redact(member.error_message), 220)}")
    lines.append("")
    return lines


def _after_failure(records: list[CallRecord], tool: str) -> list[str]:
    outcomes: Counter[str] = Counter()
    longest = 0
    for calls in _runs(records):
        streak = 0
        for index, call in enumerate(calls):
            if call.name != tool:
                continue
            streak = streak + 1 if call.failed else 0
            longest = max(longest, streak)
            if not call.failed:
                continue
            following = calls[index + 1] if index + 1 < len(calls) else None
            if following is None:
                outcomes["no further call in the Run"] += 1
            elif following.name != tool:
                outcomes[f"switched to {following.name}"] += 1
            elif following.failed:
                outcomes["same Tool failed again"] += 1
            else:
                outcomes["same Tool succeeded next"] += 1
    if not outcomes:
        return []
    lines = ["What followed a failure in the same Run:"]
    lines.extend(f"  {what:<36} {count:>7}" for what, count in outcomes.most_common(12))
    lines.append(f"  longest failure streak: {longest}")
    lines.append("")
    return lines


def _repeats(records: list[CallRecord], tool: str) -> list[str]:
    repeated = 0
    total = 0
    for calls in _runs(records):
        seen: set[str] = set()
        for call in calls:
            if call.name != tool:
                continue
            total += 1
            if call.arguments_json in seen:
                repeated += 1
            seen.add(call.arguments_json)
    if not repeated:
        return []
    return [
        f"Identical repeats within a Run: {repeated} of {total} calls "
        f"({_percent(repeated, total)})",
        "",
    ]


def _by_model(calls: list[CallRecord]) -> list[str]:
    models: dict[str, list[CallRecord]] = defaultdict(list)
    for call in calls:
        models[call.model or "(unknown)"].append(call)
    lines = ["By Model (top 12):"]
    for model, members in sorted(models.items(), key=lambda item: -len(item[1]))[:12]:
        failed = sum(1 for member in members if member.failed)
        lines.append(f"  {model:<48} {len(members):>6} {_percent(failed, len(members)):>6}")
    lines.append("")
    return lines


def _by_argument(calls: list[CallRecord], key: str) -> list[str]:
    words: dict[str, list[CallRecord]] = defaultdict(list)
    for call in calls:
        arguments = call.arguments()
        value = arguments.get(key) if isinstance(arguments, dict) else None
        word = value.strip().split()[0] if isinstance(value, str) and value.strip() else "(none)"
        words[word[:40]].append(call)
    lines = [f"First word of {key!r} (top 25):"]
    for word, members in sorted(words.items(), key=lambda item: -len(item[1]))[:25]:
        failed = sum(1 for member in members if member.failed)
        lines.append(f"  {word:<40} {len(members):>6} {_percent(failed, len(members)):>6}")
    lines.append("")
    return lines


def _runs(records: list[CallRecord]) -> Iterator[list[CallRecord]]:
    runs: dict[tuple[int, int | None], list[CallRecord]] = defaultdict(list)
    for record in records:
        runs[record.run_scope].append(record)
    for calls in runs.values():
        calls.sort(key=lambda call: (call.seq, call.ordinal))
        yield calls


def _normalized(message: str) -> str:
    text = _QUOTED.sub("'...'", message.splitlines()[0] if message else "")
    return _NUMBER.sub("N", _PATH.sub("PATH", text))


def _redact(text: str) -> str:
    return _SECRET.sub("[REDACTED]", text.replace("\r", "").replace("\n", "\\n"))


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else f"{text[:limit]}..."


def _percent(part: int, whole: int) -> str:
    return f"{100 * part / whole:.1f}%" if whole else "-"


def _quantile(values: list[int], q: float) -> int:
    if not values:
        return 0
    if len(values) == 1:
        return values[0]
    return int(statistics.quantiles(values, n=100, method="inclusive")[round(q * 100) - 1])
