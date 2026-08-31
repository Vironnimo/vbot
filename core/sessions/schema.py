"""Declarative schema, runtime requirements, and additive reconciliation for the canonical SQLite Session store."""
# ruff: noqa: E501

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from core.sessions.errors import SessionStoreCorruptError

SCHEMA_VERSION = 4
# This is the physical store-format generation, not a counter for every
# additive column. Current-generation databases reconcile additive SCHEMA_SQL
# changes in place; raise both values only for a destructive format change.
SCHEMA_CONVERSION_FLOOR = 4
MINIMUM_SQLITE_VERSION = (3, 37, 0)
APPLICATION_ID = 0x56424F54
DATABASE_ID_META_KEY = "database_id"
JOURNAL_MODE_WAL = "wal"
JOURNAL_MODE_DELETE = "delete"

# FTS declarative constants — kept testable and in one place.
FTS_TABLE = "messages_fts"
FTS_VIEW = "messages_fts_source"
FTS_TRIGRAM_TABLE = "messages_fts_trigram"
FTS_TRIGRAM_VIEW = "messages_fts_trigram_source"
FTS_TRIGGERS = (
    "messages_fts_delete",
    "messages_fts_trigram_delete",
)
FTS_STALE_KEY = "fts_stale"
FTS_GENERATION_KEY = "fts_rebuild_generation"
FTS_TARGET_HIGH_WATER_KEY = "fts_rebuild_target_high_water"
FTS_COMPLETED_HIGH_WATER_KEY = "fts_rebuild_completed_high_water"
FTS_DEGRADED_REASON_KEY = "fts_degraded_reason"
# Kept as aliases for callers that only need the target/progress distinction.
FTS_HIGH_WATER_KEY = FTS_TARGET_HIGH_WATER_KEY
FTS_PROGRESS_KEY = FTS_COMPLETED_HIGH_WATER_KEY
FTS_STORAGE_VERSION_KEY = "fts_storage_version"
FTS_STORAGE_VERSION = 3
FTS_TRIGRAM_TOKENIZER = "trigram"

