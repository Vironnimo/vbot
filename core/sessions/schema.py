"""Declarative schema and identity of the canonical SQLite Session database.

The shared database kernel (``core.database``) opens, evolves, verifies and
snapshots it; this module only declares what it contains.
"""
# ruff: noqa: E501

from __future__ import annotations

DATABASE_NAME = "sessions"
APPLICATION_ID = 0x56425353  # "VBSS"
# The physical format generation, not a counter for additive changes: those
# reconcile in place. Raise it only for a change a converter must perform.
FORMAT_GENERATION = 1

# FTS declarative constants — kept testable and in one place.
FTS_TABLE = "messages_fts"
FTS_VIEW = "messages_fts_source"
FTS_TRIGRAM_TABLE = "messages_fts_trigram"
FTS_TRIGRAM_VIEW = "messages_fts_trigram_source"
FTS_TRIGGERS: tuple[str, ...] = ()
FTS_STALE_KEY = "fts_stale"
FTS_GENERATION_KEY = "fts_rebuild_generation"
FTS_TARGET_HIGH_WATER_KEY = "fts_rebuild_target_high_water"
FTS_COMPLETED_HIGH_WATER_KEY = "fts_rebuild_completed_high_water"
FTS_DEGRADED_REASON_KEY = "fts_degraded_reason"
# Kept as aliases for callers that only need the target/progress distinction.
FTS_HIGH_WATER_KEY = FTS_TARGET_HIGH_WATER_KEY
FTS_PROGRESS_KEY = FTS_COMPLETED_HIGH_WATER_KEY
FTS_STORAGE_VERSION_KEY = "fts_storage_version"
FTS_STORAGE_VERSION = 1
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

CREATE INDEX sessions_live_scope_order
  ON sessions (project_id, agent_id, active_sort DESC, session_id)
  WHERE status = 'live';

CREATE INDEX sessions_live_global_order
  ON sessions (active_sort DESC, project_id, agent_id, session_id)
  WHERE status = 'live';

CREATE INDEX sessions_live_scope_visibility
  ON sessions (project_id, agent_id, list_visibility_mask)
  WHERE status = 'live';

CREATE INDEX sessions_live_fork_source
  ON sessions (project_id, agent_id, json_extract(fork_source_json, '$.session_id'))
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

CREATE INDEX temporary_session_bindings_owner_group
  ON temporary_session_bindings (owner_name, group_id, participant_id);

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

CREATE INDEX session_delivery_receipts_session
  ON session_delivery_receipts (session_key, owner_name, receipt_id);

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

CREATE INDEX run_execution_owners_group
  ON run_execution_owners (owner_name, group_id, record_key);

CREATE INDEX run_execution_owners_group_run
  ON run_execution_owners (owner_name, group_id, run_id);

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

CREATE INDEX runs_by_session ON runs(session_key, start_sequence);
CREATE INDEX runs_by_terminal ON runs(session_key, run_id, terminal_sequence);

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

CREATE INDEX messages_by_run ON messages(session_key, run_id, seq);

CREATE INDEX messages_by_session_time
  ON messages (session_key, timestamp, seq);

CREATE INDEX messages_by_session_instant
  ON messages (session_key, julianday(timestamp), seq);

CREATE INDEX messages_by_message_id
  ON messages (session_key, message_id);

CREATE INDEX messages_active_by_session
  ON messages (session_key, seq)
  WHERE active = 1;

CREATE INDEX messages_by_session_role_sequence
  ON messages (session_key, role, seq DESC);

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
  CHECK ((rejection_code IS NULL) = (rejection_message IS NULL) AND (rejection_code IS NULL) = (rejection_fingerprint IS NULL)),
  CHECK ((argument_sequence_index IS NULL) = (argument_sequence_length IS NULL)),
  CHECK ((result_id IS NULL) = (result_sequence IS NULL)),
  CHECK ((result_id IS NULL) = (result_content IS NULL)),
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

CREATE INDEX compaction_checkpoints_by_session
  ON compaction_checkpoints (session_key, seq);

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

CREATE INDEX history_edits_by_session
  ON history_edits (session_key, seq);

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

FTS_SQL = """
CREATE VIEW IF NOT EXISTS messages_fts_source AS
  SELECT m.message_key, COALESCE(m.content, t.result_content) AS content,
         m.content_search, a.reasoning, t.name, e.error_kind,
         (SELECT group_concat(tc.name || ' ' || tc.arguments_json, char(10))
          FROM tool_calls AS tc WHERE tc.message_key = m.source_key) AS tool_calls
  FROM history_records AS m
  LEFT JOIN assistant_messages AS a ON a.message_key = m.source_key
  LEFT JOIN tool_calls AS t ON t.result_key = m.message_key
  LEFT JOIN error_messages AS e ON e.message_key = m.source_key
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


CREATE VIEW IF NOT EXISTS messages_fts_trigram_source AS
  SELECT message_key, content, content_search, name, error_kind, tool_calls
  FROM messages_fts_source
  WHERE message_key IN (SELECT message_key FROM history_records WHERE role <> 'tool');

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

"""

FTS_SQL_FALLBACK = """
CREATE VIEW IF NOT EXISTS messages_fts_source AS
  SELECT m.message_key, COALESCE(m.content, t.result_content) AS content,
         m.content_search, a.reasoning, t.name, e.error_kind,
         (SELECT group_concat(tc.name || ' ' || tc.arguments_json, char(10))
          FROM tool_calls AS tc WHERE tc.message_key = m.source_key) AS tool_calls
  FROM history_records AS m
  LEFT JOIN assistant_messages AS a ON a.message_key = m.source_key
  LEFT JOIN tool_calls AS t ON t.result_key = m.message_key
  LEFT JOIN error_messages AS e ON e.message_key = m.source_key
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

"""
