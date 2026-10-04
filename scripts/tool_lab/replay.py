"""Replay real file-edit calls from Sessions through the current Tools.

``export`` simulates each Session's file history in a copy of a sessions
database (see ``_replay_history``) and writes every ``apply_patch`` call whose
files it could reconstruct to ``corpus.jsonl``: the call's arguments, the
content of each file it names before the call, which files the Session had
read, the recorded result, and an intended target when the history shows one.

* ``self``: a recorded success; its own result, flagged ``H1`` when the next
  edit of the file within a few calls removes an added line that repeats its
  neighbor, ``H2`` when that edit revises added lines, ``S`` when it broke the
  Python or JSON syntax of a file that parsed before.
* ``eventual``: a recorded failure or partial result followed later in the
  same Run by a successful ``apply_patch`` on its files; the content after
  that success, with the number of calls in between.

``run`` dispatches every case through the production Tool path on its files
in a fresh scratch directory and classifies it against the recording:

* ``same``: recorded and replayed success, and the result matches the target
* ``wrong``: replayed success whose files differ from the target
* ``regressed``: recorded success that now fails or applies only in part
* ``fixed``: recorded failure that now succeeds with the eventual target
* ``still_failing``: recorded failure that fails again
* ``unknown``: recorded failure that now succeeds, without a target to judge it

The corpus holds private Session content, so it and every output are written
only below ``--work``, which must lie outside the repository.
"""

from __future__ import annotations

import asyncio
import difflib
import json
import os
import re
import shutil
import time
import warnings
from collections import Counter, defaultdict, deque
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO

from core.runtime.runtime import Runtime
from core.tools._line_diff import line_opcodes
from core.tools.arguments import split_text_lines
from core.tools.tools import ToolCall, ToolExecutionConfig, ToolExecutor
from scripts.tool_lab._lab_runtime import DEFAULT_AGENT_ID, lab_runtime
from scripts.tool_lab._replay_history import (
    ROOT_TOKEN,
    Dispatched,
    HistoryCall,
    ReplayError,
    SessionReplay,
    SessionStats,
    session_histories,
)
from scripts.tool_lab.definitions import tool_definitions
from scripts.tool_lab.probe import _VBOT_ROOT, model_view
from scripts.tool_lab.sessions import Filters, _normalized

if TYPE_CHECKING:
    from _typeshed import HasFileno

CORPUS_NAME = "corpus.jsonl"
CLASSES = ("same", "fixed", "wrong", "regressed", "still_failing", "unknown")
# Dispatches in flight; file I/O in the scratch directory dominates, so overlap helps.
DEFAULT_JOBS = 8
_SHORT = {"still_failing": "still", "regressed": "regr", "unknown": "unkn"}
_FULL = frozenset({"applied", "unchanged"})
_REVIEWED = ("wrong", "regressed", "fixed", "unknown")
_FILTER_KEYS = (
    "model",
    "code",
    "status",
    "target",
    "flag",
    "verified",
    "since",
    "until",
    "session",
    "case",
)
_ROOT = re.compile(re.escape(ROOT_TOKEN) + r"(?=([\\/]?))")
_DIFF_LINES = 400


def work_directory(path: Path) -> Path:
    """Return ``path`` resolved, refusing a location inside the repository.

    Session content is private; it must never land where git could pick it up.
    """
    resolved = path.expanduser().resolve()
    checkouts = [_VBOT_ROOT]
    if _VBOT_ROOT.parent.name == ".worktrees":
        checkouts.append(_VBOT_ROOT.parent.parent)
    for checkout in checkouts:
        if resolved == checkout or resolved.is_relative_to(checkout):
            raise ReplayError(f"{path} lies inside the repository {checkout}; use a path outside")
    return resolved


# --- Dispatch ------------------------------------------------------------------------