SCHEMA_SQL = """
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
  archived_at TEXT,
  message_count INTEGER NOT NULL DEFAULT 0 CHECK (message_count >= 0),
  last_message_id TEXT,
  history_revision INTEGER NOT NULL DEFAULT 0 CHECK (history_revision >= 0),
  state_revision INTEGER NOT NULL DEFAULT 0 CHECK (state_revision >= 0),
  metadata_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(metadata_json) AND json_type(metadata_json) = 'object'),
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

CREATE INDEX sessions_live_scope_order
  ON sessions (project_id, agent_id, last_message_at DESC, session_id)
  WHERE status = 'live';

CREATE TABLE messages (
  message_key INTEGER PRIMARY KEY,
  session_key INTEGER NOT NULL,
  seq INTEGER NOT NULL CHECK (seq >= 0),
  message_id TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('system', 'user', 'assistant', 'tool', 'note', 'error', 'compaction_checkpoint', 'run_summary', 'agent_takeover', 'history_edit')),
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
  FOREIGN KEY (session_key) REFERENCES sessions (session_key) ON DELETE CASCADE
) STRICT;

CREATE INDEX messages_by_session_time
  ON messages (session_key, timestamp, seq);

CREATE INDEX messages_by_message_id
  ON messages (session_key, message_id);

CREATE INDEX messages_active_by_session
  ON messages (session_key, seq)
  WHERE active = 1;

CREATE TABLE assistant_messages (
  message_key INTEGER PRIMARY KEY,
  reasoning TEXT,
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
  CHECK ((rejection_code IS NULL) = (rejection_message IS NULL) AND (rejection_code IS NULL) = (rejection_fingerprint IS NULL)),
  CHECK ((argument_sequence_index IS NULL) = (argument_sequence_length IS NULL)),
  UNIQUE (message_key, ordinal),
  FOREIGN KEY (message_key) REFERENCES messages (message_key) ON DELETE CASCADE
) STRICT;

CREATE INDEX tool_calls_by_public_id
  ON tool_calls (tool_call_id, message_key);

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

CREATE TABLE tool_messages (
  message_key INTEGER PRIMARY KEY,
  tool_call_key INTEGER,
  tool_call_id TEXT NOT NULL,
  name TEXT NOT NULL,
  result_content TEXT NOT NULL,
  result_ok INTEGER GENERATED ALWAYS AS (
    CASE
      WHEN json_valid(result_content)
      THEN CASE
        WHEN json_type(result_content) = 'object'
        THEN json_extract(result_content, '$.ok')
      END
    END
  ) VIRTUAL,
  error_code TEXT GENERATED ALWAYS AS (
    CASE WHEN json_valid(result_content) THEN json_extract(result_content, '$.error.code') END
  ) VIRTUAL,
  error_message TEXT GENERATED ALWAYS AS (
    CASE WHEN json_valid(result_content) THEN json_extract(result_content, '$.error.message') END
  ) VIRTUAL,
  error_retryable INTEGER GENERATED ALWAYS AS (
    CASE WHEN json_valid(result_content) THEN json_extract(result_content, '$.error.retryable') END
  ) VIRTUAL,
  error_attempts_made INTEGER GENERATED ALWAYS AS (
    CASE WHEN json_valid(result_content) THEN json_extract(result_content, '$.error.attempts_made') END
  ) VIRTUAL,
  data_json TEXT GENERATED ALWAYS AS (
    CASE WHEN json_valid(result_content) THEN json_extract(result_content, '$.data') END
  ) VIRTUAL,
  artifacts_json TEXT GENERATED ALWAYS AS (
    CASE WHEN json_valid(result_content) THEN json_extract(result_content, '$.artifacts') END
  ) VIRTUAL,
  started_at TEXT,
  completed_at TEXT,
  duration_ms INTEGER CHECK (duration_ms IS NULL OR duration_ms >= 0),
  timing_extra_json TEXT CHECK (timing_extra_json IS NULL OR (json_valid(timing_extra_json) AND json_type(timing_extra_json) = 'object')),
  display_json TEXT CHECK (display_json IS NULL OR (json_valid(display_json) AND json_type(display_json) = 'object')),
  FOREIGN KEY (message_key) REFERENCES messages (message_key) ON DELETE CASCADE,
  FOREIGN KEY (tool_call_key) REFERENCES tool_calls (tool_call_key)
) STRICT;

CREATE INDEX tool_messages_by_call
  ON tool_messages (tool_call_key);

CREATE TABLE error_messages (
  message_key INTEGER PRIMARY KEY,
  error_kind TEXT NOT NULL,
  FOREIGN KEY (message_key) REFERENCES messages (message_key) ON DELETE CASCADE
) STRICT;

CREATE TABLE compaction_checkpoints (
  message_key INTEGER PRIMARY KEY,
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
  FOREIGN KEY (message_key) REFERENCES messages (message_key) ON DELETE CASCADE
) STRICT;

CREATE TABLE run_summaries (
  message_key INTEGER PRIMARY KEY,
  run_id TEXT NOT NULL,
  work_id TEXT,
  status TEXT NOT NULL CHECK (status IN ('completed', 'failed', 'cancelled', 'interrupted')),
  started_at TEXT NOT NULL,
  completed_at TEXT NOT NULL,
  duration_ms INTEGER NOT NULL CHECK (duration_ms >= 0),
  timing_extra_json TEXT CHECK (timing_extra_json IS NULL OR (json_valid(timing_extra_json) AND json_type(timing_extra_json) = 'object')),
  iteration_count INTEGER CHECK (iteration_count IS NULL OR iteration_count >= 0),
  changed_files INTEGER CHECK (changed_files IS NULL OR changed_files >= 0),
  lines_added INTEGER CHECK (lines_added IS NULL OR lines_added >= 0),
  lines_removed INTEGER CHECK (lines_removed IS NULL OR lines_removed >= 0),
  change_stats_extra_json TEXT CHECK (change_stats_extra_json IS NULL OR (json_valid(change_stats_extra_json) AND json_type(change_stats_extra_json) = 'object')),
  FOREIGN KEY (message_key) REFERENCES messages (message_key) ON DELETE CASCADE
) STRICT;

CREATE INDEX run_summaries_by_run
  ON run_summaries (run_id, message_key);

CREATE TABLE run_change_paths (
  message_key INTEGER NOT NULL,
  ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
  path TEXT NOT NULL,
  PRIMARY KEY (message_key, ordinal),
  FOREIGN KEY (message_key) REFERENCES run_summaries (message_key) ON DELETE CASCADE
) STRICT, WITHOUT ROWID;

CREATE TABLE history_edits (
  message_key INTEGER PRIMARY KEY,
  target_message_id TEXT NOT NULL,
  FOREIGN KEY (message_key) REFERENCES messages (message_key) ON DELETE CASCADE
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

CREATE TABLE continuation_requests (
  session_key INTEGER NOT NULL,
  ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
  request_json TEXT NOT NULL CHECK (json_valid(request_json)),
  PRIMARY KEY (session_key, ordinal),
  FOREIGN KEY (session_key) REFERENCES continuations (session_key) ON DELETE CASCADE
) STRICT, WITHOUT ROWID;

CREATE TABLE continuation_steps (
  session_key INTEGER NOT NULL,
  run_id TEXT NOT NULL,
  step INTEGER NOT NULL CHECK (step >= 0),
  ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
  reasoning TEXT NOT NULL DEFAULT '',
  content TEXT NOT NULL DEFAULT '',
  assistant_message_id TEXT,
  interrupted INTEGER NOT NULL DEFAULT 0 CHECK (interrupted IN (0, 1)),
  PRIMARY KEY (session_key, run_id, step),
  UNIQUE (session_key, ordinal),
  FOREIGN KEY (session_key) REFERENCES continuations (session_key) ON DELETE CASCADE
) STRICT, WITHOUT ROWID;

CREATE TABLE continuation_operations (
  session_key INTEGER NOT NULL,
  tool_call_id TEXT NOT NULL,
  ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
  name TEXT NOT NULL,
  run_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('unknown', 'completed')),
  ok INTEGER CHECK (ok IS NULL OR ok IN (0, 1)),
  PRIMARY KEY (session_key, tool_call_id),
  UNIQUE (session_key, ordinal),
  FOREIGN KEY (session_key) REFERENCES continuations (session_key) ON DELETE CASCADE
) STRICT, WITHOUT ROWID;

"""

