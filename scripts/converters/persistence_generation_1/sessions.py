"""Generation 1 conversion of the Session database ``sessions.db``.

Before Generation 1 a Session's history lived in ``messages``, in the result
columns of ``tool_calls``, in the terminal columns of ``runs``, in
``compaction_checkpoints`` and in ``history_edits``. An edit cleared the
``active`` flag of the tail it replaced, and a fork copied every row of its
source.

This area rebuilds every Session with the Generation 1 codec, so each entry
gets exactly the side rows the current store writes for its role:

- Every history record becomes one entry at its seq. An inactive record is
  superseded at the seq of the edit that replaced it, found by replaying the
  Session's edits; an inactive record no edit explains is superseded at its own
  seq, which hides it from every view, and is reported. Edit markers are
  superseded at their own seq.
- A Run keeps its status, timing, change statistics and changed paths; its
  terminal record becomes its ``run_summary`` entry. A Run still running in a
  live Session stays running, so the next start settles it as it settles a Run
  after a crash; one in an archived Session ends interrupted. Tool calls without
  a result end interrupted, or cancelled for a cancelled Run, once their Run
  has ended.
- A boundary checkpoint without a projection gets the projection the old reader
  derived: the summary note, then the messages from its tail boundary up to
  the checkpoint. A projected checkpoint drops the retired tail-guidance note
  older Compactions placed between the summary and the tail.
- A fork whose copied prefix still matches its source seq for seq (role,
  Message id and visibility), and whose copied Runs match the source's, shares
  that prefix through ``session_lineage``, exactly as a Generation 1 fork
  does, and its copies are dropped. Any other fork, such as one made while a
  Run was running, keeps its copies as history below its fork point, and the
  copied Runs become inherited.
- Session metadata is split into its columns and relations: prompt pins, seen
  Skills and the prompt-cache affinity move to their own storage, and the Run
  kinds move to ``session_run_kinds``. The derived ``fork_source`` and
  ``run_kinds`` keys are not stored, and the retired Channel routing keys are
  dropped. A value the store would refuse is dropped and reported.
- Continuations are dropped. Owner-managed bindings, group titles, delivery
  receipts and Run execution owners are kept; retired Tool names in a binding's
  ``tool_access`` are replaced as in every other Tool access policy
  (``_tool_access``).
- Three old Assistant Message shapes get their current form, in history and in
  stored checkpoint projections. A line-only output-file reference named a
  line that held just the path, and the server replaced that whole line with
  the file link; it gets the span of the whole line. One whose line is missing
  or empty, or that shares its line with another reference, is dropped and
  reported. A Usage whose only provenance was the whole-turn ``estimated``
  meant that both primary counters were estimated; it gets both field-level
  flags, and ``estimated`` stays their summary. A Usage without the
  ``context_usage`` snapshot every current Assistant step stores gets the
  Context the old reader derived from it: input plus output, each measured
  unless estimated. One without an input count to derive it from, where the
  old reader showed no Context, keeps no snapshot and is reported.

The source is opened read-only. Timestamps become canonical UTC, and every drop
or approximation is reported. The staged database is reopened once, so its
search indexes are built and verified. The old Session store's marker
``session-store.json``, its snapshot health and its ``session-snapshots`` are
retired: the database kernel replaced them.
"""
# ruff: noqa: E501

from __future__ import annotations

import bisect
import heapq
import json
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.chat._step_outcomes import tool_result_facts
from core.chat.errors import ChatError
from core.chat.messages import COMPACTION_SUMMARY_NOTE_PREFIX, ChatMessage
from core.database import open_offline_database
from core.sessions import _store_codec, _store_fts, _store_lineage, _store_prompts, _store_values
from core.sessions._metadata import _RUN_KIND_VALUES, _is_prompt_cache_affinity_id
from core.sessions._store_lineage import MAX_SEQ, ViewRange
from core.sessions._store_schema import session_database_spec
from core.sessions._types import SESSION_TERMINAL_RUN_STATUSES
from core.sessions.errors import SessionStoreCorruptError
from core.sessions.schema import APPLICATION_ID, FTS_STALE_KEY
from core.utils.timestamps import format_canonical_timestamp, utc_now_timestamp
from scripts.converters.persistence_generation_1._context import (
    ConversionContext,
    ConversionError,
)
from scripts.converters.persistence_generation_1._legacy_sqlite import (
    LEGACY_IDENTITY,
    Tally,
    identity,
    remove_staged,
    retire_sidecars,
    table_columns,
)
from scripts.converters.persistence_generation_1._tool_access import convert_policy

AREA = "sessions"
DATABASE = "sessions.db"

# Application ids the old table shape was written with, under any user
# version: none at first, then "VBOT" in released builds, and between the
# database kernel and Generation 1 already the current "VBSS". The table shape
# decides; the identity only rules out foreign databases.
_RELEASED_APPLICATION_ID = 0x56424F54
_SOURCE_APPLICATION_IDS = (LEGACY_IDENTITY[0], _RELEASED_APPLICATION_ID, APPLICATION_ID)
_RUN_LATEST_RELEASE = (
    "Start the latest vBot release before Generation 1 once so it updates this Session "
    "database, then convert again"
)
# The old Session store's marker, snapshot health and snapshots (full copies of
# sessions.db); the database kernel replaced all of them. Its maintenance guard
# means an old conversion never finished.
_LEGACY_STORE_FILES = ("session-store.json", "session-snapshot-health.json", "session-snapshots")
_LEGACY_MAINTENANCE_GUARD = "session-store-maintenance.json"
# Boundary checkpoints were written by the summary-and-tail strategy.
_LEGACY_CHECKPOINT_STRATEGY = "summary_tail"
# Projected checkpoints once placed this note between the summary and the tail.
_LEGACY_TAIL_GUIDANCE = (
    "The messages below are the most recent verbatim Session activity retained after this "
    "Compaction checkpoint. They chronologically follow the summary above."
)
# Channel routing moved out of Sessions; these metadata keys retired with it.
_RETIRED_METADATA_KEYS = ("active_session_id", "conversation_kind", "participants")
_RUNNING = "running"
_INTERRUPTED = "interrupted"
_CANCELLED = "cancelled"
_UNFINISHED_CALL_STATUSES = frozenset({"pending", "running"})
# Field-level Usage provenance; ``estimated`` is their whole-turn summary.
_USAGE_ESTIMATION_FIELDS = ("input_tokens_estimated", "output_tokens_estimated")
_VERIFY_BATCH = 500

# The pre-Generation-1 columns this area reads, frozen from the old schema.
_REQUIRED_COLUMNS: dict[str, tuple[str, ...]] = {
    "sessions": (
        "session_key", "generation_id", "project_id", "agent_id", "session_id", "status",
        "created_at", "last_message_at", "archived_at", "message_count", "last_message_id",
        "history_reset_sequence", "history_revision", "state_revision", "metadata_json",
        "title", "auto_title", "source_channel_id", "platform", "platform_conv_id",
        "is_subagent_session", "subagent_parent_json", "fork_source_json", "run_kinds_json",
        "compaction_policy_json", "activity_json",
    ),
    "messages": (
        "message_key", "session_key", "seq", "run_id", "message_id", "role", "timestamp",
        "content", "content_blocks_json", "model", "active",
    ),
    "assistant_messages": (
        "message_key", "reasoning", "reasoning_summary_json", "reasoning_meta_json",
        "reasoning_scope", "reasoning_started_at", "reasoning_completed_at",
        "reasoning_duration_ms", "reasoning_timing_extra_json", "phase", "input_tokens",
        "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens",
        "usage_estimated", "input_tokens_estimated", "output_tokens_estimated",
        "usage_present", "usage_extra_json", "tool_calls_present", "interrupted",
        "interruption_cause",
    ),
    "tool_calls": (
        "tool_call_key", "message_key", "ordinal", "tool_call_id", "name", "arguments_json",
        "rejection_code", "rejection_message", "rejection_fingerprint",
        "argument_sequence_index", "argument_sequence_length", "status", "result_id",
        "result_sequence", "result_timestamp", "result_active", "result_content",
        "started_at", "completed_at", "duration_ms", "timing_extra_json", "display_json",
    ),
    "assistant_output_files": (
        "message_key", "ordinal", "path", "line_index", "start_index", "end_index",
    ),
    "user_message_senders": ("message_key", "sender_id", "display_name", "role"),
    "error_messages": ("message_key", "error_kind"),
    "compaction_checkpoints": (
        "snapshot_key", "session_key", "seq", "run_id", "message_id", "timestamp", "content",
        "active", "tail_boundary_id", "projection_json", "policy", "strategy",
        "compacted_token_count", "context_tokens_before", "context_tokens_after",
        "compaction_duration_ms", "usage_present", "usage_extra_json",
    ),
    "runs": (
        "run_key", "session_key", "run_id", "work_id", "run_kind", "contributes_to_activity",
        "origin_generation_id", "status", "started_at", "completed_at", "duration_ms",
        "start_sequence", "terminal_sequence", "terminal_id", "terminal_active",
        "completion_reason", "timing_extra_json", "iteration_count", "changed_files",
        "lines_added", "lines_removed", "change_stats_extra_json",
    ),
    "run_change_paths": ("run_key", "ordinal", "path"),
    "history_edits": (
        "edit_key", "session_key", "seq", "message_id", "timestamp", "target_message_id",
    ),
    "temporary_session_bindings": (
        "session_key", "generation_id", "owner_name", "group_id", "participant_id",
        "config_json",
    ),
    "temporary_group_titles": ("owner_name", "group_id", "title"),
    "session_delivery_receipts": (
        "session_key", "generation_id", "owner_name", "receipt_id", "content_hash",
        "effect_kind", "carrier_kind", "carrier_sequence",
    ),
    "run_execution_owners": (
        "record_key", "session_key", "generation_id", "run_id", "input_id", "owner_name",
        "group_id", "participant_id", "participant_generation_id", "epoch",
    ),
}  # fmt: skip

