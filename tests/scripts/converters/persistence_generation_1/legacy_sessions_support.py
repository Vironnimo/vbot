"""A pre-Generation-1 Session database, for building converter test sources.

``LEGACY_SESSIONS_DDL`` is the Session schema frozen from the store before
Generation 1, without its search indexes; never update it to follow the
application's current schema. ``LegacySessionStore`` writes rows the way that
store's writer did: side rows per role, Tool results in the columns of their
call, Run terminals in the columns of their Run, edits that clear the
``active`` flag of the tail they replace, and forks that copy every row.
"""

# ruff: noqa: E501

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

LEGACY_SESSIONS_DDL = """
CREATE TABLE store_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
) STRICT;

CREATE TABLE sessions (
  session_key INTEGER PRIMARY KEY,
  generation_id TEXT NOT NULL UNIQUE,
  project_id TEXT NOT NULL,
  agent_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'live' CHECK (status IN ('live', 'archived')),
  created_at TEXT NOT NULL,
  last_message_at TEXT,
  active_sort REAL NOT NULL DEFAULT 0.0,
  archived_at TEXT,
  message_count INTEGER NOT NULL DEFAULT 0 CHECK (message_count >= 0),
  last_message_id TEXT,
  history_reset_sequence INTEGER NOT NULL DEFAULT 0,
  history_revision INTEGER NOT NULL DEFAULT 0 CHECK (history_revision >= 0),
  state_revision INTEGER NOT NULL DEFAULT 0 CHECK (state_revision >= 0),
  metadata_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(metadata_json) AND json_type(metadata_json) = 'object'),
  title TEXT,
  auto_title TEXT,
  source_channel_id TEXT,
  platform TEXT,
  platform_conv_id TEXT,
  is_subagent_session INTEGER CHECK (is_subagent_session IS NULL OR is_subagent_session IN (0, 1)),
  subagent_parent_json TEXT CHECK (subagent_parent_json IS NULL OR (json_valid(subagent_parent_json) AND json_type(subagent_parent_json) = 'object')),
  fork_source_json TEXT CHECK (fork_source_json IS NULL OR (json_valid(fork_source_json) AND json_type(fork_source_json) = 'object')),
  run_kinds_json TEXT CHECK (run_kinds_json IS NULL OR (json_valid(run_kinds_json) AND json_type(run_kinds_json) = 'array')),
  compaction_policy_json TEXT CHECK (compaction_policy_json IS NULL OR (json_valid(compaction_policy_json) AND json_type(compaction_policy_json) = 'object')),
  list_visibility_mask INTEGER NOT NULL DEFAULT 0 CHECK (list_visibility_mask >= 0),
  activity_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(activity_json) AND json_type(activity_json) = 'object'),
  latest_completion_run_id TEXT GENERATED ALWAYS AS (json_extract(activity_json, '$.latest_completion.run_id')) STORED,
  latest_completion_status TEXT GENERATED ALWAYS AS (json_extract(activity_json, '$.latest_completion.status')) STORED,
  latest_completion_at TEXT GENERATED ALWAYS AS (json_extract(activity_json, '$.latest_completion.timestamp')) STORED,
  read_completion_run_id TEXT GENERATED ALWAYS AS (json_extract(activity_json, '$.read_run_id')) STORED,
  CHECK (latest_completion_status IS NULL OR latest_completion_status IN ('completed', 'failed', 'cancelled', 'interrupted'))
) STRICT;

CREATE UNIQUE INDEX sessions_one_live_address
  ON sessions (project_id, agent_id, session_id)
  WHERE status = 'live';

CREATE INDEX sessions_archived_address
  ON sessions (project_id, agent_id, session_id)
  WHERE status = 'archived';

CREATE TABLE temporary_session_bindings (
  session_key INTEGER PRIMARY KEY,
  generation_id TEXT NOT NULL,
  owner_name TEXT NOT NULL,
  group_id TEXT NOT NULL,
  participant_id TEXT NOT NULL,
  config_json TEXT NOT NULL CHECK (json_valid(config_json) AND json_type(config_json) = 'object'),
  UNIQUE (owner_name, group_id, participant_id),
  FOREIGN KEY (session_key) REFERENCES sessions (session_key) ON DELETE CASCADE
) STRICT;

CREATE TABLE temporary_group_titles (
  owner_name TEXT NOT NULL,
  group_id TEXT NOT NULL,
  title TEXT NOT NULL CHECK (length(title) > 0),
  PRIMARY KEY (owner_name, group_id)
) STRICT, WITHOUT ROWID;

CREATE TABLE session_delivery_receipts (
  session_key INTEGER NOT NULL,
  generation_id TEXT NOT NULL,
  owner_name TEXT NOT NULL,
  receipt_id TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  effect_kind TEXT NOT NULL,
  carrier_kind TEXT NOT NULL,
  carrier_sequence INTEGER NOT NULL CHECK (carrier_sequence >= 0),
  PRIMARY KEY (generation_id, owner_name, receipt_id),
  FOREIGN KEY (session_key) REFERENCES sessions (session_key) ON DELETE CASCADE
) STRICT, WITHOUT ROWID;

CREATE TABLE run_execution_owners (
  record_key INTEGER PRIMARY KEY,
  session_key INTEGER NOT NULL,
  generation_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  input_id TEXT,
  owner_name TEXT NOT NULL,
  group_id TEXT NOT NULL,
  participant_id TEXT NOT NULL,
  participant_generation_id TEXT NOT NULL,
  epoch TEXT NOT NULL,
  start_sequence INTEGER NOT NULL CHECK (start_sequence >= 0),
  UNIQUE (session_key, run_id),
  UNIQUE (session_key, input_id),
  FOREIGN KEY (session_key) REFERENCES sessions (session_key) ON DELETE CASCADE
) STRICT;

CREATE TABLE runs (
  run_key INTEGER PRIMARY KEY,
  session_key INTEGER NOT NULL,
  run_id TEXT NOT NULL,
  work_id TEXT,
  run_kind TEXT NOT NULL DEFAULT 'user',
  contributes_to_activity INTEGER NOT NULL DEFAULT 1,
  origin_generation_id TEXT,
  status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed', 'cancelled', 'interrupted')),
  started_at TEXT NOT NULL,
  completed_at TEXT,
  duration_ms INTEGER CHECK (duration_ms IS NULL OR duration_ms >= 0),
  start_sequence INTEGER NOT NULL,
  terminal_sequence INTEGER,
  terminal_id TEXT,
  terminal_key INTEGER UNIQUE,
  terminal_active INTEGER NOT NULL DEFAULT 1 CHECK (terminal_active IN (0, 1)),
  completion_reason TEXT,
  timing_extra_json TEXT,
  iteration_count INTEGER,
  changed_files INTEGER,
  lines_added INTEGER,
  lines_removed INTEGER,
  change_stats_extra_json TEXT,
  UNIQUE (session_key, run_id),
  FOREIGN KEY (session_key) REFERENCES sessions(session_key) ON DELETE CASCADE,
  CHECK ((status = 'running') = (completed_at IS NULL)),
  CHECK ((terminal_sequence IS NULL) = (terminal_id IS NULL))
) STRICT;

CREATE TABLE messages (
  message_key INTEGER PRIMARY KEY,
  session_key INTEGER NOT NULL,
  seq INTEGER NOT NULL CHECK (seq >= 0),
  run_id TEXT,
  message_id TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('system', 'user', 'assistant', 'note', 'error', 'agent_takeover')),
  timestamp TEXT NOT NULL,
  content TEXT,
  content_blocks_json TEXT CHECK (content_blocks_json IS NULL OR (json_valid(content_blocks_json) AND json_type(content_blocks_json) = 'array')),
  content_search TEXT,
  model TEXT,
  active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
  searchable INTEGER NOT NULL CHECK (searchable IN (0, 1)),
  CHECK ((content IS NULL) OR (content_blocks_json IS NULL)),
  CHECK ((content_blocks_json IS NULL) = (content_search IS NULL)),
  UNIQUE (session_key, seq),
  FOREIGN KEY (session_key, run_id) REFERENCES runs(session_key, run_id),
  FOREIGN KEY (session_key) REFERENCES sessions (session_key) ON DELETE CASCADE
) STRICT;

CREATE TABLE assistant_messages (
  message_key INTEGER PRIMARY KEY,
  reasoning TEXT,
  reasoning_summary_json TEXT CHECK (reasoning_summary_json IS NULL OR (json_valid(reasoning_summary_json) AND json_type(reasoning_summary_json) = 'array')),
  reasoning_meta_json TEXT CHECK (reasoning_meta_json IS NULL OR (json_valid(reasoning_meta_json) AND json_type(reasoning_meta_json) = 'object')),
  reasoning_scope TEXT,
  reasoning_started_at TEXT,
  reasoning_completed_at TEXT,
  reasoning_duration_ms INTEGER CHECK (reasoning_duration_ms IS NULL OR reasoning_duration_ms >= 0),
  reasoning_timing_extra_json TEXT CHECK (reasoning_timing_extra_json IS NULL OR (json_valid(reasoning_timing_extra_json) AND json_type(reasoning_timing_extra_json) = 'object')),
  phase TEXT,
  input_tokens INTEGER CHECK (input_tokens IS NULL OR input_tokens >= 0),
  output_tokens INTEGER CHECK (output_tokens IS NULL OR output_tokens >= 0),
  cache_read_tokens INTEGER CHECK (cache_read_tokens IS NULL OR cache_read_tokens >= 0),
  cache_write_tokens INTEGER CHECK (cache_write_tokens IS NULL OR cache_write_tokens >= 0),
  reasoning_tokens INTEGER CHECK (reasoning_tokens IS NULL OR reasoning_tokens >= 0),
  usage_estimated INTEGER CHECK (usage_estimated IS NULL OR usage_estimated IN (0, 1)),
  input_tokens_estimated INTEGER CHECK (input_tokens_estimated IS NULL OR input_tokens_estimated IN (0, 1)),
  output_tokens_estimated INTEGER CHECK (output_tokens_estimated IS NULL OR output_tokens_estimated IN (0, 1)),
  usage_present INTEGER NOT NULL DEFAULT 0 CHECK (usage_present IN (0, 1)),
  usage_extra_json TEXT CHECK (usage_extra_json IS NULL OR (json_valid(usage_extra_json) AND json_type(usage_extra_json) = 'object')),
  tool_calls_present INTEGER NOT NULL DEFAULT 0 CHECK (tool_calls_present IN (0, 1)),
  interrupted INTEGER NOT NULL DEFAULT 0 CHECK (interrupted IN (0, 1)),
  interruption_cause TEXT,
  FOREIGN KEY (message_key) REFERENCES messages (message_key) ON DELETE CASCADE
) STRICT;

CREATE TABLE tool_calls (
  tool_call_key INTEGER PRIMARY KEY,
  message_key INTEGER NOT NULL,
  ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
  tool_call_id TEXT NOT NULL,
  name TEXT NOT NULL,
  arguments_json TEXT NOT NULL CHECK (json_valid(arguments_json) AND json_type(arguments_json) = 'object'),
  rejection_code TEXT,
  rejection_message TEXT,
  rejection_fingerprint TEXT,
  argument_sequence_index INTEGER CHECK (argument_sequence_index IS NULL OR argument_sequence_index >= 0),
  argument_sequence_length INTEGER CHECK (argument_sequence_length IS NULL OR argument_sequence_length > 1),
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'running', 'completed', 'cancelled', 'failed', 'interrupted')),
  result_id TEXT,
  result_sequence INTEGER,
  result_timestamp TEXT,
  result_key INTEGER UNIQUE,
  result_active INTEGER NOT NULL DEFAULT 1 CHECK (result_active IN (0, 1)),
  result_content TEXT,
  result_ok INTEGER GENERATED ALWAYS AS (
    CASE
      WHEN json_valid(result_content)
      THEN CASE
        WHEN json_type(result_content) = 'object'
        THEN json_extract(result_content, '$.ok')
      END
    END
  ) VIRTUAL,
  started_at TEXT,
  completed_at TEXT,
  duration_ms INTEGER CHECK (duration_ms IS NULL OR duration_ms >= 0),
  timing_extra_json TEXT CHECK (timing_extra_json IS NULL OR (json_valid(timing_extra_json) AND json_type(timing_extra_json) = 'object')),
  display_json TEXT CHECK (display_json IS NULL OR (json_valid(display_json) AND json_type(display_json) = 'object')),
  CHECK ((rejection_code IS NULL) = (rejection_message IS NULL) AND (rejection_code IS NULL) = (rejection_fingerprint IS NULL)),
  CHECK ((argument_sequence_index IS NULL) = (argument_sequence_length IS NULL)),
  CHECK ((result_id IS NULL) = (result_sequence IS NULL)),
  CHECK ((result_id IS NULL) = (result_content IS NULL)),
  UNIQUE (message_key, ordinal),
  FOREIGN KEY (message_key) REFERENCES messages (message_key) ON DELETE CASCADE
) STRICT;

CREATE TABLE assistant_output_files (
  message_key INTEGER NOT NULL,
  ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
  path TEXT NOT NULL,
  line_index INTEGER NOT NULL CHECK (line_index >= 0),
  start_index INTEGER CHECK (start_index IS NULL OR start_index >= 0),
  end_index INTEGER CHECK (end_index IS NULL OR end_index > 0),
  CHECK ((start_index IS NULL) = (end_index IS NULL)),
  PRIMARY KEY (message_key, ordinal),
  FOREIGN KEY (message_key) REFERENCES assistant_messages (message_key) ON DELETE CASCADE
) STRICT, WITHOUT ROWID;

CREATE TABLE user_message_senders (
  message_key INTEGER PRIMARY KEY,
  sender_id TEXT NOT NULL,
  display_name TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('admin', 'member')),
  FOREIGN KEY (message_key) REFERENCES messages (message_key) ON DELETE CASCADE
) STRICT;

CREATE TABLE error_messages (
  message_key INTEGER PRIMARY KEY,
  error_kind TEXT NOT NULL,
  FOREIGN KEY (message_key) REFERENCES messages (message_key) ON DELETE CASCADE
) STRICT;

CREATE TABLE compaction_checkpoints (
  snapshot_key INTEGER PRIMARY KEY,
  session_key INTEGER NOT NULL,
  seq INTEGER NOT NULL,
  run_id TEXT,
  message_id TEXT NOT NULL,
  timestamp TEXT NOT NULL,
  content TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
  tail_boundary_id TEXT,
  projection_json TEXT CHECK (projection_json IS NULL OR (json_valid(projection_json) AND json_type(projection_json) = 'array')),
  policy TEXT,
  strategy TEXT,
  compacted_token_count INTEGER CHECK (compacted_token_count IS NULL OR compacted_token_count >= 0),
  context_tokens_before INTEGER CHECK (context_tokens_before IS NULL OR context_tokens_before >= 0),
  context_tokens_after INTEGER CHECK (context_tokens_after IS NULL OR context_tokens_after >= 0),
  compaction_duration_ms INTEGER CHECK (compaction_duration_ms IS NULL OR compaction_duration_ms >= 0),
  usage_present INTEGER NOT NULL DEFAULT 0 CHECK (usage_present IN (0, 1)),
  usage_extra_json TEXT CHECK (usage_extra_json IS NULL OR (json_valid(usage_extra_json) AND json_type(usage_extra_json) = 'object')),
  CHECK ((context_tokens_before IS NULL) = (context_tokens_after IS NULL)),
  FOREIGN KEY (session_key) REFERENCES sessions (session_key) ON DELETE CASCADE,
  FOREIGN KEY (session_key, run_id) REFERENCES runs(session_key, run_id)
) STRICT;

CREATE TABLE run_change_paths (
  run_key INTEGER NOT NULL,
  ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
  path TEXT NOT NULL,
  PRIMARY KEY (run_key, ordinal),
  FOREIGN KEY (run_key) REFERENCES runs (run_key) ON DELETE CASCADE
) STRICT, WITHOUT ROWID;

CREATE TABLE history_edits (
  edit_key INTEGER PRIMARY KEY,
  session_key INTEGER NOT NULL,
  seq INTEGER NOT NULL,
  message_id TEXT NOT NULL,
  timestamp TEXT NOT NULL,
  target_message_id TEXT NOT NULL,
  FOREIGN KEY (session_key) REFERENCES sessions(session_key) ON DELETE CASCADE
) STRICT;

CREATE TABLE continuations (
  session_key INTEGER PRIMARY KEY,
  checkpoint_id TEXT NOT NULL,
  origin_run_id TEXT NOT NULL,
  latest_run_id TEXT NOT NULL,
  cause TEXT CHECK (cause IS NULL OR cause IN ('user', 'provider', 'network', 'timeout', 'process_restart', 'internal')),
  active INTEGER NOT NULL CHECK (active IN (0, 1)),
  CHECK ((active = 1 AND cause IS NULL) OR (active = 0 AND cause IS NOT NULL)),
  FOREIGN KEY (session_key) REFERENCES sessions (session_key) ON DELETE CASCADE
) STRICT;

CREATE VIEW history_records AS
SELECT m.message_key AS message_key, m.message_key AS source_key,
       m.session_key, m.seq, m.message_id, m.role, m.timestamp, m.content,
       m.content_blocks_json, m.content_search, m.model, m.active, m.searchable,
       m.run_id AS owner_run_id
FROM messages m
UNION ALL
SELECT t.result_key, NULL, m.session_key, t.result_sequence, t.result_id,
       'tool', t.result_timestamp, t.result_content, NULL, NULL, NULL,
       t.result_active, 1, m.run_id
FROM tool_calls t JOIN messages m ON m.message_key = t.message_key
WHERE t.result_id IS NOT NULL
UNION ALL
SELECT r.terminal_key, NULL, r.session_key, r.terminal_sequence, r.terminal_id,
       'run_summary', r.completed_at, NULL, NULL, NULL, NULL,
       r.terminal_active, 0, r.run_id
FROM runs r WHERE r.terminal_sequence IS NOT NULL
UNION ALL
SELECT c.snapshot_key, NULL, c.session_key, c.seq, c.message_id,
       'compaction_checkpoint', c.timestamp, c.content, NULL, NULL, NULL,
       c.active, 1, c.run_id
FROM compaction_checkpoints c
UNION ALL
SELECT e.edit_key, NULL, e.session_key, e.seq, e.message_id,
       'history_edit', e.timestamp, NULL, NULL, NULL, NULL, 0, 0, NULL
FROM history_edits e;
"""