class Dispatcher:
    """Run one call on given files in a fresh scratch directory, through the Tool path."""

    def __init__(self, runtime: Runtime, root: Path) -> None:
        _offered, definitions = tool_definitions(runtime, DEFAULT_AGENT_ID, include_all=True)
        self._names = tuple(definitions)
        self._contracts = runtime.tools.contracts_for_provider_definitions(
            list(definitions.values())
        )
        self._executor = ToolExecutor(runtime.tools)
        self._state = runtime.file_read_state
        self._root = root
        self._count = 0

    def offers(self, tool: str) -> bool:
        return tool in self._names

    async def __call__(
        self,
        arguments: Any,
        files: dict[str, str | None],
        stamped: set[str],
        *,
        tool: str = "apply_patch",
    ) -> Dispatched:
        """Dispatch ``arguments``; ``files`` maps scratch paths to content (None: absent).

        The call runs in a fresh Session that has read the ``stamped`` files.
        """
        self._count += 1
        number = self._count
        workspace = self._root / "replay" / str(number)
        session_id = f"replay-{number}"
        try:
            (workspace / "cwd").mkdir(parents=True)
            for location, content in files.items():
                if content is None:
                    continue
                target = workspace / location
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("w", encoding="utf-8", newline="") as handle:
                    handle.write(content)
                if location in stamped:
                    self._state.record_read(session_id, target.resolve())
        except (OSError, UnicodeEncodeError) as error:
            shutil.rmtree(workspace, ignore_errors=True)
            return Dispatched(False, "failed", "replay_setup", str(error), {})
        config = ToolExecutionConfig(
            agent_id=DEFAULT_AGENT_ID,
            session_id=session_id,
            run_id=f"replay-run-{number}",
            workspace=workspace,
            vbot_root=_VBOT_ROOT,
            data_root=self._root / "data",
            cwd=workspace / "cwd",
            allowed_tools=self._names,
            session_tool_grants=self._names,
            input_contracts=self._contracts,
        )
        call = ToolCall(
            id=f"replay-call-{number}", name=tool, arguments=_with_root(arguments, workspace)
        )
        try:
            [envelope] = await self._executor.execute_many([call], config)
            after = {location: _read(workspace / location) for location in files}
        finally:
            shutil.rmtree(workspace, ignore_errors=True)
        text = model_view(envelope)
        if not envelope.get("ok"):
            failure = envelope.get("error") or {}
            return Dispatched(False, "failed", failure.get("code"), text, after)
        data = envelope.get("data") or {}
        status = data.get("status") if isinstance(data, dict) else None
        return Dispatched(True, str(status or "applied"), None, text, after)


@asynccontextmanager
async def replay_dispatcher() -> AsyncIterator[Dispatcher]:
    """A Dispatcher on a throwaway Runtime without extensions or background services.

    Scratch files need no durability, so ``os.fsync`` does nothing meanwhile,
    and the syntax warnings that checking the replayed Python files raises
    stay quiet.
    """
    real_fsync = os.fsync
    os.fsync = _skip_fsync
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            async with lab_runtime(extensions=False) as (runtime, root):
                yield Dispatcher(runtime, root)
    finally:
        os.fsync = real_fsync


def _skip_fsync(fd: int | HasFileno) -> None:
    return None


def _with_root(value: Any, workspace: Path) -> Any:
    if isinstance(value, dict):
        return {key: _with_root(item, workspace) for key, item in value.items()}
    if isinstance(value, list):
        return [_with_root(item, workspace) for item in value]
    if isinstance(value, str) and ROOT_TOKEN in value:
        return _ROOT.sub(
            lambda match: str(workspace) if match[1] == "\\" else workspace.as_posix(), value
        )
    return value


def _read(path: Path) -> str | None:
    try:
        with path.open(encoding="utf-8", errors="replace", newline="") as handle:
            return handle.read()
    except FileNotFoundError, IsADirectoryError, NotADirectoryError, PermissionError:
        return None