_SESSIONS_SQL = (
    f"SELECT {', '.join(_REQUIRED_COLUMNS['sessions'])} FROM sessions ORDER BY session_key"
)

# Every history record of one Session except Run terminals, which are read
# with their Run. Each branch reads one old table; the joins add what the old
# reader decoded for that record.
_RECORDS_SQL = """
WITH records (kind, record_key, source_key, seq, message_id, role, timestamp, content,
              content_blocks_json, model, active, owner_run_id, call_key) AS (
  SELECT 'message', m.message_key, m.message_key, m.seq, m.message_id, m.role, m.timestamp,
         m.content, m.content_blocks_json, m.model, m.active, m.run_id, NULL
  FROM messages AS m WHERE m.session_key = :session
  UNION ALL
  SELECT 'result', t.tool_call_key, NULL, t.result_sequence, t.result_id, 'tool',
         t.result_timestamp, t.result_content, NULL, NULL, t.result_active, m.run_id,
         t.tool_call_key
  FROM tool_calls AS t JOIN messages AS m ON m.message_key = t.message_key
  WHERE m.session_key = :session AND t.result_id IS NOT NULL
  UNION ALL
  SELECT 'checkpoint', c.snapshot_key, NULL, c.seq, c.message_id, 'compaction_checkpoint',
         c.timestamp, c.content, NULL, NULL, c.active, c.run_id, NULL
  FROM compaction_checkpoints AS c WHERE c.session_key = :session
  UNION ALL
  SELECT 'edit', e.edit_key, NULL, e.seq, e.message_id, 'history_edit', e.timestamp,
         NULL, NULL, NULL, 0, NULL, NULL
  FROM history_edits AS e WHERE e.session_key = :session
)
SELECT h.*,
       a.reasoning, a.reasoning_meta_json, a.reasoning_summary_json, a.reasoning_scope,
       a.reasoning_started_at, a.reasoning_completed_at, a.reasoning_duration_ms,
       a.reasoning_timing_extra_json, a.phase, a.input_tokens, a.output_tokens,
       a.cache_read_tokens, a.cache_write_tokens, a.reasoning_tokens, a.usage_estimated,
       a.input_tokens_estimated, a.output_tokens_estimated, a.usage_present,
       a.usage_extra_json, a.tool_calls_present, a.interrupted, a.interruption_cause,
       t.message_key AS call_message_key, t.tool_call_id, t.name,
       t.started_at AS timing_started_at, t.completed_at AS timing_completed_at,
       t.duration_ms AS timing_duration_ms, t.timing_extra_json,
       t.display_json AS tool_display_json,
       u.sender_id, u.display_name AS sender_display_name, u.role AS sender_role,
       e.error_kind,
       c.tail_boundary_id, c.projection_json, c.policy AS compaction_policy,
       c.strategy AS compaction_strategy, c.compacted_token_count, c.context_tokens_before,
       c.context_tokens_after, c.compaction_duration_ms,
       c.usage_present AS compaction_usage_present,
       c.usage_extra_json AS compaction_usage_extra_json,
       x.target_message_id
FROM records AS h
LEFT JOIN assistant_messages AS a ON a.message_key = h.source_key
LEFT JOIN tool_calls AS t ON t.tool_call_key = h.call_key
LEFT JOIN user_message_senders AS u ON u.message_key = h.source_key
LEFT JOIN error_messages AS e ON e.message_key = h.source_key
LEFT JOIN compaction_checkpoints AS c ON h.kind = 'checkpoint' AND c.snapshot_key = h.record_key
LEFT JOIN history_edits AS x ON h.kind = 'edit' AND x.edit_key = h.record_key
ORDER BY h.seq
"""
_CALLS_SQL = """
SELECT t.tool_call_key, t.message_key, t.ordinal, t.tool_call_id, t.name, t.arguments_json,
       t.rejection_code, t.rejection_message, t.rejection_fingerprint,
       t.argument_sequence_index, t.argument_sequence_length, t.status, t.result_id,
       t.started_at, t.completed_at, t.duration_ms
FROM tool_calls AS t JOIN messages AS m ON m.message_key = t.message_key
WHERE m.session_key = ? ORDER BY t.message_key, t.ordinal
"""
_OUTPUT_FILES_SQL = """
SELECT f.message_key, f.path, f.line_index, f.start_index, f.end_index
FROM assistant_output_files AS f JOIN messages AS m ON m.message_key = f.message_key
WHERE m.session_key = ? ORDER BY f.message_key, f.ordinal
"""
_RUNS_SQL = f"SELECT {', '.join(_REQUIRED_COLUMNS['runs'])} FROM runs WHERE session_key = ? ORDER BY run_key"
_CHANGE_PATHS_SQL = """
SELECT p.run_key, p.path FROM run_change_paths AS p JOIN runs AS r ON r.run_key = p.run_key
WHERE r.session_key = ? ORDER BY p.run_key, p.ordinal
"""


def check_source(source: Path) -> None:
    """Refuse a Session store this area cannot convert, before anything is staged."""
    guard = source / _LEGACY_MAINTENANCE_GUARD
    if guard.exists():
        raise ConversionError(
            f"{guard} shows an unfinished maintenance of the old Session store. "
            f"{_RUN_LATEST_RELEASE}"
        )
    source_path = source / DATABASE
    if source_path.is_file():
        with _open_source(source_path) as connection:
            _is_current(source_path, connection)


def convert(context: ConversionContext) -> None:
    """Stage the Generation 1 ``sessions.db`` from the source Session database."""
    for relative in _LEGACY_STORE_FILES:
        if context.source_path(relative).exists():
            context.retire(relative)
            context.report.count(AREA, "legacy_store_files_retired")
    source_path = context.source_path(DATABASE)
    if not source_path.is_file():
        context.report.count(AREA, "source_missing")
        return
    with _open_source(source_path) as source:
        if _is_current(source_path, source):
            context.report.count(AREA, "already_current")
            return
        target_path = context.staged(DATABASE)
        remove_staged(target_path)
        database = open_offline_database(session_database_spec(target_path))
        try:
            tally = database.write(lambda connection: _Conversion(source, connection).run())
        finally:
            database.close()
    _build_search_index(target_path, tally)
    tally.publish(context, AREA)
    retire_sidecars(context, DATABASE)


def session_label(row: sqlite3.Row | Mapping[str, Any]) -> str:
    """The report item naming one Session generation, for its drops and approximations."""
    project = row["project_id"] or "-"
    return f"session {project}/{row['agent_id']}/{row['session_id']} ({row['generation_id']})"


# -- Source ----------------------------------------------------------------------------


@contextmanager
def _open_source(path: Path) -> Iterator[sqlite3.Connection]:
    """Open the source read-only without creating journal files beside it.

    A read-only connection to a WAL database still creates ``-wal`` and
    ``-shm`` files. Without a pending journal the database file is complete,
    so it is opened immutable; with one, SQLite must read the journal, and the
    source is opened plainly read-only.
    """
    journals = [Path(f"{path}{suffix}") for suffix in ("-wal", "-journal")]
    pending = any(journal.exists() and journal.stat().st_size > 0 for journal in journals)
    uri = f"{path.resolve().as_uri()}?mode=ro" + ("" if pending else "&immutable=1")
    try:
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as error:
        raise ConversionError(f"cannot open {path} read-only: {error}") from error
    try:
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
        except sqlite3.Error as error:
            raise ConversionError(f"cannot read {path}: {error}") from error
        yield connection
    finally:
        connection.close()


def _has_generation_1_shape(source: sqlite3.Connection) -> bool:
    return (
        table_columns(source, "messages") is None and table_columns(source, "entries") is not None
    )


def _is_current(path: Path, source: sqlite3.Connection) -> bool:
    """Whether the source is already Generation 1; refuse any shape this area cannot read."""
    found = identity(source)
    if found[0] == APPLICATION_ID and _has_generation_1_shape(source):
        return True
    _require_legacy_source(path, source, found)
    return False


def _require_legacy_source(path: Path, source: sqlite3.Connection, found: tuple[int, int]) -> None:
    """Refuse a foreign database; ask for an update when the old shape is incomplete."""
    if found[0] not in _SOURCE_APPLICATION_IDS or table_columns(source, "messages") is None:
        raise ConversionError(
            f"{path} is not a pre-Generation-1 sessions database "
            f"(application_id={found[0]}, user_version={found[1]})"
        )
    for table, columns in _REQUIRED_COLUMNS.items():
        present = table_columns(source, table)
        missing = list(columns) if present is None else [c for c in columns if c not in present]
        if missing:
            what = "is missing" if present is None else f"lacks {', '.join(missing)}"
            raise ConversionError(f"{path}: table {table} {what}. {_RUN_LATEST_RELEASE}")


# -- Values ----------------------------------------------------------------------------


def _normalize_timestamp(value: Any) -> tuple[str | None, str | None]:
    """Return ``(canonical, problem)``; a value without an offset is read as UTC."""
    if not isinstance(value, str) or not value:
        return None, "is missing"
    text = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None, f"{value!r} is not an ISO 8601 timestamp"
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return format_canonical_timestamp(parsed.replace(tzinfo=UTC)), f"{value!r} read as UTC"
    return format_canonical_timestamp(parsed), None


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _json_value(payload: Any) -> Any:
    if payload is None:
        return None
    try:
        return json.loads(str(payload))
    except (TypeError, ValueError):
        return None


def _json_object(payload: Any) -> dict[str, Any]:
    if payload is None:
        return {}
    value = json.loads(str(payload))
    if not isinstance(value, dict):
        raise ValueError("stored extras must be a JSON object")
    return value


def _milliseconds_between(started_at: str, completed_at: str) -> int:
    started = datetime.fromisoformat(started_at)
    completed = datetime.fromisoformat(completed_at)
    return max(0, round((completed - started).total_seconds() * 1000))