# The identity released builds stamped on the Session database: "VBOT", 1.
RELEASED_IDENTITY = (0x56424F54, 1)

_START = datetime(2026, 3, 1, 10, 0, tzinfo=UTC)

_SCALAR_METADATA = ("title", "auto_title", "source_channel_id", "platform", "platform_conv_id")
_JSON_METADATA = (
    ("subagent_parent", "subagent_parent_json", dict),
    ("fork_source", "fork_source_json", dict),
    ("run_kinds", "run_kinds_json", list),
    ("compaction_policy", "compaction_policy_json", dict),
)
_METADATA_COLUMNS = (
    *_SCALAR_METADATA,
    "is_subagent_session",
    *(column for _key, column, _type in _JSON_METADATA),
)


def at(minute: float) -> str:
    """The old store's text for ``minute`` minutes after the test start."""
    return (_START + timedelta(minutes=minute)).isoformat()


def canonical(minute: float) -> str:
    """The Generation 1 text of the same instant."""
    return (_START + timedelta(minutes=minute)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _json(value: Any) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False)


def _split(
    payload: dict[str, Any] | None, promoted: Sequence[str]
) -> tuple[dict[str, Any], str | None, bool]:
    """Promote known fields into columns and keep the rest as JSON, as the old codec did."""
    if payload is None:
        return {}, None, False
    remaining = dict(payload)
    columns = {key: remaining.pop(key) for key in promoted if key in remaining}
    return columns, _json(remaining or None), True