async def _in_order[T, R](
    items: Iterable[T], work: Callable[[T], Awaitable[R]], jobs: int
) -> AsyncIterator[R]:
    """Run ``work`` on the items, at most ``jobs`` at a time; yield results in item order."""
    gate = asyncio.Semaphore(max(1, jobs))
    pending: deque[asyncio.Task[R]] = deque()

    async def bounded(item: T) -> R:
        try:
            return await work(item)
        finally:
            gate.release()

    try:
        for item in items:
            await gate.acquire()
            pending.append(asyncio.create_task(bounded(item)))
            while pending and pending[0].done():
                yield pending.popleft().result()
        while pending:
            yield await pending.popleft()
    finally:
        for task in pending:
            task.cancel()


# --- Export --------------------------------------------------------------------------


async def export_corpus(
    database: Path,
    corpus: Path,
    *,
    dispatch: Dispatcher,
    admit: Filters,
    jobs: int = DEFAULT_JOBS,
) -> tuple[SessionStats, int]:
    """Write the replayable apply_patch calls of ``database`` to ``corpus``.

    ``admit`` picks the calls that become cases; the history around them is
    simulated anyway. Returns the simulation counts and the number of Sessions.
    """

    async def simulate(calls: list[HistoryCall]) -> tuple[list[dict[str, Any]], SessionStats]:
        replay = SessionReplay(calls, dispatch, admit=admit.admit)
        return await replay.run(), replay.stats

    stats = SessionStats()
    sessions = 0
    corpus.parent.mkdir(parents=True, exist_ok=True)
    with corpus.open("w", encoding="utf-8", newline="\n") as out:
        async for records, session_stats in _in_order(session_histories(database), simulate, jobs):
            sessions += 1
            stats.merge(session_stats)
            for record in records:
                out.write(json.dumps(record, ensure_ascii=True) + "\n")
    return stats, sessions


def export_summary(stats: SessionStats, sessions: int) -> str:
    lines = [
        f"Sessions with apply_patch calls: {sessions}",
        f"apply_patch calls admitted: {stats.calls}",
        f"replayable cases: {stats.cases} ({_percent(stats.cases, stats.calls)})",
    ]
    for title, counts in (("target", stats.targets), ("verified by", stats.verified)):
        shown = ", ".join(f"{name} {count}" for name, count in counts.most_common())
        lines.append(f"  {title}: {shown or '-'}")
    lines.append("not replayable:")
    lines.extend(f"  {reason:<34} {count:>6}" for reason, count in stats.skipped.most_common())
    lines.append(f"contradictions by later evidence: {stats.contradictions}")
    lines.append(
        "recorded successes on known files that the current engine ends differently: "
        f"{stats.mismatches.total()}"
    )
    lines.extend(f"  {kind:<34} {count:>6}" for kind, count in stats.mismatches.most_common(12))
    return "\n".join(lines)


# --- Run -----------------------------------------------------------------------------


@dataclass(slots=True)
class CaseResult:
    case: dict[str, Any]
    outcome: Dispatched
    label: str
    detail: str | None = None
    diffs: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _Row:
    """What the summary needs of one case."""

    label: str
    detail: str | None
    model: str
    recorded: str
    recorded_full: bool
    notes: tuple[str, ...]
    target: str
    verified: str
    day: str


def read_corpus(path: Path) -> Iterator[dict[str, Any]]:
    try:
        handle = path.open(encoding="utf-8")
    except OSError as error:
        raise ReplayError(f"cannot read {path}: {error}") from error
    with handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                raise ReplayError(f"{path}:{number}: not a corpus line: {error}") from error
            if not isinstance(record, dict) or "replay_arguments" not in record:
                raise ReplayError(f"{path}:{number}: not a corpus case")
            yield record