FTS_SQL = """
CREATE VIEW IF NOT EXISTS messages_fts_source AS
  SELECT m.message_key, COALESCE(m.content, t.result_content) AS content,
         m.content_search, a.reasoning, t.name, e.error_kind,
         (SELECT group_concat(tc.name || ' ' || tc.arguments_json, char(10))
          FROM tool_calls AS tc WHERE tc.message_key = m.message_key) AS tool_calls
  FROM messages AS m
  LEFT JOIN assistant_messages AS a ON a.message_key = m.message_key
  LEFT JOIN tool_messages AS t ON t.message_key = m.message_key
  LEFT JOIN error_messages AS e ON e.message_key = m.message_key
  WHERE m.searchable = 1 AND m.active = 1;

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
  content,
  content_search,
  reasoning,
  name,
  error_kind,
  tool_calls,
  content='messages_fts_source',
  content_rowid='message_key',
  tokenize='unicode61'
);

CREATE TRIGGER IF NOT EXISTS messages_fts_delete BEFORE DELETE ON messages
WHEN old.searchable = 1 AND old.active = 1 BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, content, content_search, reasoning, name, error_kind, tool_calls)
  SELECT 'delete', message_key, content, content_search, reasoning, name, error_kind, tool_calls
  FROM messages_fts_source WHERE message_key = old.message_key;
END;

CREATE VIEW IF NOT EXISTS messages_fts_trigram_source AS
  SELECT message_key, content, content_search, name, error_kind, tool_calls
  FROM messages_fts_source
  WHERE message_key IN (SELECT message_key FROM messages WHERE role <> 'tool');

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts_trigram USING fts5(
  content,
  content_search,
  name,
  error_kind,
  tool_calls,
  content='messages_fts_trigram_source',
  content_rowid='message_key',
  tokenize='trigram'
);

CREATE TRIGGER IF NOT EXISTS messages_fts_trigram_delete BEFORE DELETE ON messages
WHEN old.searchable = 1 AND old.active = 1 AND old.role <> 'tool' BEGIN
  INSERT INTO messages_fts_trigram(messages_fts_trigram, rowid, content, content_search, name, error_kind, tool_calls)
  SELECT 'delete', message_key, content, content_search, name, error_kind, tool_calls
  FROM messages_fts_trigram_source WHERE message_key = old.message_key;
END;
"""