def _group(rows: Sequence[sqlite3.Row], key: str) -> dict[int, list[sqlite3.Row]]:
    grouped: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        grouped[int(row[key])].append(row)
    return grouped


# -- Plan values -----------------------------------------------------------------------


@dataclass
class _Record:
    """One history record of an old Session, decoded as the old reader did."""

    kind: str  # message, result, terminal, checkpoint or edit
    key: int  # the row key in its old table
    seq: int
    role: str
    entry_id: str
    active: bool
    run_id: str | None
    message: ChatMessage | None = None
    problem: str | None = None
    call_message_key: int | None = None
    target_id: str | None = None
    superseded: int | None = None
    # A boundary checkpoint still needs the projection the old reader derived.
    needs_projection: bool = False
    tail_boundary_id: str | None = None
    # Old Assistant shapes brought to their current form: counts and drops, reported
    # only once the record is written, so a fork's shared copies are not counted twice.
    reshaped: Counter[str] = field(default_factory=Counter)
    reshape_drops: list[str] = field(default_factory=list)

    def visible_at(self, seq: int) -> bool:
        """Whether the record was current when history reached ``seq``."""
        return self.superseded is None or self.superseded >= seq


@dataclass
class _Run:
    """One old Run with the values its Generation 1 row gets."""

    run_id: str
    work_id: str | None
    run_kind: str
    copied: bool
    contributes: bool
    status: str
    started_at: str
    start_seq: int
    completed_at: str | None
    duration_ms: int | None
    completion_reason: str | None
    iteration_count: int | None
    timing_extra_json: str | None
    changed_files: int | None
    lines_added: int | None
    lines_removed: int | None
    change_stats_extra_json: str | None
    change_paths: list[str]
    # The old row's status and completion reason: a fork's copy of a Run
    # differs from its source's only when the Run was running at the fork.
    legacy_state: tuple[str, str | None]
    summary: ChatMessage | None = None
    timing_started_at: str | None = None


@dataclass
class _Fork:
    """What an old fork's ``fork_source`` says about the fork."""

    source_key: int | None
    point: int | None
    forked_at: str


# One lineage segment: (from_seq, ancestor_key, upto_seq, as_of_seq).
_Segment = tuple[int, int, int, int]


@dataclass
class _Summary:
    """What a converted fork source keeps for the forks that follow it."""

    key: int
    rows: dict[int, tuple[str, str, int | None]]  # seq -> (role, id, superseded_at_seq)
    runs: dict[str, tuple[str, str | None]]  # Run id -> its old status and completion reason
    segments: list[_Segment]
    truncations: list[tuple[int, int]]  # (edit seq, target seq)

    def view(self, as_of: int) -> tuple[ViewRange, ...]:
        """The Session's view when its history had reached ``as_of``."""
        cuts = [target for edit, target in self.truncations if edit < as_of]
        segments = _truncate(self.segments, min(cuts)) if cuts else self.segments
        return _ranges(self.key, segments)


def _truncate(segments: Sequence[_Segment], target: int) -> list[_Segment]:
    """Lineage after an edit at ``target``, as ``_store_lineage.truncate`` leaves it."""
    return [
        (start, ancestor, min(upto, target), as_of)
        for start, ancestor, upto, as_of in segments
        if start < target
    ]


def _ranges(key: int, segments: Sequence[_Segment]) -> tuple[ViewRange, ...]:
    """The view of lineage ``segments``, as ``_store_lineage.view_ranges`` reads it."""
    ranges: list[ViewRange] = []
    cursor = 0
    for start, ancestor, upto, as_of in sorted(segments):
        if start > cursor:
            ranges.append(ViewRange(key, cursor, start, MAX_SEQ))
        ranges.append(ViewRange(ancestor, start, upto, as_of))
        cursor = upto
    ranges.append(ViewRange(key, cursor, MAX_SEQ, MAX_SEQ))
    return tuple(ranges)


@dataclass
class _Issues:
    """Drops and approximations of one conversion, merged per item and reason."""

    counts: dict[tuple[str, str], int] = field(default_factory=dict)

    def add(self, item: str, reason: str) -> None:
        self.counts[(item, reason)] = self.counts.get((item, reason), 0) + 1

    def publish(self, tally: Tally) -> None:
        for (item, reason), count in self.counts.items():
            tally.skip(item, reason if count == 1 else f"{reason} ({count} times)")


# -- Conversion ------------------------------------------------------------------------


class _Conversion:
    """Convert every old Session into the staged database in one transaction."""

    def __init__(self, source: sqlite3.Connection, target: sqlite3.Connection) -> None:
        self.source = source
        self.target = target
        self.tally = Tally()
        self.issues = _Issues()
        self.sessions: dict[int, sqlite3.Row] = {}
        self.forks: dict[int, _Fork] = {}
        self.fork_sources: set[int] = set()
        self.summaries: dict[int, _Summary] = {}
        self.run_keys: dict[tuple[int, str], int] = {}

    def run(self) -> Tally:
        # Nothing maintains the search indexes while rows are written; they are
        # rebuilt when the staged database is opened again.
        _store_fts._drop_fts(self.target)
        _store_fts._set_fts_meta(self.target, FTS_STALE_KEY, "rebuilding")
        for name in ("sessions", "entries", "runs", "forks_shared", "forks_self_contained"):
            self.tally.count(name, 0)
        rows = self.source.execute(_SESSIONS_SQL).fetchall()
        self.sessions = {int(row["session_key"]): row for row in rows}
        self._plan_forks()
        continuations = table_columns(self.source, "continuations") is not None
        for key in self._write_order():
            _SessionConversion(self, self.sessions[key]).run(continuations=continuations)
        self._convert_owned_tables()
        self._verify()
        self.issues.publish(self.tally)
        return self.tally

    def label(self, row: sqlite3.Row) -> str:
        return session_label(row)

    # -- Forks ---------------------------------------------------------------------------

    def _plan_forks(self) -> None:
        """Resolve every fork's source generation before any Session is written."""
        generations: dict[tuple[str, str, str], list[sqlite3.Row]] = defaultdict(list)
        for row in self.sessions.values():
            generations[(row["project_id"] or "", row["agent_id"], row["session_id"])].append(row)
        for key, row in self.sessions.items():
            source = _json_value(row["fork_source_json"])
            if not isinstance(source, dict):
                continue
            label = self.label(row)
            forked_at, problem = _normalize_timestamp(source.get("forked_at"))
            if forked_at is None:
                forked_at = _normalize_timestamp(row["created_at"])[0] or utc_now_timestamp()
                self.issues.add(label, f"fork time {problem}; the Session creation time is used")
            elif problem is not None:
                self.issues.add(label, f"fork time {problem}")
            point = source.get("message_count")
            if not (isinstance(point, int) and _is_count(point)) or point > int(
                row["message_count"]
            ):
                self.issues.add(label, "fork point is invalid; the fork keeps its copied history")
                point = None
            address = (
                source.get("project_id") or "",
                str(source.get("agent_id")),
                str(source.get("session_id")),
            )
            # The source is the newest generation that was live at the address
            # when the fork was made. Imported Sessions do not number their
            # keys in creation order, so the key cannot decide.
            candidates = [
                (
                    (_normalize_timestamp(candidate["created_at"])[0] or ""),
                    int(candidate["session_key"]),
                )
                for candidate in generations.get(address, ())
                if int(candidate["session_key"]) != key
                and (_normalize_timestamp(candidate["created_at"])[0] or "") <= forked_at
                and (
                    candidate["archived_at"] is None
                    or (_normalize_timestamp(candidate["archived_at"])[0] or forked_at) >= forked_at
                )
            ]
            source_key = max(candidates)[1] if candidates else None
            if source_key is None:
                self.issues.add(
                    label,
                    "fork source no longer exists; the fork keeps its copied history "
                    "without a fork source",
                )
            elif point is not None:
                self.fork_sources.add(source_key)
            self.forks[key] = _Fork(source_key, point, forked_at)

    def _write_order(self) -> list[int]:
        """Session keys in key order, except that each fork follows its source."""
        forks_of: dict[int, list[int]] = defaultdict(list)
        waiting: set[int] = set()
        for key, fork in self.forks.items():
            if fork.source_key is not None:
                forks_of[fork.source_key].append(key)
                waiting.add(key)
        ready = [key for key in self.sessions if key not in waiting]
        heapq.heapify(ready)
        order: list[int] = []
        while True:
            while ready:
                key = heapq.heappop(ready)
                order.append(key)
                for fork_key in forks_of.pop(key, ()):
                    waiting.discard(fork_key)
                    heapq.heappush(ready, fork_key)
            if not waiting:
                return order
            # Forks that name each other as sources: the first one loses its source.
            key = min(waiting)
            waiting.discard(key)
            fork = self.forks[key]
            forks_of[fork.source_key or 0].remove(key)
            self.forks[key] = replace(fork, source_key=None)
            self.issues.add(
                self.label(self.sessions[key]),
                "fork sources form a cycle; the fork keeps its copied history "
                "without a fork source",
            )
            heapq.heappush(ready, key)

    # -- Owned tables --------------------------------------------------------------------

    def _convert_owned_tables(self) -> None:
        for key in (
            "temporary_session_bindings",
            "temporary_group_titles",
            "session_delivery_receipts",
            "run_execution_owners",
        ):
            self.tally.count(key, 0)
        for row in self.source.execute(
            "SELECT session_key, generation_id, owner_name, group_id, participant_id, "
            "config_json FROM temporary_session_bindings ORDER BY session_key"
        ):
            item = f"temporary Session binding {row['owner_name']}/{row['group_id']}/{row['participant_id']}"
            session = self.sessions.get(int(row["session_key"]))
            config = _json_value(row["config_json"])
            if session is None or session["generation_id"] != row["generation_id"]:
                self.issues.add(item, "dropped: its Session generation no longer exists")
                continue
            if not isinstance(config, dict):
                self.issues.add(item, "dropped: its configuration is not a JSON object")
                continue
            changes: list[str] = []
            if "tool_access" in config:
                config["tool_access"], changes = convert_policy(config["tool_access"])
            inserted = self._insert_owned(
                item,
                "temporary_session_bindings",
                ("session_key", "owner_name", "group_id", "participant_id", "config_json"),
                (
                    row["session_key"],
                    row["owner_name"],
                    row["group_id"],
                    row["participant_id"],
                    _store_values._json_object(config, "temporary Session config"),
                ),
            )
            if inserted and changes:
                self.tally.count("retired_tool_names_converted")
                self.issues.add(item, f"tool_access: {'; '.join(changes)}")
        for row in self.source.execute(
            "SELECT owner_name, group_id, title FROM temporary_group_titles "
            "ORDER BY owner_name, group_id"
        ):
            self._insert_owned(
                f"temporary group title {row['owner_name']}/{row['group_id']}",
                "temporary_group_titles",
                ("owner_name", "group_id", "title"),
                (row["owner_name"], row["group_id"], row["title"]),
            )
        receipt_columns = (
            "session_key",
            "owner_name",
            "receipt_id",
            "content_hash",
            "effect_kind",
            "carrier_kind",
            "carrier_sequence",
        )
        for row in self.source.execute(
            f"SELECT generation_id, {', '.join(receipt_columns)} FROM session_delivery_receipts "
            "ORDER BY session_key, owner_name, receipt_id"
        ):
            item = f"delivery receipt {row['owner_name']}/{row['receipt_id']}"
            session = self.sessions.get(int(row["session_key"]))
            if session is None or session["generation_id"] != row["generation_id"]:
                self.issues.add(item, "dropped: its Session generation no longer exists")
                continue
            self._insert_owned(
                item,
                "session_delivery_receipts",
                receipt_columns,
                tuple(row[column] for column in receipt_columns),
            )
        owner_columns = (
            "record_key",
            "session_key",
            "run_id",
            "input_id",
            "owner_name",
            "group_id",
            "participant_id",
            "participant_generation_id",
            "epoch",
        )
        # Admission order is the record key order; it is kept.
        for row in self.source.execute(
            f"SELECT generation_id, {', '.join(owner_columns)} FROM run_execution_owners "
            "ORDER BY record_key"
        ):
            item = (
                f"Run execution owner {row['owner_name']}/{row['group_id']} of Run {row['run_id']}"
            )
            session = self.sessions.get(int(row["session_key"]))
            run_key = self.run_keys.get((int(row["session_key"]), str(row["run_id"])))
            if session is None or session["generation_id"] != row["generation_id"]:
                self.issues.add(item, "dropped: its Session generation no longer exists")
                continue
            if run_key is None:
                self.issues.add(item, "dropped: its Run does not exist")
                continue
            self._insert_owned(
                item,
                "run_execution_owners",
                ("run_key", *owner_columns),
                (run_key, *(row[column] for column in owner_columns)),
            )

    def _insert_owned(
        self, item: str, table: str, columns: Sequence[str], values: Sequence[Any]
    ) -> bool:
        """Insert one owned row; return whether it was kept."""
        self.target.execute("SAVEPOINT convert_owned")
        try:
            self.target.execute(
                f"INSERT INTO {table} ({', '.join(columns)}) "
                f"VALUES ({', '.join('?' for _ in columns)})",
                tuple(values),
            )
        except sqlite3.IntegrityError as error:
            self.target.execute("ROLLBACK TO convert_owned")
            self.issues.add(item, f"dropped: {error}")
            return False
        else:
            self.tally.count(table)
            return True
        finally:
            self.target.execute("RELEASE convert_owned")

    # -- Verification --------------------------------------------------------------------

    def _verify(self) -> None:
        """Read every converted entry back through the store's decoder."""
        keys = [int(row[0]) for row in self.target.execute("SELECT entry_key FROM entries")]
        for start in range(0, len(keys), _VERIFY_BATCH):
            batch = _store_codec.select_entries(
                self.target,
                "e.entry_key IN (SELECT value FROM json_each(?))",
                (_store_values._key_list(keys[start : start + _VERIFY_BATCH]),),
            )
            for row in batch.rows:
                try:
                    batch.message(row)
                except (ChatError, SessionStoreCorruptError) as error:
                    raise ConversionError(
                        f"converted entry {row['entry_id']} of Session key "
                        f"{row['session_key']} cannot be read back: {error}"
                    ) from error
        problems = self.target.execute("PRAGMA foreign_key_check").fetchall()
        if problems:
            raise ConversionError(
                f"converted Session rows break references: {[tuple(row) for row in problems[:5]]}"
            )