def case_filter(specs: Iterable[str]) -> Callable[[dict[str, Any]], bool]:
    """A predicate from ``KEY=VALUE`` filters; every filter must hold."""
    tests: list[Callable[[dict[str, Any]], bool]] = []
    for spec in specs:
        key, separator, value = spec.partition("=")
        if not separator or key not in _FILTER_KEYS:
            raise ReplayError(f"filter {spec!r}: use KEY=VALUE with KEY one of {_FILTER_KEYS}")
        tests.append(_filter_test(key, value))
    return lambda case: all(test(case) for test in tests)


def _filter_test(key: str, value: str) -> Callable[[dict[str, Any]], bool]:
    def target(case: dict[str, Any]) -> dict[str, Any]:
        return case.get("target") or {}

    tests: dict[str, Callable[[dict[str, Any]], bool]] = {
        "model": lambda case: value.lower() in (case.get("model") or "").lower(),
        "code": lambda case: case["recorded"].get("error_code") == value,
        "status": lambda case: case["recorded"].get("status") == value,
        "target": lambda case: target(case).get("label", "none") == value,
        "flag": lambda case: value in target(case).get("flags", ()),
        "verified": lambda case: case.get("verified") == value,
        "since": lambda case: case.get("created_at", "") >= value,
        "until": lambda case: case.get("created_at", "") < value,
        "session": lambda case: case.get("session") == value,
        "case": lambda case: case.get("id") == value,
    }
    return tests[key]


async def replay_cases(
    cases: Iterable[dict[str, Any]],
    *,
    dispatch: Dispatcher,
    tool: str,
    jobs: int = DEFAULT_JOBS,
) -> AsyncIterator[CaseResult]:
    """Dispatch each case to ``tool`` on its files and classify the result, in case order."""
    if not dispatch.offers(tool):
        raise ReplayError(f"the {tool} Tool is not registered in this checkout")

    async def replay(case: dict[str, Any]) -> CaseResult:
        files = {entry["path"]: entry["content"] for entry in case["files"]}
        stamped = {entry["path"] for entry in case["files"] if entry["read"]}
        outcome = await dispatch(case["replay_arguments"], files, stamped, tool=tool)
        return classify(case, outcome)

    async for result in _in_order(cases, replay, jobs):
        yield result


def classify(case: dict[str, Any], outcome: Dispatched) -> CaseResult:
    recorded = case["recorded"]
    target = case.get("target") or {}
    replayed = outcome.ok and outcome.status in _FULL
    expected: dict[str, str | None] = target.get("files") or {}
    differing = [path for path, content in expected.items() if outcome.files.get(path) != content]
    result = CaseResult(case, outcome, "same")
    if recorded["status"] in _FULL:
        if not replayed:
            result.label = "regressed"
        elif target.get("label") == "self" and differing:
            result.label = "wrong"
    elif not replayed:
        result.label = "still_failing"
        if outcome.error_code != recorded.get("error_code"):
            result.detail = "other error" if outcome.error_code else "partial"
    elif target.get("label") != "eventual":
        result.label = "unknown"
    elif differing:
        result.label = "wrong"
        result.detail = _wrong_detail(case, outcome, expected, differing)
    else:
        result.label = "fixed"
    if replayed:
        for path in differing:
            result.diffs[path] = _diff(path, expected[path], outcome.files.get(path))
    return result


def _wrong_detail(
    case: dict[str, Any],
    outcome: Dispatched,
    expected: dict[str, str | None],
    differing: list[str],
) -> str:
    """Whether the replayed change is part of the eventual change or departs from it."""
    before = {entry["path"]: entry["content"] for entry in case["files"]}
    for path in differing:
        made = _changes(before.get(path), outcome.files.get(path))
        wanted = _changes(before.get(path), expected[path])
        if not made <= wanted:
            return "conflict"
    return "target_has_more"