FTS_SQL_FALLBACK = """
CREATE VIEW IF NOT EXISTS messages_fts_source AS
  SELECT m.message_key, COALESCE(m.content, t.result_content) AS content,
         m.content_search, a.reasoning, t.name, e.error_kind,
         (SELECT group_concat(tc.name || ' ' || tc.arguments_json, char(10))
          FROM tool_calls AS tc WHERE tc.message_key = m.message_key) AS tool_calls
  FROM messages AS m
  LEFT JOIN assistant_messages AS a ON a.message_key = m.message_key
  LEFT JOIN tool_messages AS t ON t.message_key = m.message_key
  LEFT JOIN error_messages AS e ON e.message_key = m.message_key
  WHERE m.searchable = 1 AND m.active = 1;

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
  content,
  content_search,
  reasoning,
  name,
  error_kind,
  tool_calls,
  content='messages_fts_source',
  content_rowid='message_key',
  tokenize='unicode61'
);

CREATE TRIGGER IF NOT EXISTS messages_fts_delete BEFORE DELETE ON messages
WHEN old.searchable = 1 AND old.active = 1 BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, content, content_search, reasoning, name, error_kind, tool_calls)
  SELECT 'delete', message_key, content, content_search, reasoning, name, error_kind, tool_calls
  FROM messages_fts_source WHERE message_key = old.message_key;
END;
"""


def _version_tuple(parts: tuple[int, ...]) -> tuple[int, int, int]:
    values = [int(part) for part in parts]
    values.extend([0] * (3 - len(values)))
    return values[0], values[1], values[2]


def is_wal_reset_vulnerable(version_info: tuple[int, ...]) -> bool:
    """Return whether *version_info* carries SQLite's WAL-reset corruption bug.

    Affected are 3.7.0 (2010-07-21) through 3.51.2 (2026-01-09); fixed in
    3.51.3 and later, with source backports in 3.50.7 and 3.44.6
    (https://sqlite.org/wal.html, "The WAL-Reset Bug"). The race needs WAL
    plus two connections writing or checkpointing at the same instant;
    pre-WAL builds cannot hit it.
    """
    info = _version_tuple(version_info)
    if info < (3, 7, 0):
        return False
    return not (
        info >= (3, 51, 3) or (3, 50, 7) <= info < (3, 51, 0) or (3, 44, 6) <= info < (3, 45, 0)
    )


def required_journal_mode(version_info: tuple[int, ...]) -> str:
    """Rollback journal on WAL-reset-vulnerable SQLite, WAL otherwise."""
    if is_wal_reset_vulnerable(version_info):
        return JOURNAL_MODE_DELETE
    return JOURNAL_MODE_WAL


@dataclass(frozen=True)
class DeclaredColumn:
    """One declared column with the type expression ADD COLUMN needs."""

    name: str
    type_name: str
    type_expression: str
    primary_key_position: int
    generated: bool
    required_without_default: bool

    @property
    def addable(self) -> bool:
        """Whether SQLite can add the column without rewriting existing rows."""
        return (
            not self.generated
            and self.primary_key_position == 0
            and not self.required_without_default
        )


@dataclass(frozen=True)
class DeclaredSchema:
    """The schema parsed from its DDL, ready to diff against a live database.

    ``objects`` carries every CREATE statement with its sqlite_master kind and
    name in dependency order; a database missing one by name gets it created
    verbatim. ``unique_index_text`` holds the normalized definition of unique
    indexes, whose shape is a constraint the store relies on.
    """

    objects: tuple[tuple[str, str, str], ...]
    table_columns: dict[str, dict[str, DeclaredColumn]]
    table_constraints: dict[str, tuple[str, ...]]
    table_suffixes: dict[str, str]
    index_text: dict[str, str]
    unique_index_text: dict[str, str]