class _SessionConversion:
    """Convert one old Session generation."""

    def __init__(self, conversion: _Conversion, row: sqlite3.Row) -> None:
        self.conversion = conversion
        self.source = conversion.source
        self.target = conversion.target
        self.tally = conversion.tally
        self.row = row
        self.key = int(row["session_key"])
        self.generation_id = str(row["generation_id"])
        self.label = conversion.label(row)
        self.archived = row["status"] == "archived"
        self.created_at = self._time(row["created_at"], "creation time", utc_now_timestamp())
        self.last_activity = (
            self.created_at
            if row["last_message_at"] is None
            else self._time(row["last_message_at"], "last activity time", self.created_at)
        )
        self.archived_at = (
            None
            if row["archived_at"] is None
            else self._time(row["archived_at"], "archive time", self.last_activity)
        )
        self.records: list[_Record] = []
        self.runs: dict[str, _Run] = {}
        self.calls: dict[int, list[sqlite3.Row]] = {}
        self.call_keys: dict[int, int] = {}  # old tool_call_key -> new call_key
        self.truncations: list[tuple[int, int]] = []

    def issue(self, reason: str) -> None:
        self.conversion.issues.add(self.label, reason)

    def _time(self, value: Any, what: str, fallback: str) -> str:
        canonical, problem = _normalize_timestamp(value)
        if canonical is None:
            self.issue(f"{what} {problem}; the nearest known time is used")
            return fallback
        if problem is not None:
            self.issue(f"{what} {problem}")
        return canonical

    # -- Plan ----------------------------------------------------------------------------

    def run(self, *, continuations: bool) -> None:
        fork = self.conversion.forks.get(self.key)
        self._load()
        self._supersede()
        segments = None if fork is None else self._shared_segments(fork)
        shared = segments is not None
        point = 0 if fork is None or fork.point is None else fork.point
        if fork is not None:
            self.tally.count("forks_shared" if shared else "forks_self_contained")
        own = [record for record in self.records if not shared or record.seq >= point]
        runs = [run for run in self.runs.values() if not (shared and run.copied)]
        if shared:
            self.tally.count("fork_copies_dropped", len(self.records) - len(own))
            self.tally.count("fork_runs_dropped", len(self.runs) - len(runs))
        self._materialize_checkpoints(own)
        self._write_session(fork)
        run_keys = self._write_runs(runs)
        self._write_completion(run_keys)
        written = self._write_records(own, run_keys)
        self._settle_calls()
        current = list(segments or ())
        for _edit, target in self._own_truncations(point if shared else 0):
            current = _truncate(current, target)
        self.target.executemany(
            "INSERT INTO session_lineage (session_key, from_seq, ancestor_key, upto_seq, "
            "as_of_seq) VALUES (?, ?, ?, ?, ?)",
            [(self.key, *segment) for segment in current],
        )
        if self.key in self.conversion.fork_sources:
            self.conversion.summaries[self.key] = _Summary(
                self.key,
                {
                    record.seq: (record.role, record.entry_id, record.superseded)
                    for record in own
                    if id(record) in written
                },
                {run.run_id: run.legacy_state for run in self.runs.values()},
                list(segments or ()),
                self._own_truncations(point if shared else 0),
            )
        if continuations:
            self._count_continuations()

    def _load(self) -> None:
        self.calls = _group(self.source.execute(_CALLS_SQL, (self.key,)).fetchall(), "message_key")
        files = _group(
            self.source.execute(_OUTPUT_FILES_SQL, (self.key,)).fetchall(), "message_key"
        )
        paths = _group(self.source.execute(_CHANGE_PATHS_SQL, (self.key,)).fetchall(), "run_key")
        records: list[_Record] = []
        previous = self.created_at
        for row in self.source.execute(_RECORDS_SQL, {"session": self.key}):
            record = _Record(
                kind=str(row["kind"]),
                key=int(row["record_key"]),
                seq=int(row["seq"]),
                role=str(row["role"]),
                entry_id=str(row["message_id"]),
                active=bool(row["active"]),
                run_id=row["owner_run_id"],
                call_message_key=row["call_message_key"],
                target_id=row["target_message_id"],
            )
            try:
                data = _legacy_message_data(row, self.calls, files)
                previous = self._normalize_message_times(record, data, previous)
                if record.kind == "checkpoint":
                    self._prepare_checkpoint(record, data)
                _current_assistant_shape(record, f"{record.role} {record.entry_id}", data)
                record.message = ChatMessage.from_dict(data)
            except (ChatError, KeyError, TypeError, ValueError) as error:
                record.problem = str(error) or type(error).__name__
            records.append(record)
        for row in self.source.execute(_RUNS_SQL, (self.key,)):
            run = self._plan_run(row, paths.get(int(row["run_key"]), []))
            self.runs[run.run_id] = run
            if row["terminal_sequence"] is None:
                continue
            record = _Record(
                kind="terminal",
                key=int(row["run_key"]),
                seq=int(row["terminal_sequence"]),
                role="run_summary",
                entry_id=str(row["terminal_id"]),
                active=bool(row["terminal_active"]),
                run_id=run.run_id,
                message=run.summary,
            )
            if run.summary is None:
                record.problem = "the Run has no valid completion"
            records.append(record)
        records.sort(key=lambda record: record.seq)
        taken: set[int] = set()
        for record in records:
            if record.seq < 0 or record.seq in taken:
                why = "negative seq" if record.seq < 0 else f"seq {record.seq} is taken"
                self.issue(f"{record.role} {record.entry_id} dropped: {why}")
                continue
            taken.add(record.seq)
            self.records.append(record)

    def _normalize_message_times(self, record: _Record, data: dict[str, Any], previous: str) -> str:
        """Make the record's timestamps canonical; return its timestamp."""
        what = f"{record.role} {record.entry_id}"
        timestamp = self._time(data.get("timestamp"), f"{what} time", previous)
        data["timestamp"] = timestamp
        for name in ("reasoning_timing", "timing"):
            timing = data.get(name)
            if not isinstance(timing, dict):
                continue
            problem = None
            for part in ("started_at", "completed_at"):
                canonical, part_problem = _normalize_timestamp(timing.get(part))
                if canonical is None:
                    problem = f"{part} {part_problem}"
                    break
                if part_problem is not None:
                    self.issue(f"{what} {name} {part} {part_problem}")
                timing[part] = canonical
            if problem is None and not _is_count(timing.get("duration_ms")):
                problem = "duration is missing"
            if problem is not None:
                self.issue(f"{what} {name} dropped: {problem}")
                del data[name]
        return timestamp

    def _prepare_checkpoint(self, record: _Record, data: dict[str, Any]) -> None:
        """Give a checkpoint the projection, policy and strategy every checkpoint has."""
        policy = data.pop("compaction_policy", None)
        strategy = data.pop("compaction_strategy", None)
        record.tail_boundary_id = data.pop("tail_boundary_id", None)
        projection = data.get("projection")
        if projection is None:
            # Materialized once every record is known; see _materialize_checkpoints.
            record.needs_projection = True
            data["projection"] = []
        else:
            if isinstance(projection, list):
                data["projection"] = self._without_tail_guidance(projection)
                for entry in data["projection"]:
                    if isinstance(entry, dict):
                        _current_assistant_shape(
                            record,
                            f"compaction checkpoint {record.entry_id} projection "
                            f"{entry.get('role')} {entry.get('id')}",
                            entry,
                        )
            if not policy or not strategy:
                self.issue(f"compaction checkpoint {record.entry_id} policy or strategy filled in")
        data["compaction_policy"] = policy or strategy or _LEGACY_CHECKPOINT_STRATEGY
        data["compaction_strategy"] = strategy or policy or _LEGACY_CHECKPOINT_STRATEGY

    def _without_tail_guidance(self, projection: list[Any]) -> list[Any]:
        """Drop the retired tail-guidance note; the current reader knows no such note."""
        kept = [
            entry
            for entry in projection
            if not (
                isinstance(entry, dict)
                and entry.get("role") == "note"
                and entry.get("content") == _LEGACY_TAIL_GUIDANCE
            )
        ]
        if len(kept) != len(projection):
            self.tally.count("tail_guidance_notes_dropped", len(projection) - len(kept))
        return kept

    def _plan_run(self, row: sqlite3.Row, paths: list[sqlite3.Row]) -> _Run:
        run_id = str(row["run_id"])
        what = f"Run {run_id}"
        origin = row["origin_generation_id"]
        copied = origin is not None and origin != self.generation_id
        status = str(row["status"])
        completion_reason = row["completion_reason"]
        started_at = self._time(row["started_at"], f"{what} start", self.created_at)
        completed_at: str | None = None
        if status != _RUNNING and status not in SESSION_TERMINAL_RUN_STATUSES:
            self.issue(f"{what} status {status!r} is unknown; the Run ends interrupted")
            status = _INTERRUPTED
        if status == _RUNNING and (copied or self.archived):
            # Restart recovery settles running Runs of live Sessions only.
            where = "copied into a fork" if copied else "in an archived Session"
            self.issue(f"{what} was still running {where}; it ends interrupted")
            status = _INTERRUPTED
            completion_reason = completion_reason or "process_restart"
            completed_at = self.archived_at or self.last_activity
        elif status != _RUNNING:
            completed_at = self._time(row["completed_at"], f"{what} completion", started_at)
        duration = row["duration_ms"] if _is_count(row["duration_ms"]) else None
        iteration_count = row["iteration_count"]
        if iteration_count is not None and not _is_count(iteration_count):
            self.issue(f"{what} iteration count {iteration_count!r} dropped")
            iteration_count = None
        timing_extra = _json_value(row["timing_extra_json"])
        if row["timing_extra_json"] is not None and not isinstance(timing_extra, dict):
            self.issue(f"{what} timing extras dropped: not a JSON object")
            timing_extra = None
        change_extra = _json_value(row["change_stats_extra_json"])
        if row["change_stats_extra_json"] is not None and not isinstance(change_extra, dict):
            self.issue(f"{what} change statistics extras dropped: not a JSON object")
            change_extra = None
        counts = (row["changed_files"], row["lines_added"], row["lines_removed"])
        change_paths = [str(path["path"]) for path in paths]
        if counts[0] is not None and not all(_is_count(value) for value in counts):
            self.issue(f"{what} change statistics dropped: the counts are invalid")
            counts = (None, None, None)
        if counts[0] is None:
            change_extra = None
            if change_paths:
                self.issue(f"{what} changed paths dropped: the Run has no change statistics")
            change_paths = []
        terminal = row["terminal_sequence"]
        run = _Run(
            run_id=run_id,
            work_id=row["work_id"],
            run_kind=str(row["run_kind"]),
            copied=copied,
            contributes=bool(row["contributes_to_activity"]) and not copied,
            status=status,
            started_at=started_at,
            start_seq=max(0, int(row["start_sequence"])),
            completed_at=completed_at,
            duration_ms=duration,
            completion_reason=completion_reason,
            iteration_count=iteration_count,
            timing_extra_json=(
                None
                if timing_extra is None
                else _store_values._json_object(timing_extra, "Run timing")
            ),
            changed_files=counts[0],
            lines_added=counts[1],
            lines_removed=counts[2],
            change_stats_extra_json=(
                None
                if change_extra is None
                else _store_values._json_object(change_extra, "Run change statistics")
            ),
            change_paths=change_paths,
            legacy_state=(str(row["status"]), row["completion_reason"]),
        )
        if terminal is not None:
            if completed_at is None:
                self.issue(f"{what} terminal record dropped: the Run is still running")
            else:
                self._plan_summary(row, run, timing_extra, change_extra)
        return run

    def _plan_summary(
        self,
        row: sqlite3.Row,
        run: _Run,
        timing_extra: dict[str, Any] | None,
        change_extra: dict[str, Any] | None,
    ) -> None:
        """Build the summary entry a settled Run's terminal record showed."""
        assert run.completed_at is not None
        if run.duration_ms is None:
            run.duration_ms = _milliseconds_between(run.started_at, run.completed_at)
            self.issue(f"Run {run.run_id} duration derived from its start and completion")
        timing = {
            **(timing_extra or {}),
            "started_at": run.started_at,
            "completed_at": run.completed_at,
            "duration_ms": run.duration_ms,
        }
        data: dict[str, Any] = {
            "id": str(row["terminal_id"]),
            "role": "run_summary",
            "timestamp": run.completed_at,
            "run_id": run.run_id,
            "status": run.status,
            "timing": timing,
        }
        if run.work_id is not None:
            data["work_id"] = run.work_id
        if run.iteration_count is not None:
            data["iteration_count"] = run.iteration_count
        if run.changed_files is not None:
            data["change_stats"] = {
                **(change_extra or {}),
                "files": run.changed_files,
                "added": run.lines_added,
                "removed": run.lines_removed,
                "paths": list(run.change_paths),
            }
        try:
            run.summary = ChatMessage.from_dict(data)
        except ChatError as error:
            self.issue(f"Run {run.run_id} summary dropped: {error}")
            return
        _timing, extra, _present = _store_codec._timing_fields(timing)
        run.timing_started_at = run.started_at
        run.timing_extra_json = extra

    def _supersede(self) -> None:
        """Replay the edits: each supersedes the tail from its target at its own seq."""
        seqs = [record.seq for record in self.records]
        current: dict[int, bool] = {}
        users: dict[str, list[int]] = defaultdict(list)
        for index, record in enumerate(self.records):
            if record.kind != "edit":
                current[record.seq] = True
                if record.role == "user":
                    users[record.entry_id].append(record.seq)
                continue
            record.superseded = record.seq
            # The old store edited the earliest active user Message with the id.
            target = next(
                (seq for seq in users.get(record.target_id or "", ()) if current.get(seq)), None
            )
            if target is None:
                continue
            self.truncations.append((record.seq, target))
            for replaced in self.records[bisect.bisect_left(seqs, target) : index]:
                if current.get(replaced.seq):
                    current[replaced.seq] = False
                    replaced.superseded = record.seq
        for record in self.records:
            if record.kind == "edit":
                continue
            if record.active and record.superseded is not None:
                self.issue("a record the edits replaced was still current; it stays current")
                record.superseded = None
            elif not record.active and record.superseded is None:
                self.issue("an inactive record no edit explains is hidden at its own seq")
                record.superseded = record.seq
        self.tally.count(
            "superseded_entries",
            sum(1 for record in self.records if record.superseded is not None),
        )

    def _own_truncations(self, point: int) -> list[tuple[int, int]]:
        return [(edit, target) for edit, target in self.truncations if edit >= point]

    def _shared_segments(self, fork: _Fork) -> list[_Segment] | None:
        """Return the fork's lineage when its copies match its source, else ``None``."""
        point = fork.point
        source = None if fork.source_key is None else self.conversion.summaries.get(fork.source_key)
        if point is None or source is None:
            return None
        segments: list[_Segment] = [
            (
                view_range.from_seq,
                view_range.source_key,
                view_range.upto_seq,
                point if view_range.source_key == source.key else view_range.as_of_seq,
            )
            for view_range in _store_lineage.clip(source.view(point), 0, point)
        ]
        reason = self._share_blocker(point, source) or self._prefix_mismatch(point, segments)
        if reason is not None:
            self.issue(f"fork keeps its copied history: {reason}")
            return None
        return segments

    def _share_blocker(self, point: int, source: _Summary) -> str | None:
        for run in self.runs.values():
            if run.copied and source.runs.get(run.run_id) != run.legacy_state:
                # The old fork settled the copy of a running Run as interrupted.
                if run.legacy_state == (_INTERRUPTED, "fork_snapshot"):
                    return f"Run {run.run_id} was running when the Session was forked"
                return f"copied Run {run.run_id} does not match its source's Run"
        copied_runs = {run.run_id for run in self.runs.values() if run.copied}
        copied_messages = {
            record.key for record in self.records if record.kind == "message" and record.seq < point
        }
        for record in self.records:
            if record.seq < point:
                continue
            if record.run_id in copied_runs:
                return f"{record.role} {record.entry_id} belongs to a copied Run"
            if record.kind == "result" and record.call_message_key in copied_messages:
                return f"Tool result {record.entry_id} answers a copied Tool call"
        return None

    def _prefix_mismatch(self, point: int, segments: list[_Segment]) -> str | None:
        """Compare the fork's copies with what the lineage shows, then and now."""
        by_seq = {record.seq: record for record in self.records if record.seq < point}
        current = segments
        for _edit, target in self._own_truncations(point):
            current = _truncate(current, target)
        for seq in range(point):
            copy = by_seq.get(seq)
            usable = copy is not None and copy.message is not None
            shared = self._resolve(segments, seq)
            if (usable and copy is not None and copy.visible_at(point)) != (shared is not None):
                return f"seq {seq} differs in visibility"
            if copy is not None and shared is not None and (copy.role, copy.entry_id) != shared:
                return f"seq {seq} holds another Message"
            copy_current = usable and copy is not None and copy.superseded is None
            if copy_current != (self._resolve(current, seq) is not None):
                return f"seq {seq} differs after the fork's own edits"
        return None

    def _resolve(self, segments: Sequence[_Segment], seq: int) -> tuple[str, str] | None:
        """The (role, id) a lineage shows at ``seq``, or ``None`` when it shows nothing."""
        for start, ancestor, upto, as_of in segments:
            if start <= seq < upto:
                summary = self.conversion.summaries.get(ancestor)
                found = None if summary is None else summary.rows.get(seq)
                if found is None or not (found[2] is None or found[2] >= as_of):
                    return None
                return found[0], found[1]
        return None

    def _materialize_checkpoints(self, records: Sequence[_Record]) -> None:
        """Give each boundary checkpoint the projection the old reader derived."""
        for record in records:
            if not record.needs_projection or record.message is None:
                continue
            try:
                converted = replace(record.message, projection=self._legacy_projection(record))
                converted.validate()
            except ChatError as error:
                record.message = None
                record.problem = f"its projection cannot be rebuilt: {error}"
                continue
            record.message = converted
            self.tally.count("checkpoints_materialized")

    def _legacy_projection(self, record: _Record) -> list[dict[str, Any]]:
        """The summary note plus the messages from the tail boundary to the checkpoint."""
        checkpoint = record.message
        assert checkpoint is not None
        summary = checkpoint.content if isinstance(checkpoint.content, str) else ""
        note = ChatMessage.note(
            f"{COMPACTION_SUMMARY_NOTE_PREFIX}{summary}",
            timestamp=datetime.fromisoformat(checkpoint.timestamp),
        )
        visible = [
            other.message
            for other in self.records
            if other.seq < record.seq
            and other.kind != "edit"
            and other.message is not None
            and other.visible_at(record.seq)
        ]
        boundary = next(
            (
                index
                for index, message in enumerate(visible)
                if message.id == record.tail_boundary_id
            ),
            len(visible),
        )
        tail = [
            message for message in visible[boundary:] if message.role != "compaction_checkpoint"
        ]
        return [note.to_dict(), *(message.to_dict() for message in tail)]

    # -- Write ---------------------------------------------------------------------------

    def _write_session(self, fork: _Fork | None) -> None:
        row = self.row
        metadata = _SessionMetadata(self).split()
        records_end = max((record.seq + 1 for record in self.records), default=0)
        next_seq = max(0, int(row["message_count"]))
        if records_end > next_seq:
            self.issue(f"next seq {next_seq} raised to {records_end} past its last record")
            next_seq = records_end
        columns = {
            "session_key": self.key,
            "generation_id": self.generation_id,
            "project_id": row["project_id"] or "",
            "agent_id": row["agent_id"],
            "session_id": row["session_id"],
            "state": "archived" if self.archived else "live",
            "created_at": self.created_at,
            "archived_at": self.archived_at,
            "next_seq": next_seq,
            "history_revision": max(0, int(row["history_revision"])),
            "state_revision": max(0, int(row["state_revision"])),
            "cursor_floor_seq": min(max(0, int(row["history_reset_sequence"])), next_seq),
            "last_activity_at": self.last_activity,
            "last_entry_id": row["last_message_id"],
            # A fork is written after its source (see _write_order).
            "fork_parent_key": None if fork is None else fork.source_key,
            "forked_at": None if fork is None else fork.forked_at,
            "fork_point_seq": None if fork is None else (fork.point or 0),
            **dict(
                zip(_store_values._METADATA_WRITE_COLUMNS, metadata.storage.columns, strict=True)
            ),
            "prompt_cache_affinity_id": metadata.affinity,
            "seen_skills_initialized": int(metadata.seen_skills is not None),
        }
        self.target.execute(
            f"INSERT INTO sessions ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)})",
            tuple(columns.values()),
        )
        self.tally.count("sessions")
        self.tally.count(f"sessions_{columns['state']}")
        self.target.executemany(
            "INSERT INTO session_run_kinds (session_key, run_kind) VALUES (?, ?)",
            [(self.key, kind) for kind in metadata.run_kinds],
        )
        if metadata.seen_skills is not None:
            self.target.executemany(
                "INSERT INTO session_seen_skills (session_key, skill_name) VALUES (?, ?)",
                [(self.key, name) for name in metadata.seen_skills],
            )
            self.tally.count("seen_skill_sets")
        _store_prompts.replace_pins(self.target, self.key, metadata.pins)
        self.tally.count("prompt_pins", len(metadata.pins))
        if metadata.affinity is not None:
            self.tally.count("prompt_cache_affinities")

    def _write_completion(self, run_keys: Mapping[str, int]) -> None:
        """Point the latest completion and its read mark at Runs stored in this Session."""
        latest, read_run_id = self._activity()
        latest_key = read_key = None
        if latest is not None:
            latest_key = run_keys.get(latest[0])
            if latest_key is None:
                self.issue(f"latest completion dropped: Run {latest[0]} is not in this Session")
                latest = None
        if read_run_id is not None:
            read_key = run_keys.get(read_run_id)
            if read_key is None:
                self.issue(f"read completion dropped: Run {read_run_id} is not in this Session")
        if latest is None and read_key is None:
            return
        self.target.execute(
            "UPDATE sessions SET latest_completion_run_key = ?, latest_completion_status = ?, "
            "latest_completion_at = ?, read_completion_run_key = ? WHERE session_key = ?",
            (
                latest_key,
                None if latest is None else latest[1],
                None if latest is None else latest[2],
                read_key,
                self.key,
            ),
        )

    def _activity(self) -> tuple[tuple[str, str, str] | None, str | None]:
        activity = _json_value(self.row["activity_json"])
        if not isinstance(activity, dict):
            self.issue("completion activity dropped: not a JSON object")
            return None, None
        latest = activity.get("latest_completion")
        completion = None
        if isinstance(latest, dict):
            run_id, status = latest.get("run_id"), latest.get("status")
            timestamp, problem = _normalize_timestamp(latest.get("timestamp"))
            if (
                isinstance(run_id, str)
                and run_id
                and status in SESSION_TERMINAL_RUN_STATUSES
                and timestamp is not None
            ):
                completion = (run_id, str(status), timestamp)
                if problem is not None:
                    self.issue(f"latest completion time {problem}")
            else:
                self.issue("latest completion dropped: it is incomplete")
        elif latest is not None:
            self.issue("latest completion dropped: not a JSON object")
        read_run_id = activity.get("read_run_id")
        if read_run_id is not None and not isinstance(read_run_id, str):
            self.issue("read completion dropped: not a Run id")
            read_run_id = None
        unknown = sorted(set(activity) - {"latest_completion", "read_run_id"})
        if unknown:
            self.issue(f"completion activity keys dropped: {', '.join(unknown)}")
        return completion, read_run_id

    def _write_runs(self, runs: Sequence[_Run]) -> dict[str, int]:
        run_keys: dict[str, int] = {}
        for run in runs:
            cursor = self.target.execute(
                "INSERT INTO runs (session_key, run_id, work_id, run_kind, "
                "contributes_to_activity, inherited, status, started_at, start_seq, "
                "completed_at, duration_ms, timing_started_at, completion_reason, "
                "iteration_count, changed_files, lines_added, lines_removed, "
                "timing_extra_json, change_stats_extra_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    self.key,
                    run.run_id,
                    run.work_id,
                    run.run_kind,
                    int(run.contributes),
                    int(run.copied),
                    run.status,
                    run.started_at,
                    run.start_seq,
                    run.completed_at,
                    run.duration_ms,
                    run.timing_started_at,
                    run.completion_reason,
                    run.iteration_count,
                    run.changed_files,
                    run.lines_added,
                    run.lines_removed,
                    run.timing_extra_json,
                    run.change_stats_extra_json,
                ),
            )
            run_key = int(cursor.lastrowid or 0)
            run_keys[run.run_id] = run_key
            self.target.executemany(
                "INSERT INTO run_change_paths (run_key, ordinal, path) VALUES (?, ?, ?)",
                [(run_key, ordinal, path) for ordinal, path in enumerate(run.change_paths)],
            )
            self.tally.count("runs")
            if run.copied:
                self.tally.count("runs_inherited")
            else:
                self.conversion.run_keys[(self.key, run.run_id)] = run_key
            if run.status == _RUNNING:
                self.tally.count("runs_running")
        return run_keys

    def _write_records(
        self, records: Sequence[_Record], run_keys: Mapping[str, int]
    ) -> dict[int, int]:
        """Write the records in seq order; return entry keys by ``id(record)``."""
        written: dict[int, int] = {}
        message_ids: dict[int, str] = {}
        for record in records:
            if record.message is None:
                self.issue(f"{record.role} {record.entry_id} dropped: {record.problem}")
                continue
            run_key = None if record.run_id is None else run_keys.get(record.run_id)
            if record.run_id is not None and run_key is None and record.kind != "terminal":
                self.issue(
                    f"{record.role} {record.entry_id} loses Run {record.run_id}: no such Run"
                )
            self.target.execute("SAVEPOINT convert_entry")
            try:
                entry_key = self._write_record(record, run_key, message_ids)
            except (sqlite3.IntegrityError, ChatError) as error:
                self.target.execute("ROLLBACK TO convert_entry")
                self.target.execute("RELEASE convert_entry")
                self.issue(f"{record.role} {record.entry_id} dropped: {error}")
                continue
            self.target.execute("RELEASE convert_entry")
            written[id(record)] = entry_key
            if record.kind == "message":
                message_ids[record.key] = record.entry_id
            self.tally.count("entries")
            for name, count in record.reshaped.items():
                self.tally.count(name, count)
            for reason in record.reshape_drops:
                self.issue(reason)
        return written

    def _write_record(
        self, record: _Record, run_key: int | None, message_ids: Mapping[int, str]
    ) -> int:
        message = record.message
        assert message is not None
        message.validate()
        call_message = None
        if record.kind == "result":
            call_message = message_ids.get(record.call_message_key or -1)
            if call_message is None:
                raise ChatError("its Tool call was not converted")
        if record.kind == "terminal" and run_key is None:
            raise ChatError("its Run was not converted")
        entry_key = _store_codec.insert_entry(
            self.target,
            self.key,
            record.seq,
            message,
            run_key=run_key,
            superseded_at_seq=record.superseded,
        )
        match record.kind:
            case "result":
                _store_codec.link_tool_result(
                    self.target,
                    self.key,
                    entry_key,
                    message,
                    run_key=None,
                    assistant_message_id=call_message,
                    facts=tool_result_facts([message]).get(str(message.tool_call_id)),
                )
                self.tally.count("tool_results")
            case "terminal":
                self.target.execute(
                    "UPDATE runs SET end_entry_key = ? WHERE run_key = ?", (entry_key, run_key)
                )
            case "checkpoint":
                self.tally.count("checkpoints")
            case "edit":
                self.tally.count("history_edits")
            case "message" if record.role == "assistant":
                ordinals = {
                    int(row[1]): int(row[0])
                    for row in self.target.execute(
                        "SELECT call_key, ordinal FROM tool_calls WHERE entry_key = ?",
                        (entry_key,),
                    )
                }
                for call in self.calls.get(record.key, ()):
                    call_key = ordinals.get(int(call["ordinal"]))
                    if call_key is not None:
                        self.call_keys[int(call["tool_call_key"])] = call_key
                self.tally.count("tool_calls", len(ordinals))
        return entry_key

    def _settle_calls(self) -> None:
        """Give each Tool call without a result the status and times it ended with."""
        linked = {
            int(row[0])
            for row in self.target.execute(
                "SELECT c.call_key FROM tool_calls AS c JOIN entries AS e "
                "ON e.entry_key = c.entry_key WHERE e.session_key = ? "
                "AND c.result_entry_key IS NOT NULL",
                (self.key,),
            )
        }
        run_of_message = {
            record.key: self.runs.get(record.run_id or "")
            for record in self.records
            if record.kind == "message"
        }
        settled = 0
        for message_key, calls in self.calls.items():
            run = run_of_message.get(message_key)
            for call in calls:
                call_key = self.call_keys.get(int(call["tool_call_key"]))
                if call_key is None or call_key in linked:
                    continue
                status = str(call["status"])
                completed = call["completed_at"]
                run_open = run is not None and run.status == _RUNNING
                if call["result_id"] is not None or (
                    status in _UNFINISHED_CALL_STATUSES and not run_open
                ):
                    # The call's result was lost, or its Run ended without one.
                    status = (
                        _CANCELLED if run is not None and run.status == _CANCELLED else _INTERRUPTED
                    )
                    completed = completed or (None if run is None else run.completed_at)
                    settled += 1
                completed_at = _normalize_timestamp(completed)[0]
                if completed_at is None and status not in _UNFINISHED_CALL_STATUSES:
                    completed_at = None if run is None else run.completed_at
                self.target.execute(
                    "UPDATE tool_calls SET status = ?, started_at = ?, completed_at = ?, "
                    "duration_ms = ? WHERE call_key = ?",
                    (
                        status,
                        _normalize_timestamp(call["started_at"])[0],
                        completed_at,
                        call["duration_ms"] if _is_count(call["duration_ms"]) else None,
                        call_key,
                    ),
                )
        if settled:
            self.tally.count("tool_calls_settled", settled)
            self.issue(f"{settled} Tool calls without a result end interrupted or cancelled")

    def _count_continuations(self) -> None:
        row = self.source.execute(
            "SELECT COUNT(*) FROM continuations WHERE session_key = ?", (self.key,)
        ).fetchone()
        if row[0]:
            self.tally.count("continuations_dropped", int(row[0]))
            self.issue("Continuation dropped: Generation 1 does not resume old Continuations")