def _changes(before: str | None, after: str | None) -> set[tuple[str, int | str]]:
    old = split_text_lines(before or "")
    new = split_text_lines(after or "")
    changes: set[tuple[str, int | str]] = set()
    for tag, i1, i2, j1, j2 in line_opcodes(old, new):
        if tag != "equal":
            changes.update(("-", index) for index in range(i1, i2))
            changes.update(("+", text) for text in new[j1:j2])
    return changes


def _diff(path: str, expected: str | None, actual: str | None) -> str:
    lines = difflib.unified_diff(
        (expected or "").splitlines(keepends=True),
        (actual or "").splitlines(keepends=True),
        fromfile=f"target/{path}" + (" (absent)" if expected is None else ""),
        tofile=f"replay/{path}" + (" (absent)" if actual is None else ""),
        n=2,
    )
    shown = [line if line.endswith("\n") else line + "\n" for line in lines]
    if len(shown) > _DIFF_LINES:
        shown = [*shown[:_DIFF_LINES], f"[{len(shown) - _DIFF_LINES} more diff lines]\n"]
    return "".join(shown)


class _Output:
    """Per-case details as JSON lines, and a readable file per class worth reviewing."""

    def __init__(self, out: Path) -> None:
        out.mkdir(parents=True, exist_ok=True)
        self._out = out
        review = out / "review"
        shutil.rmtree(review, ignore_errors=True)
        review.mkdir()
        self._review = review
        self._details = (out / "details.jsonl").open("w", encoding="utf-8", newline="\n")
        self._readable: dict[str, TextIO] = {}

    def add(self, result: CaseResult) -> None:
        case = result.case
        target = case.get("target") or {}
        entry = {
            "id": case["id"],
            "class": result.label,
            "detail": result.detail,
            "model": case.get("model"),
            "created_at": case.get("created_at"),
            "target": target.get("label"),
            "flags": target.get("flags", []),
            "verified": case.get("verified"),
            "recorded": {k: case["recorded"].get(k) for k in ("status", "error_code", "text")},
            "replay": {
                "status": result.outcome.status,
                "error_code": result.outcome.error_code,
                "text": result.outcome.text,
            },
            "diffs": result.diffs,
        }
        self._details.write(json.dumps(entry, ensure_ascii=True) + "\n")
        if result.label in _REVIEWED:
            if result.label not in self._readable:
                path = self._review / f"{result.label}.txt"
                self._readable[result.label] = path.open("w", encoding="utf-8")
            self._readable[result.label].write(_readable(result) + "\n")

    def close(self, summary: str) -> None:
        self._details.close()
        for handle in self._readable.values():
            handle.close()
        (self._out / "summary.txt").write_text(summary + "\n", encoding="utf-8")


def _readable(result: CaseResult) -> str:
    case = result.case
    recorded = case["recorded"]
    target = case.get("target") or {}
    detail = f" ({result.detail})" if result.detail else ""
    head = (
        f"=== {case['id']} {result.label}{detail} | {case.get('model')} | "
        f"{case.get('created_at')} | target {target.get('label')} "
        f"{','.join(target.get('flags', []))} | verified {case.get('verified')}"
    )
    parts = [
        head,
        "--- arguments",
        json.dumps(case["arguments"], ensure_ascii=False, indent=1),
        f"--- recorded: {recorded['status']} {recorded.get('error_code') or ''}",
        recorded.get("text", ""),
        f"--- replay: {result.outcome.status} {result.outcome.error_code or ''}",
        result.outcome.text,
    ]
    parts.extend(f"--- diff {path}\n{diff}" for path, diff in result.diffs.items())
    return "\n".join(parts) + "\n"


def _row(result: CaseResult) -> _Row:
    case = result.case
    recorded = case["recorded"]
    target = case.get("target")
    flags = (target or {}).get("flags") or []
    return _Row(
        label=result.label,
        detail=result.detail,
        model=case.get("model") or "-",
        recorded=str(recorded.get("error_code") or recorded.get("status")),
        recorded_full=recorded.get("status") in _FULL,
        notes=tuple(sorted({_note_class(note) for note in recorded.get("notes", ())})),
        target=(target["label"] + (f" {'+'.join(flags)}" if flags else "")) if target else "none",
        verified=case.get("verified") or "-",
        day=(case.get("created_at") or "-")[:10],
    )