def _quote_identifier(name: str) -> str:
    return name.replace('"', '""')


def _normalized_ddl(sql: str) -> str:
    return " ".join(sql.split())


def declared_schema(schema_sql: str) -> DeclaredSchema:
    """Parse the declared schema by executing the DDL in an in-memory database.

    SQLite itself resolves every statement shape — defaults, CHECKs, STRICT
    types, generated columns — so the declaration cannot drift from what the
    DDL means, and PRAGMA table_xinfo reports generated columns as hidden.
    The parse runs once per store construction on a three-table schema, so
    its result is not cached.
    """
    reference = sqlite3.connect(":memory:")
    try:
        reference.executescript(schema_sql)
        objects: list[tuple[str, str, str]] = []
        table_columns: dict[str, dict[str, DeclaredColumn]] = {}
        table_constraints: dict[str, tuple[str, ...]] = {}
        table_suffixes: dict[str, str] = {}
        index_text: dict[str, str] = {}
        unique_index_text: dict[str, str] = {}
        for kind, name, sql in reference.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY rowid"
        ):
            if str(name).startswith("sqlite_"):
                continue
            objects.append((str(kind), str(name), str(sql)))
            if kind == "table":
                expressions, constraints, suffix = _table_declaration(str(sql))
                table_columns[str(name)] = {
                    column.name: column
                    for column in _declared_columns(reference, str(name), expressions)
                }
                table_constraints[str(name)] = constraints
                table_suffixes[str(name)] = suffix
            elif kind == "index":
                normalized = _normalized_ddl(str(sql))
                index_text[str(name)] = normalized
                if normalized.upper().startswith("CREATE UNIQUE INDEX"):
                    unique_index_text[str(name)] = normalized
        return DeclaredSchema(
            objects=tuple(objects),
            table_columns=table_columns,
            table_constraints=table_constraints,
            table_suffixes=table_suffixes,
            index_text=index_text,
            unique_index_text=unique_index_text,
        )
    finally:
        reference.close()


def _declared_columns(
    reference: sqlite3.Connection,
    table_name: str,
    expressions: dict[str, str],
) -> list[DeclaredColumn]:
    columns: list[DeclaredColumn] = []
    for row in reference.execute(f'PRAGMA table_xinfo("{_quote_identifier(table_name)}")'):
        # row: (cid, name, type, notnull, dflt_value, pk, hidden)
        _cid, name, type_name, notnull, default, pk, hidden = row
        expression = expressions.get(str(name))
        if expression is None:
            raise SessionStoreCorruptError(
                f"Session schema declaration could not resolve column {table_name}.{name}"
            )
        columns.append(
            DeclaredColumn(
                name=str(name),
                type_name=str(type_name or ""),
                type_expression=expression,
                primary_key_position=int(pk),
                generated=bool(hidden),
                required_without_default=bool(notnull and default is None and not pk),
            )
        )
    return columns


_TABLE_CONSTRAINT_PREFIXES = ("CONSTRAINT", "PRIMARY", "UNIQUE", "CHECK", "FOREIGN")


def _table_declaration(sql: str) -> tuple[dict[str, str], tuple[str, ...], str]:
    """Return exact column expressions, table constraints, and table suffix."""
    open_index = sql.find("(")
    if open_index < 0:
        raise SessionStoreCorruptError("Session schema declaration has no table body")
    close_index = _matching_parenthesis(sql, open_index)
    expressions: dict[str, str] = {}
    constraints: list[str] = []
    for item in _split_sql_items(sql[open_index + 1 : close_index]):
        stripped = item.strip()
        first, remainder = _first_sql_token(stripped)
        if first.upper() in _TABLE_CONSTRAINT_PREFIXES:
            constraints.append(_normalized_ddl(stripped))
            continue
        expressions[first] = remainder.strip()
    return expressions, tuple(constraints), _normalized_ddl(sql[close_index + 1 :].rstrip(";"))