@dataclass
class _Metadata:
    storage: _store_values._MetadataStorage
    run_kinds: list[str]
    seen_skills: list[str] | None
    pins: dict[str, dict[str, Any]]
    affinity: str | None


class _SessionMetadata:
    """Split one old metadata facade into Generation 1 columns and relations."""

    _TEXT_KEYS = ("title", "auto_title", "source_channel_id", "platform", "platform_conv_id")
    _BOOL_KEYS = ("auto_title_initialized", "is_subagent_session")
    _PARENT_FIELDS = frozenset(key for key, _column in _store_values._SUBAGENT_PARENT_FIELDS)

    def __init__(self, session: _SessionConversion) -> None:
        self.session = session
        self.row = session.row

    def drop(self, key: str, reason: str) -> None:
        self.session.issue(f"metadata {key} dropped: {reason}")

    def facade(self) -> dict[str, Any]:
        """The metadata the old store returned: the open object updated by its columns."""
        row = self.row
        metadata = _json_value(row["metadata_json"])
        if not isinstance(metadata, dict):
            self.drop("object", "not a JSON object")
            metadata = {}
        for key in self._TEXT_KEYS:
            if row[key] is not None:
                metadata[key] = str(row[key])
        if row["is_subagent_session"] is not None:
            metadata["is_subagent_session"] = bool(row["is_subagent_session"])
        for key, column in (
            ("subagent_parent", "subagent_parent_json"),
            ("fork_source", "fork_source_json"),
            ("run_kinds", "run_kinds_json"),
            ("compaction_policy", "compaction_policy_json"),
        ):
            if row[column] is not None:
                metadata[key] = _json_value(row[column])
        return metadata

    def split(self) -> _Metadata:
        metadata = self.facade()
        # The fork columns replace the derived fork source.
        metadata.pop("fork_source", None)
        run_kinds = self._run_kinds(metadata.pop("run_kinds", None))
        retired = [key for key in _RETIRED_METADATA_KEYS if key in metadata]
        for key in retired:
            del metadata[key]
        if retired:
            self.session.tally.count("retired_metadata_keys", len(retired))
        seen_skills = self._seen_skills(metadata.pop("seen_skills", None))
        affinity = metadata.pop("prompt_cache_affinity_id", None)
        if affinity is not None and not _is_prompt_cache_affinity_id(affinity):
            self.drop("prompt_cache_affinity_id", "not a prompt-cache affinity id")
            affinity = None
        pins: dict[str, dict[str, Any]] = {}
        for key in [
            key for key in metadata if key.startswith(_store_values._PROMPT_PIN_KEY_PREFIX)
        ]:
            value = metadata.pop(key)
            if isinstance(value, dict):
                pins[key] = value
            elif value is not None:
                self.drop(key, "a prompt pin must be a JSON object")
        for key in self._TEXT_KEYS:
            if key in metadata and not isinstance(metadata[key], str | None):
                self.drop(key, "not a string")
                del metadata[key]
        for key in self._BOOL_KEYS:
            if key in metadata and not isinstance(metadata[key], bool | None):
                self.drop(key, "not a boolean")
                del metadata[key]
        if not isinstance(metadata.get("compaction_policy"), dict | None):
            self.drop("compaction_policy", "not a JSON object")
            del metadata["compaction_policy"]
        self._subagent_parent(metadata)
        derived = {"run_kinds": run_kinds} if run_kinds else {}
        try:
            storage = _store_values._session_metadata_storage(metadata, derived)
        except ChatError as error:
            self.drop("object", str(error))
            storage = _store_values._session_metadata_storage({}, derived)
        return _Metadata(storage, run_kinds, seen_skills, pins, affinity)

    def _run_kinds(self, value: Any) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list):
            self.drop("run_kinds", "not a list")
            return []
        known = [kind for kind in value if isinstance(kind, str) and kind in _RUN_KIND_VALUES]
        if len(known) != len(value):
            unknown = [kind for kind in value if kind not in known]
            self.drop("run_kinds entries", f"unknown Run kinds {unknown!r}")
        return sorted(set(known))

    def _seen_skills(self, value: Any) -> list[str] | None:
        if value is None:
            return None
        if not isinstance(value, list):
            self.drop("seen_skills", "not a list")
            return None
        names = [name for name in value if isinstance(name, str) and name]
        if len(names) != len(value):
            self.drop("seen_skills entries", "Skill names must be non-empty strings")
        return sorted(set(names))

    def _subagent_parent(self, metadata: dict[str, Any]) -> None:
        parent = metadata.get("subagent_parent")
        if parent is None:
            metadata.pop("subagent_parent", None)
            return
        if not isinstance(parent, dict):
            self.drop("subagent_parent", "not a JSON object")
            del metadata["subagent_parent"]
            return
        kept: dict[str, Any] = {}
        for key, value in parent.items():
            valid = key in self._PARENT_FIELDS and (
                _is_count(value) if key == "tool_call_index" else isinstance(value, str)
            )
            if valid:
                kept[key] = value
            elif value is not None:
                self.drop(f"subagent_parent.{key}", "not a supported Sub-Agent parent field")
        if kept:
            metadata["subagent_parent"] = kept
        else:
            del metadata["subagent_parent"]