def _note_class(note: str) -> str:
    """A note without the file text it quotes, numbers or paths."""
    return _normalized(note).split(": ", 1)[0].rstrip(":")[:100]


# --- Summary -------------------------------------------------------------------------


def run_summary(rows: list[_Row], *, tool: str, seconds: float) -> str:
    total = len(rows)
    counts = Counter(row.label for row in rows)
    agree = counts["same"] + counts["still_failing"]
    lines = [
        f"Replayed {total} cases with {tool} in {seconds:.1f} s",
        f"reproduced the recorded outcome (same + still_failing): {agree} "
        f"({_percent(agree, total)})",
        "",
    ]
    groups: list[tuple[str, Callable[[_Row], str | None]]] = [
        ("class", lambda row: row.label),
        ("recorded error code or status", lambda row: row.recorded),
        ("model", lambda row: row.model),
        ("target", lambda row: row.target),
        ("verified by", lambda row: row.verified),
        ("day", lambda row: row.day),
        ("detail", lambda row: f"{row.label}: {row.detail}" if row.detail else None),
    ]
    for title, key in groups:
        table = _table(title, rows, key)
        lines.extend([*table, ""] if table else [])
    lines.extend(_note_table(rows))
    return "\n".join(lines).rstrip()


def _table(title: str, rows: list[_Row], key: Callable[[_Row], str | None]) -> list[str]:
    groups: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        name = key(row)
        if name is not None:
            groups[name][row.label] += 1
    if not groups:
        return []
    width = max(len(title), *(len(name) for name in groups)) + 2
    header = f"{title:<{width}}{'total':>7}" + "".join(
        f"{_SHORT.get(label, label):>7}" for label in CLASSES
    )
    lines = [header, "-" * len(header)]
    if title == "day":
        ordered = sorted(groups.items())
    else:
        ordered = sorted(groups.items(), key=lambda item: (-item[1].total(), item[0]))
    for name, counts in ordered:
        cells = "".join(f"{counts[label] or '':>7}" for label in CLASSES)
        lines.append(f"{name:<{width}}{counts.total():>7}{cells}")
    return lines


def _note_table(rows: list[_Row]) -> list[str]:
    groups: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        if row.recorded_full:
            for note in row.notes:
                groups[note][row.label] += 1
    if not groups:
        return []
    lines = ["recorded successes with a Note, by note"]
    for note, counts in sorted(groups.items(), key=lambda item: -item[1].total()):
        cells = ", ".join(f"{label} {counts[label]}" for label in CLASSES if counts[label])
        lines.append(f"{counts.total():>6}  {note}  [{cells}]")
    return lines


def _percent(part: int, whole: int) -> str:
    return f"{100 * part / whole:.1f}%" if whole else "-"


async def run_and_write(
    corpus: Path,
    out: Path,
    *,
    tool: str,
    select: Callable[[dict[str, Any]], bool],
    limit: int,
    jobs: int = DEFAULT_JOBS,
) -> str:
    """Replay the selected cases of ``corpus``; write the outputs below ``out``."""
    cases: Iterable[dict[str, Any]] = (case for case in read_corpus(corpus) if select(case))
    if limit:
        cases = (case for _number, case in zip(range(limit), cases, strict=False))
    started = time.perf_counter()
    rows: list[_Row] = []
    async with replay_dispatcher() as dispatch:
        output = _Output(out)
        try:
            async for result in replay_cases(cases, dispatch=dispatch, tool=tool, jobs=jobs):
                output.add(result)
                rows.append(_row(result))
        finally:
            summary = run_summary(rows, tool=tool, seconds=time.perf_counter() - started)
            output.close(summary)
    return summary