def _matching_parenthesis(sql: str, open_index: int) -> int:
    depth = 0
    quote: str | None = None
    index = open_index
    while index < len(sql):
        character = sql[index]
        if quote is not None:
            if quote == "]" and character == "]":
                quote = None
            elif character == quote:
                if index + 1 < len(sql) and sql[index + 1] == quote:
                    index += 1
                else:
                    quote = None
        elif character in {"'", '"', "`"}:
            quote = character
        elif character == "[":
            quote = "]"
        elif character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    raise SessionStoreCorruptError("Session schema declaration has an unterminated table body")


def _split_sql_items(body: str) -> list[str]:
    items: list[str] = []
    start = 0
    depth = 0
    quote: str | None = None
    index = 0
    while index < len(body):
        character = body[index]
        if quote is not None:
            if quote == "]" and character == "]":
                quote = None
            elif character == quote:
                if index + 1 < len(body) and body[index + 1] == quote:
                    index += 1
                else:
                    quote = None
        elif character in {"'", '"', "`"}:
            quote = character
        elif character == "[":
            quote = "]"
        elif character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
        elif character == "," and depth == 0:
            items.append(body[start:index])
            start = index + 1
        index += 1
    items.append(body[start:])
    return [item for item in items if item.strip()]


def _first_sql_token(item: str) -> tuple[str, str]:
    if not item:
        raise SessionStoreCorruptError("Session schema declaration has an empty table item")
    opening = item[0]
    if opening in {'"', "`", "["}:
        closing = "]" if opening == "[" else opening
        index = 1
        token: list[str] = []
        while index < len(item):
            character = item[index]
            if character == closing:
                if closing != "]" and index + 1 < len(item) and item[index + 1] == closing:
                    token.append(closing)
                    index += 2
                    continue
                return "".join(token), item[index + 1 :]
            token.append(character)
            index += 1
        raise SessionStoreCorruptError("Session schema declaration has an unterminated identifier")
    parts = item.split(None, 1)
    if len(parts) != 2:
        raise SessionStoreCorruptError(f"Session schema declaration lacks a column type: {item}")
    return parts[0], parts[1]


def reconcile_schema(connection: sqlite3.Connection, *, schema_sql: str | None = None) -> list[str]:
    """Bring an existing database up to the declared schema, additively.

    The declared DDL is the single source of truth: missing addable columns
    are added with ADD COLUMN, missing tables, indexes, and triggers are
    created from the parsed DDL text, and a lagging ``PRAGMA user_version``
    is bumped — all in one transaction, so an interrupted reconcile leaves
    no partial state behind.

    Shape changes ADD COLUMN cannot express — a missing generated or
    primary-key column, a column type mismatch, a primary-key mismatch, a
    diverged unique index — raise ``SessionStoreCorruptError`` with an
    offline-conversion instruction instead of serving a stale shape. Extra
    live objects and columns are tolerated.

    Returns the applied changes; empty when the database was already current.
    """
    declared = declared_schema(schema_sql or SCHEMA_SQL)
    applied: list[tuple[str, str]] = []
    for table_name, columns in declared.table_columns.items():
        applied.extend(
            _missing_column_statements(
                table_name,
                columns,
                declared.table_constraints[table_name],
                declared.table_suffixes[table_name],
                connection,
            )
        )
    applied.extend(_missing_object_statements(declared, connection))
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version < SCHEMA_VERSION:
        applied.append(
            (
                f"PRAGMA user_version = {SCHEMA_VERSION};",
                f"schema version {version} -> {SCHEMA_VERSION}",
            )
        )
    if not applied:
        return []
    connection.executescript(
        "BEGIN IMMEDIATE;\n" + "\n".join(statement for statement, _change in applied) + "\nCOMMIT;"
    )
    return [change for _statement, change in applied]