# -- Search index ----------------------------------------------------------------------


def _build_search_index(path: Path, tally: Tally) -> None:
    """Reopen the staged database so it builds and verifies its search indexes."""
    database = open_offline_database(session_database_spec(path))
    try:
        with database.read() as connection:
            health = _store_fts._fts_health_from_connection(connection, verify_coverage=True)
    finally:
        database.close()
    if health.available:
        tally.count("search_index_healthy")
    else:
        tally.skip(
            "search index",
            f"not verified ({health.state}: {health.reason}); vBot rebuilds it when it opens",
        )


# -- The old reader --------------------------------------------------------------------


def _current_assistant_shape(record: _Record, what: str, data: dict[str, Any]) -> None:
    """Give old Assistant Message data the Usage and file spans it meant."""
    if data.get("role") != "assistant":
        return
    usage = data.get("usage")
    if isinstance(usage, dict):
        if _with_field_provenance(usage):
            record.reshaped["usage_provenance_derived"] += 1
        snapshot = usage.get("context_usage")
        if not (isinstance(snapshot, dict) and _is_count(snapshot.get("tokens"))):
            derived = _old_reader_context(usage)
            if derived is None:
                record.reshape_drops.append(
                    f"{what} Usage keeps no Context snapshot: it has no input count to derive one"
                )
            else:
                usage["context_usage"] = derived
                record.reshaped["context_snapshots_derived"] += 1
    references = data.get("output_files")
    if not isinstance(references, list):
        return
    content = data.get("content")
    lines = content.splitlines() if isinstance(content, str) else []
    per_line = Counter(
        reference.get("line_index") for reference in references if isinstance(reference, dict)
    )
    kept: list[Any] = []
    for reference in references:
        if (
            not isinstance(reference, dict)
            or reference.get("start_index") is not None
            or reference.get("end_index") is not None
        ):
            kept.append(reference)
            continue
        line_index = reference.get("line_index")
        line = (
            lines[line_index]
            if isinstance(line_index, int)
            and not isinstance(line_index, bool)
            and 0 <= line_index < len(lines)
            else ""
        )
        if not line:
            why = "names a missing or empty line"
        elif per_line[line_index] > 1:
            why = "shares its line with another reference"
        else:
            kept.append({**reference, "start_index": 0, "end_index": len(line)})
            record.reshaped["output_file_spans_derived"] += 1
            continue
        record.reshape_drops.append(
            f"{what} line-only file reference {reference.get('path')} dropped: it {why}"
        )
    if kept:
        data["output_files"] = kept
    else:
        del data["output_files"]