class LegacySessionStore:
    """Write Session rows the way the store before Generation 1 wrote them."""

    def __init__(
        self,
        path: Path,
        *,
        identity: tuple[int, int] = RELEASED_IDENTITY,
        journal_mode: str = "delete",
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.connection = sqlite3.connect(path, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(f"PRAGMA journal_mode = {journal_mode}")
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.executescript(LEGACY_SESSIONS_DDL)
        self.connection.execute(f"PRAGMA application_id = {identity[0]}")
        self.connection.execute(f"PRAGMA user_version = {identity[1]}")
        self._ids = 0

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> LegacySessionStore:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def execute(self, sql: str, parameters: Sequence[Any] = ()) -> sqlite3.Cursor:
        """Run raw SQL, for rows the old writer could have left behind."""
        return self.connection.execute(sql, tuple(parameters))

    def generation(self, key: int) -> str:
        return str(self._state(key)["generation_id"])

    def _new_id(self, prefix: str) -> str:
        self._ids += 1
        return f"{prefix}_{self._ids:04d}"

    def _state(self, key: int) -> sqlite3.Row:
        row: sqlite3.Row = self.connection.execute(
            "SELECT * FROM sessions WHERE session_key = ?", (key,)
        ).fetchone()
        return row

    def _history_key(self) -> int:
        """The old store numbered every history row from one counter."""
        row = self.connection.execute(
            "INSERT INTO store_meta(key, value) VALUES ('history_identity', '1') "
            "ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1 RETURNING value"
        ).fetchone()
        return int(row[0])

    # -- Sessions ------------------------------------------------------------------------

    def session(
        self,
        session_id: str,
        *,
        agent_id: str = "main",
        project_id: str | None = None,
        minute: float = 0,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        payload, columns = self._metadata_storage(metadata or {})
        cursor = self.connection.execute(
            "INSERT INTO sessions (generation_id, project_id, agent_id, session_id, created_at, "
            f"metadata_json, {', '.join(_METADATA_COLUMNS)}) "
            f"VALUES (?, ?, ?, ?, ?, ?, {', '.join('?' for _ in _METADATA_COLUMNS)})",
            (
                self._new_id("gen"),
                project_id or "",
                agent_id,
                session_id,
                at(minute),
                payload,
                *columns,
            ),
        )
        return int(cursor.lastrowid or 0)

    def metadata(self, key: int) -> dict[str, Any]:
        """The old metadata facade: the open object updated by its columns."""
        state = self._state(key)
        metadata: dict[str, Any] = json.loads(state["metadata_json"])
        for name in _SCALAR_METADATA:
            if state[name] is not None:
                metadata[name] = state[name]
        if state["is_subagent_session"] is not None:
            metadata["is_subagent_session"] = bool(state["is_subagent_session"])
        for name, column, _type in _JSON_METADATA:
            if state[column] is not None:
                metadata[name] = json.loads(state[column])
        return metadata

    def mutate_metadata(self, key: int, change: Callable[[dict[str, Any]], None]) -> None:
        metadata = self.metadata(key)
        change(metadata)
        payload, columns = self._metadata_storage(metadata)
        self.connection.execute(
            f"UPDATE sessions SET metadata_json = ?, {', '.join(f'{c} = ?' for c in _METADATA_COLUMNS)}, "
            "state_revision = state_revision + 1 WHERE session_key = ?",
            (payload, *columns, key),
        )

    @staticmethod
    def _metadata_storage(metadata: dict[str, Any]) -> tuple[str, tuple[Any, ...]]:
        residual = dict(metadata)
        columns: dict[str, Any] = dict.fromkeys(_METADATA_COLUMNS)
        for name in _SCALAR_METADATA:
            if isinstance(residual.get(name), str):
                columns[name] = residual.pop(name)
        if isinstance(residual.get("is_subagent_session"), bool):
            columns["is_subagent_session"] = int(residual.pop("is_subagent_session"))
        for name, column, expected in _JSON_METADATA:
            if isinstance(residual.get(name), expected):
                columns[column] = json.dumps(residual.pop(name), separators=(",", ":"))
        return json.dumps(residual), tuple(columns[column] for column in _METADATA_COLUMNS)

    def archive(self, key: int, *, minute: float) -> None:
        self.connection.execute(
            "UPDATE sessions SET status = 'archived', archived_at = ?, "
            "state_revision = state_revision + 1 WHERE session_key = ?",
            (at(minute), key),
        )

    def delete(self, key: int) -> None:
        self.connection.execute("DELETE FROM sessions WHERE session_key = ?", (key,))

    # -- Runs ----------------------------------------------------------------------------

    def start_run(
        self,
        key: int,
        run_id: str,
        *,
        minute: float,
        run_kind: str = "user",
        contributes: bool = True,
        work_id: str | None = None,
    ) -> None:
        self.connection.execute(
            "INSERT INTO runs (session_key, run_id, work_id, run_kind, contributes_to_activity, "
            "status, started_at, start_sequence) VALUES (?, ?, ?, ?, ?, 'running', ?, ?)",
            (
                key,
                run_id,
                work_id,
                run_kind,
                int(contributes),
                at(minute),
                self._state(key)["message_count"],
            ),
        )

        def record_kind(metadata: dict[str, Any]) -> None:
            kinds = metadata.setdefault("run_kinds", [])
            if run_kind not in kinds:
                kinds.append(run_kind)

        self.mutate_metadata(key, record_kind)

    def finish_run(
        self,
        key: int,
        run_id: str,
        *,
        minute: float,
        status: str = "completed",
        started_minute: float | None = None,
        iteration_count: int = 1,
        change_stats: dict[str, Any] | None = None,
        completion_reason: str | None = None,
    ) -> str:
        """Settle a running Run with its terminal record; return the terminal id."""
        run = self.connection.execute(
            "SELECT * FROM runs WHERE session_key = ? AND run_id = ? AND status = 'running'",
            (key, run_id),
        ).fetchone()
        assert run is not None, f"Run {run_id} is not running"
        started_at = run["started_at"] if started_minute is None else at(started_minute)
        completed_at = at(minute)
        duration = round(
            (
                datetime.fromisoformat(completed_at) - datetime.fromisoformat(started_at)
            ).total_seconds()
            * 1000
        )
        changes, changes_extra, present = _split(
            change_stats, ("files", "added", "removed", "paths")
        )
        terminal_id = self._new_id("msg")
        sequence = int(self._state(key)["message_count"])
        self.connection.execute(
            "UPDATE runs SET status = ?, started_at = ?, completed_at = ?, duration_ms = ?, "
            "iteration_count = ?, changed_files = ?, lines_added = ?, lines_removed = ?, "
            "change_stats_extra_json = ?, terminal_sequence = ?, terminal_id = ?, terminal_key = ?, "
            "completion_reason = ? WHERE run_key = ?",
            (
                status,
                started_at,
                completed_at,
                duration,
                iteration_count,
                changes.get("files") if present else None,
                changes.get("added") if present else None,
                changes.get("removed") if present else None,
                changes_extra,
                sequence,
                terminal_id,
                self._history_key(),
                completion_reason,
                run["run_key"],
            ),
        )
        for ordinal, path in enumerate(changes.get("paths", ())):
            self.connection.execute(
                "INSERT INTO run_change_paths (run_key, ordinal, path) VALUES (?, ?, ?)",
                (run["run_key"], ordinal, path),
            )
        self.connection.execute(
            "UPDATE tool_calls SET status = ?, completed_at = ? WHERE result_id IS NULL "
            "AND message_key IN (SELECT message_key FROM messages WHERE session_key = ? AND run_id = ?)",
            ("cancelled" if status == "cancelled" else "interrupted", completed_at, key, run_id),
        )
        activity = json.loads(self._state(key)["activity_json"])
        if run["contributes_to_activity"]:
            activity["latest_completion"] = {
                "run_id": run_id,
                "status": status,
                "timestamp": completed_at,
            }
        self.connection.execute(
            "UPDATE sessions SET message_count = message_count + 1, last_message_at = ?, "
            "last_message_id = ?, history_revision = history_revision + 1, "
            "state_revision = state_revision + 1, activity_json = ? WHERE session_key = ?",
            (completed_at, terminal_id, json.dumps(activity), key),
        )
        return terminal_id

    # -- History -------------------------------------------------------------------------

    def user(
        self,
        key: int,
        content: str | list[dict[str, Any]],
        *,
        minute: float,
        run_id: str | None = None,
        sender: dict[str, str] | None = None,
    ) -> str:
        message_key, message_id = self._message(key, "user", content, minute=minute, run_id=run_id)
        if sender is not None:
            self.connection.execute(
                "INSERT INTO user_message_senders (message_key, sender_id, display_name, role) "
                "VALUES (?, ?, ?, ?)",
                (message_key, sender["id"], sender["display_name"], sender["role"]),
            )
        return message_id

    def note(self, key: int, content: str, *, minute: float, run_id: str | None = None) -> str:
        return self._message(key, "note", content, minute=minute, run_id=run_id)[1]

    def error(
        self, key: int, error_kind: str, content: str, *, minute: float, run_id: str | None = None
    ) -> str:
        message_key, message_id = self._message(key, "error", content, minute=minute, run_id=run_id)
        self.connection.execute(
            "INSERT INTO error_messages (message_key, error_kind) VALUES (?, ?)",
            (message_key, error_kind),
        )
        return message_id

    def assistant(
        self,
        key: int,
        content: str | None,
        *,
        minute: float,
        run_id: str | None = None,
        tool_calls: Sequence[dict[str, Any]] | None = None,
        model: str = "test-model",
        reasoning: str | None = None,
        reasoning_timing: dict[str, Any] | None = None,
        usage: dict[str, Any] | None = None,
        output_files: Sequence[dict[str, Any]] = (),
        interrupted: bool = False,
    ) -> str:
        message_key, message_id = self._message(
            key, "assistant", content, minute=minute, run_id=run_id, model=model
        )
        usage_columns, usage_extra, usage_present = _split(
            usage,
            (
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
                "reasoning_tokens",
                "estimated",
                "input_tokens_estimated",
                "output_tokens_estimated",
            ),
        )
        flags = {
            name: None if usage_columns.get(name) is None else int(usage_columns[name])
            for name in ("estimated", "input_tokens_estimated", "output_tokens_estimated")
        }
        timing, timing_extra, _present = _split(
            reasoning_timing, ("started_at", "completed_at", "duration_ms")
        )
        self.connection.execute(
            "INSERT INTO assistant_messages (message_key, reasoning, reasoning_started_at, "
            "reasoning_completed_at, reasoning_duration_ms, reasoning_timing_extra_json, "
            "input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, reasoning_tokens, "
            "usage_estimated, input_tokens_estimated, output_tokens_estimated, "
            "usage_present, usage_extra_json, tool_calls_present, interrupted) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                message_key,
                reasoning,
                timing.get("started_at"),
                timing.get("completed_at"),
                timing.get("duration_ms"),
                timing_extra,
                usage_columns.get("input_tokens"),
                usage_columns.get("output_tokens"),
                usage_columns.get("cache_read_tokens"),
                usage_columns.get("cache_write_tokens"),
                usage_columns.get("reasoning_tokens"),
                flags["estimated"],
                flags["input_tokens_estimated"],
                flags["output_tokens_estimated"],
                int(usage_present),
                usage_extra,
                int(tool_calls is not None),
                int(interrupted),
            ),
        )
        for ordinal, call in enumerate(tool_calls or ()):
            self.connection.execute(
                "INSERT INTO tool_calls (message_key, ordinal, tool_call_id, name, arguments_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    message_key,
                    ordinal,
                    call["id"],
                    call["name"],
                    json.dumps(call.get("arguments", {})),
                ),
            )
        for ordinal, reference in enumerate(output_files):
            self.connection.execute(
                "INSERT INTO assistant_output_files (message_key, ordinal, path, line_index, "
                "start_index, end_index) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    message_key,
                    ordinal,
                    reference["path"],
                    reference["line_index"],
                    reference.get("start_index"),
                    reference.get("end_index"),
                ),
            )
        return message_id

    def tool_result(
        self,
        key: int,
        tool_call_id: str,
        content: str,
        *,
        minute: float,
        display: dict[str, Any] | None = None,
        duration_ms: int = 250,
    ) -> str:
        """Complete one Tool call with its result, as the old store's ``_complete_tool`` did."""
        call = self.connection.execute(
            "SELECT t.tool_call_key FROM tool_calls AS t JOIN messages AS m "
            "ON m.message_key = t.message_key WHERE m.session_key = ? AND t.tool_call_id = ?",
            (key, tool_call_id),
        ).fetchone()
        assert call is not None, f"no Tool call {tool_call_id}"
        result = json.loads(content)
        status = "completed"
        if result.get("ok") is False:
            code = (result.get("error") or {}).get("code")
            status = (
                "cancelled"
                if code in {"cancelled", "tool_cancelled", "user_cancelled"}
                else "failed"
            )
        message_id = self._new_id("msg")
        sequence = int(self._state(key)["message_count"])
        self.connection.execute(
            "UPDATE tool_calls SET result_id = ?, result_sequence = ?, result_timestamp = ?, "
            "result_content = ?, status = ?, started_at = ?, completed_at = ?, duration_ms = ?, "
            "display_json = ?, result_key = ? WHERE tool_call_key = ?",
            (
                message_id,
                sequence,
                at(minute),
                content,
                status,
                at(minute),
                at(minute),
                duration_ms,
                _json(display),
                self._history_key(),
                call["tool_call_key"],
            ),
        )
        self._appended(key, message_id, at(minute))
        return message_id

    def checkpoint(
        self,
        key: int,
        summary: str,
        *,
        minute: float,
        tail_boundary_id: str | None = None,
        projection: list[dict[str, Any]] | None = None,
        policy: str | None = None,
        strategy: str | None = None,
        compacted_token_count: int = 900,
    ) -> str:
        message_id = self._new_id("msg")
        self.connection.execute(
            "INSERT INTO compaction_checkpoints (snapshot_key, session_key, seq, message_id, "
            "timestamp, content, tail_boundary_id, projection_json, policy, strategy, "
            "compacted_token_count, usage_present) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
            (
                self._history_key(),
                key,
                self._state(key)["message_count"],
                message_id,
                at(minute),
                summary,
                tail_boundary_id,
                _json(projection),
                policy,
                strategy,
                compacted_token_count,
            ),
        )
        self._appended(key, message_id, at(minute))
        return message_id

    def edit(self, key: int, target_message_id: str, *, minute: float) -> str:
        """Replace history from the target User Message, as ``_deactivate_history_tail`` did."""
        target = self.connection.execute(
            "SELECT seq FROM history_records WHERE session_key = ? AND message_id = ? "
            "AND role = 'user' AND active = 1 ORDER BY seq LIMIT 1",
            (key, target_message_id),
        ).fetchone()
        assert target is not None, f"{target_message_id} is not an active User Message"
        floor = int(target["seq"])
        for statement in (
            "UPDATE messages SET active = 0 WHERE session_key = ? AND seq >= ?",
            "UPDATE compaction_checkpoints SET active = 0 WHERE session_key = ? AND seq >= ?",
            "UPDATE runs SET terminal_active = 0 WHERE session_key = ? AND terminal_sequence >= ?",
        ):
            self.connection.execute(statement, (key, floor))
        self.connection.execute(
            "UPDATE tool_calls SET result_active = 0 WHERE result_sequence >= ? "
            "AND message_key IN (SELECT message_key FROM messages WHERE session_key = ?)",
            (floor, key),
        )
        message_id = self._new_id("msg")
        sequence = int(self._state(key)["message_count"])
        self.connection.execute(
            "INSERT INTO history_edits (edit_key, session_key, seq, message_id, timestamp, "
            "target_message_id) VALUES (?, ?, ?, ?, ?, ?)",
            (self._history_key(), key, sequence, message_id, at(minute), target_message_id),
        )
        self.connection.execute(
            "UPDATE sessions SET history_reset_sequence = ? WHERE session_key = ?",
            (sequence + 1, key),
        )
        self._appended(key, message_id, at(minute))
        return message_id

    def _message(
        self,
        key: int,
        role: str,
        content: str | list[dict[str, Any]] | None,
        *,
        minute: float,
        run_id: str | None,
        model: str | None = None,
    ) -> tuple[int, str]:
        blocks = content if isinstance(content, list) else None
        search = (
            None if blocks is None else " ".join(str(block.get("text", "")) for block in blocks)
        )
        message_id = self._new_id("msg")
        message_key = self._history_key()
        self.connection.execute(
            "INSERT INTO messages (message_key, session_key, seq, message_id, role, timestamp, "
            "content, content_blocks_json, content_search, model, active, searchable, run_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 1, ?)",
            (
                message_key,
                key,
                self._state(key)["message_count"],
                message_id,
                role,
                at(minute),
                None if blocks is not None else content,
                _json(blocks),
                search,
                model,
                run_id,
            ),
        )
        self._appended(key, message_id, at(minute))
        return message_key, message_id

    def _appended(self, key: int, message_id: str, timestamp: str) -> None:
        self.connection.execute(
            "UPDATE sessions SET message_count = message_count + 1, last_message_at = ?, "
            "last_message_id = ?, history_revision = history_revision + 1, "
            "state_revision = state_revision + 1 WHERE session_key = ?",
            (timestamp, message_id, key),
        )

    # -- Forks ---------------------------------------------------------------------------

    def fork(
        self,
        source_key: int,
        session_id: str,
        *,
        minute: float,
        agent_id: str | None = None,
        project_id: str | None = None,
    ) -> int:
        """Fork ``source_key``: copy every row, as the old ``_copy_session_messages`` did."""
        state = self._state(source_key)
        metadata = self.metadata(source_key)
        metadata["fork_source"] = {
            "agent_id": state["agent_id"],
            "session_id": state["session_id"],
            "project_id": state["project_id"] or None,
            "forked_at": at(minute),
            "message_count": state["message_count"],
        }
        payload, columns = self._metadata_storage(metadata)
        cursor = self.connection.execute(
            "INSERT INTO sessions (generation_id, project_id, agent_id, session_id, created_at, "
            "last_message_at, message_count, last_message_id, history_revision, metadata_json, "
            f"{', '.join(_METADATA_COLUMNS)}) "
            f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, {', '.join('?' for _ in _METADATA_COLUMNS)})",
            (
                self._new_id("gen"),
                (state["project_id"] if project_id is None else project_id),
                agent_id or state["agent_id"],
                session_id,
                state["created_at"],
                state["last_message_at"],
                state["message_count"],
                state["last_message_id"],
                state["history_revision"],
                payload,
                *columns,
            ),
        )
        target_key = int(cursor.lastrowid or 0)
        self._copy_rows(source_key, target_key, at(minute))
        return target_key

    def _copy_rows(self, source_key: int, target_key: int, now: str) -> None:
        connection = self.connection
        offset = self._history_key()
        maximum = connection.execute(
            "SELECT COALESCE(MAX(message_key), 0) FROM history_records WHERE session_key = ?",
            (source_key,),
        ).fetchone()[0]
        connection.execute(
            "UPDATE store_meta SET value = ? WHERE key = 'history_identity'",
            (str(offset + maximum),),
        )

        def copy(table: str, replacements: dict[str, str | None], where: str) -> None:
            columns = [
                str(row[1])
                for row in connection.execute(f"PRAGMA table_xinfo({table})")
                if not row[6] and replacements.get(str(row[1]), "") is not None
            ]
            expressions = [replacements.get(column) or f"source.{column}" for column in columns]
            connection.execute(
                f"INSERT INTO {table} ({', '.join(columns)}) SELECT {', '.join(expressions)} "
                f"FROM {table} AS source WHERE {where}",
                (source_key,),
            )

        scope = "source.session_key = ?"
        session = {"session_key": str(target_key)}
        copy(
            "runs",
            {
                **session,
                "run_key": None,
                "status": "CASE WHEN source.status = 'running' THEN 'interrupted' ELSE source.status END",
                "completed_at": f"CASE WHEN source.status = 'running' THEN '{now}' ELSE source.completed_at END",
                "completion_reason": "CASE WHEN source.status = 'running' THEN 'fork_snapshot' ELSE source.completion_reason END",
                "contributes_to_activity": "0",
                "terminal_key": f"source.terminal_key + {offset}",
                "origin_generation_id": "COALESCE(source.origin_generation_id, "
                f"(SELECT generation_id FROM sessions WHERE session_key = {source_key}))",
            },
            scope,
        )
        copy("messages", {**session, "message_key": f"source.message_key + {offset}"}, scope)
        owned = "source.message_key IN (SELECT message_key FROM messages WHERE session_key = ?)"
        for table in (
            "assistant_messages",
            "assistant_output_files",
            "user_message_senders",
            "error_messages",
        ):
            copy(table, {"message_key": f"source.message_key + {offset}"}, owned)
        copy(
            "tool_calls",
            {
                "message_key": f"source.message_key + {offset}",
                "tool_call_key": None,
                "result_key": f"source.result_key + {offset}",
                "status": "CASE WHEN source.status IN ('pending', 'running') THEN 'interrupted' ELSE source.status END",
                "completed_at": f"CASE WHEN source.status IN ('pending', 'running') THEN '{now}' ELSE source.completed_at END",
            },
            owned,
        )
        copy(
            "compaction_checkpoints",
            {**session, "snapshot_key": f"source.snapshot_key + {offset}"},
            scope,
        )
        copy("history_edits", {**session, "edit_key": f"source.edit_key + {offset}"}, scope)
        connection.execute(
            "INSERT INTO run_change_paths (run_key, ordinal, path) "
            "SELECT target.run_key, p.ordinal, p.path FROM run_change_paths AS p "
            "JOIN runs AS source ON source.run_key = p.run_key JOIN runs AS target "
            "ON target.session_key = ? AND target.run_id = source.run_id WHERE source.session_key = ?",
            (target_key, source_key),
        )

    # -- Owner-managed rows and Continuations --------------------------------------------

    def bind(
        self, key: int, *, owner: str, group: str, participant: str, config: dict[str, Any]
    ) -> None:
        self.connection.execute(
            "INSERT INTO temporary_session_bindings (session_key, generation_id, owner_name, "
            "group_id, participant_id, config_json) VALUES (?, ?, ?, ?, ?, ?)",
            (key, self.generation(key), owner, group, participant, json.dumps(config)),
        )

    def group_title(self, *, owner: str, group: str, title: str) -> None:
        self.connection.execute(
            "INSERT INTO temporary_group_titles (owner_name, group_id, title) VALUES (?, ?, ?)",
            (owner, group, title),
        )

    def receipt(self, key: int, *, owner: str, receipt_id: str, carrier_sequence: int) -> None:
        self.connection.execute(
            "INSERT INTO session_delivery_receipts (session_key, generation_id, owner_name, "
            "receipt_id, content_hash, effect_kind, carrier_kind, carrier_sequence) "
            "VALUES (?, ?, ?, ?, ?, 'message', 'session', ?)",
            (key, self.generation(key), owner, receipt_id, f"hash-{receipt_id}", carrier_sequence),
        )

    def execution_owner(
        self,
        key: int,
        run_id: str,
        *,
        record_key: int,
        owner: str,
        group: str,
        participant: str,
        input_id: str | None = None,
    ) -> None:
        self.connection.execute(
            "INSERT INTO run_execution_owners (record_key, session_key, generation_id, run_id, "
            "input_id, owner_name, group_id, participant_id, participant_generation_id, epoch, "
            "start_sequence) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'epoch-1', 0)",
            (
                record_key,
                key,
                self.generation(key),
                run_id,
                input_id,
                owner,
                group,
                participant,
                f"pgen-{participant}",
            ),
        )

    def continuation(self, key: int, *, checkpoint_id: str, run_id: str) -> None:
        self.connection.execute(
            "INSERT INTO continuations (session_key, checkpoint_id, origin_run_id, latest_run_id, "
            "cause, active) VALUES (?, ?, ?, ?, NULL, 1)",
            (key, checkpoint_id, run_id, run_id),
        )