def _missing_column_statements(
    table_name: str,
    columns: dict[str, DeclaredColumn],
    declared_constraints: tuple[str, ...],
    declared_suffix: str,
    connection: sqlite3.Connection,
) -> list[tuple[str, str]]:
    """ADD COLUMN statements for addable gaps; destructive shape fails closed."""
    live_rows = connection.execute(
        f'PRAGMA table_xinfo("{_quote_identifier(table_name)}")'
    ).fetchall()
    if not live_rows:
        # The table is created whole by _missing_object_statements.
        return []
    live_sql_row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table_name,)
    ).fetchone()
    if live_sql_row is None or live_sql_row[0] is None:
        raise SessionStoreCorruptError(
            f"Session database requires offline conversion: {table_name} has no declared SQL"
        )
    live_expressions, live_constraints, live_suffix = _table_declaration(str(live_sql_row[0]))
    if live_constraints != declared_constraints or live_suffix.lower() != declared_suffix.lower():
        raise SessionStoreCorruptError(
            f"Session database requires offline conversion: {table_name} constraints or options "
            "do not match the declared shape"
        )
    live_types = {str(row[1]): str(row[2] or "") for row in live_rows}
    live_generated = {str(row[1]): bool(row[6]) for row in live_rows}
    declared_pk = [
        column.name
        for column in sorted(
            (column for column in columns.values() if column.primary_key_position),
            key=lambda column: column.primary_key_position,
        )
    ]
    live_pk = [
        str(row[1]) for row in sorted((row for row in live_rows if row[5]), key=lambda row: row[5])
    ]
    if declared_pk != live_pk:
        raise SessionStoreCorruptError(
            f"Session database requires offline conversion: {table_name} primary key "
            f"{live_pk} does not match declared {declared_pk}"
        )
    statements: list[tuple[str, str]] = []
    for column in columns.values():
        if column.name in live_types:
            _verify_declared_shape(
                table_name,
                column,
                live_types[column.name],
                live_generated[column.name],
                live_expressions.get(column.name),
            )
            continue
        if not column.addable:
            raise SessionStoreCorruptError(
                f"Session database requires offline conversion: "
                f"{table_name}.{column.name} is missing and cannot be added with ADD COLUMN"
            )
        statements.append(
            (
                f'ALTER TABLE "{_quote_identifier(table_name)}" ADD COLUMN '
                f'"{_quote_identifier(column.name)}" {column.type_expression};',
                f"added column {table_name}.{column.name}",
            )
        )
    return statements


def _verify_declared_shape(
    table_name: str,
    column: DeclaredColumn,
    live_type: str,
    live_generated: bool,
    live_expression: str | None,
) -> None:
    expression_matches = (
        live_expression is not None
        and _normalized_ddl(live_expression).lower()
        == _normalized_ddl(column.type_expression).lower()
    )
    if (
        column.type_name.lower() != live_type.lower()
        or column.generated != live_generated
        or not expression_matches
    ):
        raise SessionStoreCorruptError(
            f"Session database requires offline conversion: {table_name}.{column.name} "
            f"does not match the declared shape (live type {live_type or 'none'}, "
            f"generated {live_generated}; declared type {column.type_name or 'none'}, "
            f"generated {column.generated})"
        )


def _missing_object_statements(
    declared: DeclaredSchema, connection: sqlite3.Connection
) -> list[tuple[str, str]]:
    """CREATE statements for declared objects the database lacks by name."""
    live_names = {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE sql IS NOT NULL")
    }
    live_index_text = {
        str(row[0]): str(row[1] or "")
        for row in connection.execute("SELECT name, sql FROM sqlite_master WHERE type = 'index'")
    }
    statements: list[tuple[str, str]] = []
    for kind, name, sql in declared.objects:
        if name not in live_names:
            statements.append((f"{sql};", f"created {kind} {name}"))
            continue
        if kind == "index":
            declared_text = declared.index_text[name]
            live_text = _normalized_ddl(live_index_text.get(name, ""))
            if live_text == declared_text:
                continue
            if name in declared.unique_index_text:
                _verify_unique_index_shape(name, declared_text, live_text)
            statements.extend(
                (
                    (f'DROP INDEX "{_quote_identifier(name)}";', f"dropped stale index {name}"),
                    (f"{sql};", f"recreated index {name}"),
                )
            )
    return statements


def _verify_unique_index_shape(name: str, declared_text: str, live_text: str) -> None:
    if _normalized_ddl(live_text) != declared_text:
        raise SessionStoreCorruptError(
            f"Session database requires offline conversion: unique index "
            f"{name} does not match the declared definition"
        )