def _with_field_provenance(usage: dict[str, Any]) -> bool:
    """Give a whole-turn-only ``estimated`` the field-level provenance it meant.

    Without either field-level flag the old reader applied ``estimated`` to both
    primary counters. ``estimated`` is then recomputed as the summary of the
    field-level flags, as current writers keep it. Return whether the Usage had
    the old shape.
    """
    legacy = usage.get("estimated") is True and not any(
        field in usage for field in _USAGE_ESTIMATION_FIELDS
    )
    if legacy:
        for field in _USAGE_ESTIMATION_FIELDS:
            usage[field] = True
    if any(usage.get(field) is True for field in _USAGE_ESTIMATION_FIELDS):
        usage["estimated"] = True
    else:
        usage.pop("estimated", None)
    return legacy


def _old_reader_context(usage: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return the Context the old reader projected for a Usage without a snapshot.

    It projected input plus output, each Provider-measured unless its
    field-level flag says estimated, and any output made the projection an
    estimate; Messages after the Usage are added at read time, as for every
    snapshot. Without an input count it showed no Context. Needs the
    field-level provenance in place.
    """
    input_tokens: Any = usage.get("input_tokens")
    if not _is_count(input_tokens):
        return None
    output_tokens: Any = usage.get("output_tokens")
    if not _is_count(output_tokens):
        output_tokens = 0
    input_estimated = usage.get("input_tokens_estimated") is True
    output_estimated = usage.get("output_tokens_estimated") is True
    context: dict[str, Any] = {
        "tokens": input_tokens + output_tokens,
        "estimated": input_estimated or output_estimated or bool(output_tokens),
    }
    if not input_estimated:
        context["provider_input_tokens"] = input_tokens
    if not output_estimated:
        context["provider_output_tokens"] = output_tokens
    return context


def _legacy_message_data(
    row: sqlite3.Row,
    calls: Mapping[int, Sequence[sqlite3.Row]],
    files: Mapping[int, Sequence[sqlite3.Row]],
) -> dict[str, Any]:
    """Rebuild one record's Message data as the old store's reader did."""
    data: dict[str, Any] = {
        "id": str(row["message_id"]),
        "role": str(row["role"]),
        "timestamp": row["timestamp"],
    }
    if row["content_blocks_json"] is not None:
        data["content"] = json.loads(str(row["content_blocks_json"]))
    elif row["content"] is not None:
        data["content"] = str(row["content"])
    for name, column in (
        ("model", "model"),
        ("reasoning", "reasoning"),
        ("reasoning_scope", "reasoning_scope"),
        ("phase", "phase"),
        ("tool_call_id", "tool_call_id"),
        ("name", "name"),
        ("error_kind", "error_kind"),
        ("tail_boundary_id", "tail_boundary_id"),
        ("compaction_policy", "compaction_policy"),
        ("compaction_strategy", "compaction_strategy"),
        ("run_id", "owner_run_id"),
        ("target_message_id", "target_message_id"),
        ("interruption_cause", "interruption_cause"),
    ):
        if row[column] is not None:
            data[name] = row[column]
    for column, name in (
        ("reasoning_meta_json", "reasoning_meta"),
        ("reasoning_summary_json", "reasoning_summary"),
        ("tool_display_json", "tool_display"),
        ("projection_json", "projection"),
    ):
        if row[column] is not None:
            data[name] = json.loads(str(row[column]))
    if row["reasoning_started_at"] is not None or row["reasoning_timing_extra_json"] is not None:
        timing = _json_object(row["reasoning_timing_extra_json"])
        for name, column in (
            ("started_at", "reasoning_started_at"),
            ("completed_at", "reasoning_completed_at"),
            ("duration_ms", "reasoning_duration_ms"),
        ):
            if row[column] is not None:
                timing[name] = row[column]
        data["reasoning_timing"] = timing
    if row["usage_present"] or row["compaction_usage_present"]:
        usage = _json_object(
            row["usage_extra_json"] if row["usage_present"] else row["compaction_usage_extra_json"]
        )
        for column in (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "reasoning_tokens",
            "compacted_token_count",
            "context_tokens_before",
            "context_tokens_after",
            "compaction_duration_ms",
        ):
            if row[column] is not None:
                usage[column] = row[column]
        for name, column in (
            ("estimated", "usage_estimated"),
            ("input_tokens_estimated", "input_tokens_estimated"),
            ("output_tokens_estimated", "output_tokens_estimated"),
        ):
            if row[column] is not None:
                usage[name] = bool(row[column])
        data["usage"] = usage
    if row["timing_started_at"] is not None:
        timing = _json_object(row["timing_extra_json"])
        for name, column in (
            ("started_at", "timing_started_at"),
            ("completed_at", "timing_completed_at"),
            ("duration_ms", "timing_duration_ms"),
        ):
            if row[column] is not None:
                timing[name] = row[column]
        data["timing"] = timing
    if row["tool_calls_present"]:
        tool_calls: list[dict[str, Any]] = []
        for call in calls.get(int(row["record_key"]), ()):
            value: dict[str, Any] = {
                "id": call["tool_call_id"],
                "name": call["name"],
                "arguments": json.loads(str(call["arguments_json"])),
            }
            if call["rejection_code"] is not None:
                value["rejection"] = {
                    "code": call["rejection_code"],
                    "message": call["rejection_message"],
                    "fingerprint": call["rejection_fingerprint"],
                }
            if call["argument_sequence_index"] is not None:
                value["argument_sequence_index"] = call["argument_sequence_index"]
                value["argument_sequence_length"] = call["argument_sequence_length"]
            tool_calls.append(value)
        data["tool_calls"] = tool_calls
    if row["sender_id"] is not None:
        data["sender"] = {
            "id": row["sender_id"],
            "display_name": row["sender_display_name"],
            "role": row["sender_role"],
        }
    output_files = files.get(int(row["record_key"])) if row["kind"] == "message" else None
    if output_files:
        data["output_files"] = [
            {
                "path": file["path"],
                "line_index": file["line_index"],
                **({} if file["start_index"] is None else {"start_index": file["start_index"]}),
                **({} if file["end_index"] is None else {"end_index": file["end_index"]}),
            }
            for file in output_files
        ]
    if row["interrupted"]:
        data["interrupted"] = True
    return data
