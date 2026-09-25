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
FTS_TABLE = "entries_fts"
FTS_VIEW = "entries_fts_source"
FTS_TRIGRAM_TABLE = "entries_fts_trigram"
FTS_TRIGRAM_VIEW = "entries_fts_trigram_source"
FTS_STALE_KEY = "fts_stale"
FTS_GENERATION_KEY = "fts_rebuild_generation"
FTS_TARGET_HIGH_WATER_KEY = "fts_rebuild_target_high_water"
FTS_COMPLETED_HIGH_WATER_KEY = "fts_rebuild_completed_high_water"
FTS_DEGRADED_REASON_KEY = "fts_degraded_reason"
FTS_STORAGE_VERSION_KEY = "fts_storage_version"
FTS_STORAGE_VERSION = 1
# Entry roles whose text the trigram index covers: conversation text only.
FTS_TRIGRAM_ROLES = ("user", "assistant", "compaction_checkpoint")

# Generation 1 follows the evolution rules of the persistence contract: no enum
# CHECKs on extensible vocabularies (roles, kinds, statuses, causes), narrow hot
# tables with large values in 1:1 side tables, integer surrogate keys for
# internal relations, JSON only for open payloads, canonical UTC timestamps
# (``YYYY-MM-DDTHH:MM:SS.ffffffZ``), AUTOINCREMENT for keys that feed derived
# indexes or paging cursors, and every index named by the query it serves.
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
  state TEXT NOT NULL,
  created_at TEXT NOT NULL,
  archived_at TEXT,
  next_seq INTEGER NOT NULL DEFAULT 0 CHECK (next_seq >= 0),
  history_revision INTEGER NOT NULL DEFAULT 0 CHECK (history_revision >= 0),
  state_revision INTEGER NOT NULL DEFAULT 0 CHECK (state_revision >= 0),
  cursor_floor_seq INTEGER NOT NULL DEFAULT 0 CHECK (cursor_floor_seq >= 0),
  last_activity_at TEXT NOT NULL,
  last_entry_id TEXT,
  fork_parent_key INTEGER REFERENCES sessions (session_key) ON DELETE SET NULL,
  forked_at TEXT,
  fork_point_seq INTEGER CHECK (fork_point_seq IS NULL OR fork_point_seq >= 0),
  title TEXT,
  auto_title TEXT,
  auto_title_initialized INTEGER NOT NULL DEFAULT 0 CHECK (auto_title_initialized IN (0, 1)),
  source_channel_id TEXT,
  platform TEXT,
  platform_conv_id TEXT,
  is_subagent INTEGER NOT NULL DEFAULT 0 CHECK (is_subagent IN (0, 1)),
  subagent_parent_id TEXT,
  subagent_parent_project_id TEXT,
  subagent_parent_agent_id TEXT,
  subagent_parent_session_id TEXT,
  subagent_parent_run_id TEXT,
  subagent_parent_tool_call_id TEXT,
  subagent_parent_tool_call_index INTEGER CHECK (subagent_parent_tool_call_index IS NULL OR subagent_parent_tool_call_index >= 0),
  list_visibility_mask INTEGER NOT NULL DEFAULT 0 CHECK (list_visibility_mask >= 0),
  latest_completion_run_id TEXT,
  latest_completion_status TEXT,
  latest_completion_at TEXT,
  read_completion_run_id TEXT,
  prompt_cache_affinity_id TEXT,
  seen_skills_initialized INTEGER NOT NULL DEFAULT 0 CHECK (seen_skills_initialized IN (0, 1)),
  compaction_policy_json TEXT CHECK (compaction_policy_json IS NULL OR (json_valid(compaction_policy_json) AND json_type(compaction_policy_json) = 'object')),
  metadata_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(metadata_json) AND json_type(metadata_json) = 'object'),
  CHECK ((latest_completion_run_id IS NULL) = (latest_completion_status IS NULL)),
  CHECK ((forked_at IS NULL) = (fork_point_seq IS NULL)),
  CHECK ((fork_parent_key IS NULL) OR (forked_at IS NOT NULL))
) STRICT;

-- Address lookups of the live generation (every address-based read and write).
CREATE UNIQUE INDEX sessions_one_live_address
  ON sessions (project_id, agent_id, session_id)
  WHERE state = 'live';

-- Archived generations of one address (id allocation, restore).
CREATE INDEX sessions_archived_address
  ON sessions (project_id, agent_id, session_id)
  WHERE state = 'archived';

-- Session lists of one scope, newest activity first.
CREATE INDEX sessions_live_scope_order
  ON sessions (project_id, agent_id, last_activity_at DESC, session_id)
  WHERE state = 'live';

-- Session list pages across scopes, newest activity first.
CREATE INDEX sessions_live_global_order
  ON sessions (last_activity_at DESC, project_id, agent_id, session_id)
  WHERE state = 'live';

-- Visibility-filtered Session list counts of one scope.
CREATE INDEX sessions_live_scope_visibility
  ON sessions (project_id, agent_id, list_visibility_mask)
  WHERE state = 'live';

-- Forks of one Session (reflection Runs, fork provenance, the SET NULL action).
CREATE INDEX sessions_by_fork_parent
  ON sessions (fork_parent_key)
  WHERE fork_parent_key IS NOT NULL;

CREATE TABLE session_run_kinds (
  session_key INTEGER NOT NULL REFERENCES sessions (session_key) ON DELETE CASCADE,
  run_kind TEXT NOT NULL,
  PRIMARY KEY (session_key, run_kind)
) STRICT, WITHOUT ROWID;

-- A fork's inherited history, one row per seq segment: the entries of
-- ancestor_key with from_seq <= seq < upto_seq that were current when the
-- ancestor's history reached as_of_seq. Segments of one Session are disjoint
-- and flattened, so a fork of a fork names every ancestor directly; the
-- Session's own entries fill the seqs no segment covers.
CREATE TABLE session_lineage (
  session_key INTEGER NOT NULL REFERENCES sessions (session_key) ON DELETE CASCADE,
  from_seq INTEGER NOT NULL CHECK (from_seq >= 0),
  ancestor_key INTEGER NOT NULL REFERENCES sessions (session_key) ON DELETE RESTRICT,
  upto_seq INTEGER NOT NULL,
  as_of_seq INTEGER NOT NULL,
  PRIMARY KEY (session_key, from_seq),
  CHECK (from_seq < upto_seq AND upto_seq <= as_of_seq)
) STRICT, WITHOUT ROWID;

-- Descendants of one ancestor (materialization, search attribution, RESTRICT).
CREATE INDEX session_lineage_by_ancestor
  ON session_lineage (ancestor_key);

CREATE TABLE prompt_blobs (
  blob_key INTEGER PRIMARY KEY,
  sha256 TEXT NOT NULL UNIQUE,
  value_json TEXT NOT NULL CHECK (json_valid(value_json) AND json_type(value_json) = 'object')
) STRICT;

CREATE TABLE session_prompt_pins (
  session_key INTEGER NOT NULL REFERENCES sessions (session_key) ON DELETE CASCADE,
  slot TEXT NOT NULL,
  blob_key INTEGER NOT NULL REFERENCES prompt_blobs (blob_key),
  PRIMARY KEY (session_key, slot)
) STRICT, WITHOUT ROWID;

-- Whether a prompt blob is still referenced (unreferenced blobs are deleted).
CREATE INDEX session_prompt_pins_by_blob
  ON session_prompt_pins (blob_key);

CREATE TABLE session_seen_skills (
  session_key INTEGER NOT NULL REFERENCES sessions (session_key) ON DELETE CASCADE,
  skill_name TEXT NOT NULL,
  PRIMARY KEY (session_key, skill_name)
) STRICT, WITHOUT ROWID;

CREATE TABLE runs (
  run_key INTEGER PRIMARY KEY,
  session_key INTEGER NOT NULL REFERENCES sessions (session_key) ON DELETE CASCADE,
  run_id TEXT NOT NULL,
  work_id TEXT,
  run_kind TEXT NOT NULL,
  contributes_to_activity INTEGER NOT NULL CHECK (contributes_to_activity IN (0, 1)),
  inherited INTEGER NOT NULL DEFAULT 0 CHECK (inherited IN (0, 1)),
  status TEXT NOT NULL,
  started_at TEXT NOT NULL,
  start_seq INTEGER NOT NULL CHECK (start_seq >= 0),
  completed_at TEXT,
  duration_ms INTEGER CHECK (duration_ms IS NULL OR duration_ms >= 0),
  timing_started_at TEXT,
  end_entry_key INTEGER UNIQUE REFERENCES entries (entry_key),
  completion_reason TEXT,
  iteration_count INTEGER CHECK (iteration_count IS NULL OR iteration_count >= 0),
  changed_files INTEGER CHECK (changed_files IS NULL OR changed_files >= 0),
  lines_added INTEGER CHECK (lines_added IS NULL OR lines_added >= 0),
  lines_removed INTEGER CHECK (lines_removed IS NULL OR lines_removed >= 0),
  timing_extra_json TEXT CHECK (timing_extra_json IS NULL OR (json_valid(timing_extra_json) AND json_type(timing_extra_json) = 'object')),
  change_stats_extra_json TEXT CHECK (change_stats_extra_json IS NULL OR (json_valid(change_stats_extra_json) AND json_type(change_stats_extra_json) = 'object')),
  UNIQUE (session_key, run_id),
  CHECK ((status = 'running') = (completed_at IS NULL))
) STRICT;

-- Runs of one Session in start order (Run boundaries, reflection Runs, recovery).
CREATE INDEX runs_by_session ON runs (session_key, start_seq);

-- Run lookups by Work id (Run summaries and results of delegated work).
CREATE INDEX runs_by_work ON runs (session_key, work_id) WHERE work_id IS NOT NULL;

CREATE TABLE run_change_paths (
  run_key INTEGER NOT NULL REFERENCES runs (run_key) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
  path TEXT NOT NULL,
  PRIMARY KEY (run_key, ordinal)
) STRICT, WITHOUT ROWID;

-- One row per history item of one Session generation, in seq order. An edit
-- supersedes the replaced tail at the edit's seq; a history_edit marker is
-- superseded at its own seq, so no current view contains it.
CREATE TABLE entries (
  entry_key INTEGER PRIMARY KEY AUTOINCREMENT,
  session_key INTEGER NOT NULL REFERENCES sessions (session_key) ON DELETE CASCADE,
  seq INTEGER NOT NULL CHECK (seq >= 0),
  role TEXT NOT NULL,
  entry_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  run_key INTEGER REFERENCES runs (run_key),
  model TEXT,
  searchable INTEGER NOT NULL CHECK (searchable IN (0, 1)),
  superseded_at_seq INTEGER CHECK (superseded_at_seq IS NULL OR superseded_at_seq >= seq),
  UNIQUE (session_key, seq)
) STRICT;

-- Entries of one Run (Run messages, results and summaries; the runs FK).
CREATE INDEX entries_by_run ON entries (run_key, seq) WHERE run_key IS NOT NULL;

-- Entry lookups by public Message id (edit targets, anchors, Recall context).
CREATE INDEX entries_by_id ON entries (session_key, entry_id);

-- Newest or counted entries of one role (latest note, User counts, checkpoints).
CREATE INDEX entries_by_role ON entries (session_key, role, seq);

CREATE TABLE entry_text (
  entry_key INTEGER PRIMARY KEY REFERENCES entries (entry_key) ON DELETE CASCADE,
  content TEXT,
  blocks_json TEXT CHECK (blocks_json IS NULL OR (json_valid(blocks_json) AND json_type(blocks_json) = 'array')),
  search_text TEXT,
  CHECK (content IS NULL OR blocks_json IS NULL),
  CHECK ((blocks_json IS NULL) = (search_text IS NULL))
) STRICT;

CREATE TABLE assistant_entries (
  entry_key INTEGER PRIMARY KEY REFERENCES entries (entry_key) ON DELETE CASCADE,
  phase TEXT,
  reasoning_scope TEXT,
  has_tool_calls INTEGER NOT NULL DEFAULT 0 CHECK (has_tool_calls IN (0, 1)),
  interrupted INTEGER NOT NULL DEFAULT 0 CHECK (interrupted IN (0, 1)),
  interruption_cause TEXT,
  usage_present INTEGER NOT NULL DEFAULT 0 CHECK (usage_present IN (0, 1)),
  input_tokens INTEGER CHECK (input_tokens IS NULL OR input_tokens >= 0),
  output_tokens INTEGER CHECK (output_tokens IS NULL OR output_tokens >= 0),
  cache_read_tokens INTEGER CHECK (cache_read_tokens IS NULL OR cache_read_tokens >= 0),
  cache_write_tokens INTEGER CHECK (cache_write_tokens IS NULL OR cache_write_tokens >= 0),
  reasoning_tokens INTEGER CHECK (reasoning_tokens IS NULL OR reasoning_tokens >= 0),
  usage_estimated INTEGER CHECK (usage_estimated IS NULL OR usage_estimated IN (0, 1)),
  input_tokens_estimated INTEGER CHECK (input_tokens_estimated IS NULL OR input_tokens_estimated IN (0, 1)),
  output_tokens_estimated INTEGER CHECK (output_tokens_estimated IS NULL OR output_tokens_estimated IN (0, 1)),
  reasoning_started_at TEXT,
  reasoning_completed_at TEXT,
  reasoning_duration_ms INTEGER CHECK (reasoning_duration_ms IS NULL OR reasoning_duration_ms >= 0),
  usage_extra_json TEXT CHECK (usage_extra_json IS NULL OR (json_valid(usage_extra_json) AND json_type(usage_extra_json) = 'object')),
  reasoning_timing_extra_json TEXT CHECK (reasoning_timing_extra_json IS NULL OR (json_valid(reasoning_timing_extra_json) AND json_type(reasoning_timing_extra_json) = 'object'))
) STRICT;

CREATE TABLE assistant_reasoning (
  entry_key INTEGER PRIMARY KEY REFERENCES entries (entry_key) ON DELETE CASCADE,
  reasoning TEXT,
  summary_json TEXT CHECK (summary_json IS NULL OR (json_valid(summary_json) AND json_type(summary_json) = 'array')),
  meta_json TEXT CHECK (meta_json IS NULL OR (json_valid(meta_json) AND json_type(meta_json) = 'object'))
) STRICT;

CREATE TABLE assistant_output_files (
  entry_key INTEGER NOT NULL REFERENCES entries (entry_key) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
  path TEXT NOT NULL,
  line_index INTEGER NOT NULL CHECK (line_index >= 0),
  start_index INTEGER CHECK (start_index IS NULL OR start_index >= 0),
  end_index INTEGER CHECK (end_index IS NULL OR end_index > 0),
  CHECK ((start_index IS NULL) = (end_index IS NULL)),
  PRIMARY KEY (entry_key, ordinal)
) STRICT, WITHOUT ROWID;

CREATE TABLE tool_calls (
  call_key INTEGER PRIMARY KEY,
  entry_key INTEGER NOT NULL REFERENCES entries (entry_key) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
  call_id TEXT NOT NULL,
  name TEXT NOT NULL,
  status TEXT NOT NULL,
  result_entry_key INTEGER UNIQUE REFERENCES entries (entry_key),
  result_ok INTEGER CHECK (result_ok IS NULL OR result_ok IN (0, 1)),
  error_code TEXT,
  error_retryable INTEGER CHECK (error_retryable IS NULL OR error_retryable IN (0, 1)),
  error_attempts INTEGER CHECK (error_attempts IS NULL OR error_attempts >= 0),
  started_at TEXT,
  completed_at TEXT,
  duration_ms INTEGER CHECK (duration_ms IS NULL OR duration_ms >= 0),
  rejection_code TEXT,
  rejection_fingerprint TEXT,
  argument_sequence_index INTEGER CHECK (argument_sequence_index IS NULL OR argument_sequence_index >= 0),
  argument_sequence_length INTEGER CHECK (argument_sequence_length IS NULL OR argument_sequence_length > 1),
  CHECK ((rejection_code IS NULL) = (rejection_fingerprint IS NULL)),
  CHECK ((argument_sequence_index IS NULL) = (argument_sequence_length IS NULL)),
  UNIQUE (entry_key, ordinal)
) STRICT;

-- Tool calls by public call id (Tool result correlation, persisted-result checks).
CREATE INDEX tool_calls_by_call_id ON tool_calls (call_id);

CREATE TABLE tool_call_payloads (
  call_key INTEGER PRIMARY KEY REFERENCES tool_calls (call_key) ON DELETE CASCADE,
  arguments_json TEXT NOT NULL CHECK (json_valid(arguments_json) AND json_type(arguments_json) = 'object'),
  rejection_message TEXT,
  display_json TEXT CHECK (display_json IS NULL OR (json_valid(display_json) AND json_type(display_json) = 'object')),
  timing_extra_json TEXT CHECK (timing_extra_json IS NULL OR (json_valid(timing_extra_json) AND json_type(timing_extra_json) = 'object'))
) STRICT;

-- Result payloads an Extension attached to one Tool call, loaded by payload id
-- through the calling Session's current view. A materialized copy keeps the
-- payload id, so one id can name payloads in several Sessions.
CREATE TABLE tool_result_payloads (
  payload_key INTEGER PRIMARY KEY,
  payload_id TEXT NOT NULL,
  call_key INTEGER NOT NULL REFERENCES tool_calls (call_key) ON DELETE CASCADE,
  owner_name TEXT NOT NULL,
  created_at TEXT NOT NULL,
  payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
  UNIQUE (call_key, payload_id)
) STRICT;

-- Payload loads by public payload id.
CREATE INDEX tool_result_payloads_by_id ON tool_result_payloads (payload_id);

CREATE TABLE user_entry_senders (
  entry_key INTEGER PRIMARY KEY REFERENCES entries (entry_key) ON DELETE CASCADE,
  sender_id TEXT NOT NULL,
  display_name TEXT NOT NULL,
  sender_role TEXT NOT NULL
) STRICT;

CREATE TABLE error_entries (
  entry_key INTEGER PRIMARY KEY REFERENCES entries (entry_key) ON DELETE CASCADE,
  error_kind TEXT NOT NULL
) STRICT;

CREATE TABLE history_edit_entries (
  entry_key INTEGER PRIMARY KEY REFERENCES entries (entry_key) ON DELETE CASCADE,
  target_entry_id TEXT NOT NULL
) STRICT;

CREATE TABLE checkpoint_entries (
  entry_key INTEGER PRIMARY KEY REFERENCES entries (entry_key) ON DELETE CASCADE,
  policy TEXT NOT NULL,
  strategy TEXT NOT NULL,
  compacted_token_count INTEGER CHECK (compacted_token_count IS NULL OR compacted_token_count >= 0),
  context_tokens_before INTEGER CHECK (context_tokens_before IS NULL OR context_tokens_before >= 0),
  context_tokens_after INTEGER CHECK (context_tokens_after IS NULL OR context_tokens_after >= 0),
  duration_ms INTEGER CHECK (duration_ms IS NULL OR duration_ms >= 0),
  usage_present INTEGER NOT NULL DEFAULT 0 CHECK (usage_present IN (0, 1)),
  usage_extra_json TEXT CHECK (usage_extra_json IS NULL OR (json_valid(usage_extra_json) AND json_type(usage_extra_json) = 'object')),
  CHECK ((context_tokens_before IS NULL) = (context_tokens_after IS NULL))
) STRICT;

CREATE TABLE checkpoint_projections (
  entry_key INTEGER PRIMARY KEY REFERENCES entries (entry_key) ON DELETE CASCADE,
  projection_json TEXT NOT NULL CHECK (json_valid(projection_json) AND json_type(projection_json) = 'array')
) STRICT;

CREATE TABLE continuations (
  session_key INTEGER PRIMARY KEY REFERENCES sessions (session_key) ON DELETE CASCADE,
  checkpoint_id TEXT NOT NULL,
  origin_run_key INTEGER NOT NULL,
  latest_run_key INTEGER NOT NULL,
  cause TEXT,
  active INTEGER NOT NULL CHECK (active IN (0, 1)),
  CHECK ((active = 1) = (cause IS NULL))
) STRICT;

CREATE TABLE continuation_requests (
  session_key INTEGER NOT NULL REFERENCES continuations (session_key) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
  request_json TEXT NOT NULL CHECK (json_valid(request_json)),
  PRIMARY KEY (session_key, ordinal)
) STRICT, WITHOUT ROWID;

CREATE TABLE continuation_steps (
  session_key INTEGER NOT NULL REFERENCES continuations (session_key) ON DELETE CASCADE,
  run_key INTEGER NOT NULL,
  step INTEGER NOT NULL CHECK (step >= 1),
  ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
  assistant_message_id TEXT,
  interrupted INTEGER NOT NULL DEFAULT 0 CHECK (interrupted IN (0, 1)),
  PRIMARY KEY (session_key, run_key, step),
  UNIQUE (session_key, ordinal)
) STRICT, WITHOUT ROWID;

CREATE TABLE continuation_step_chunks (
  chunk_key INTEGER PRIMARY KEY,
  session_key INTEGER NOT NULL,
  run_key INTEGER NOT NULL,
  step INTEGER NOT NULL,
  reasoning_delta TEXT NOT NULL DEFAULT '',
  content_delta TEXT NOT NULL DEFAULT '',
  FOREIGN KEY (session_key, run_key, step)
    REFERENCES continuation_steps (session_key, run_key, step) ON DELETE CASCADE
) STRICT;

-- Chunks of one step in append order (recovery reads, boundary folds, the FK).
CREATE INDEX continuation_step_chunks_by_step
  ON continuation_step_chunks (session_key, run_key, step, chunk_key);

CREATE TABLE continuation_operations (
  session_key INTEGER NOT NULL REFERENCES continuations (session_key) ON DELETE CASCADE,
  tool_call_id TEXT NOT NULL,
  ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
  name TEXT NOT NULL,
  run_key INTEGER NOT NULL,
  status TEXT NOT NULL,
  ok INTEGER CHECK (ok IS NULL OR ok IN (0, 1)),
  PRIMARY KEY (session_key, tool_call_id),
  UNIQUE (session_key, ordinal)
) STRICT, WITHOUT ROWID;

CREATE TABLE temporary_session_bindings (
  session_key INTEGER PRIMARY KEY REFERENCES sessions (session_key) ON DELETE CASCADE,
  owner_name TEXT NOT NULL,
  group_id TEXT NOT NULL,
  participant_id TEXT NOT NULL,
  config_json TEXT NOT NULL CHECK (json_valid(config_json) AND json_type(config_json) = 'object'),
  UNIQUE (owner_name, group_id, participant_id)
) STRICT;

CREATE TABLE temporary_group_titles (
  owner_name TEXT NOT NULL,
  group_id TEXT NOT NULL,
  title TEXT NOT NULL CHECK (length(title) > 0),
  PRIMARY KEY (owner_name, group_id)
) STRICT, WITHOUT ROWID;

CREATE TABLE session_delivery_receipts (
  session_key INTEGER NOT NULL REFERENCES sessions (session_key) ON DELETE CASCADE,
  owner_name TEXT NOT NULL,
  receipt_id TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  effect_kind TEXT NOT NULL,
  carrier_kind TEXT NOT NULL,
  carrier_sequence INTEGER NOT NULL CHECK (carrier_sequence >= 0),
  PRIMARY KEY (session_key, owner_name, receipt_id)
) STRICT, WITHOUT ROWID;

CREATE TABLE run_execution_owners (
  record_key INTEGER PRIMARY KEY AUTOINCREMENT,
  run_key INTEGER NOT NULL UNIQUE REFERENCES runs (run_key) ON DELETE CASCADE,
  session_key INTEGER NOT NULL REFERENCES sessions (session_key) ON DELETE CASCADE,
  run_id TEXT NOT NULL,
  input_id TEXT,
  owner_name TEXT NOT NULL,
  group_id TEXT NOT NULL,
  participant_id TEXT NOT NULL,
  participant_generation_id TEXT NOT NULL,
  epoch TEXT NOT NULL,
  UNIQUE (session_key, input_id)
) STRICT;

-- One owner group's execution records in admission order (owned Run pages).
CREATE INDEX run_execution_owners_group
  ON run_execution_owners (owner_name, group_id, record_key);

-- Exact Run ids of one owner group (one probe per id, independent of history).
CREATE INDEX run_execution_owners_group_run
  ON run_execution_owners (owner_name, group_id, run_id);
"""

# An entry belongs to the search indexes while it is searchable and current in
# some Session's view: its owner's, or a fork's that still inherits it after
# the owner superseded it. Both indexes and their coverage checks share it.
FTS_MEMBERSHIP_SQL = (
    "e.searchable = 1 AND (e.superseded_at_seq IS NULL OR EXISTS ("
    "SELECT 1 FROM session_lineage AS l WHERE l.ancestor_key = e.session_key "
    "AND e.seq >= l.from_seq AND e.seq < l.upto_seq "
    "AND e.superseded_at_seq >= l.as_of_seq))"
)
FTS_TRIGRAM_MEMBERSHIP_SQL = (
    f"{FTS_MEMBERSHIP_SQL} AND e.role IN ({', '.join(repr(role) for role in FTS_TRIGRAM_ROLES)}) "
    "AND EXISTS (SELECT 1 FROM entry_text AS x WHERE x.entry_key = e.entry_key)"
)

# The standard index holds text, User block text, Assistant reasoning, the Tool
# name of a result, the error kind and the Tool calls (name and arguments) of an
# Assistant entry.
_FTS_STANDARD_SQL = f"""
CREATE VIEW IF NOT EXISTS entries_fts_source AS
  SELECT e.entry_key, t.content, t.search_text, r.reasoning, c.name, x.error_kind,
         (SELECT group_concat(k.name || ' ' || p.arguments_json, char(10))
          FROM tool_calls AS k JOIN tool_call_payloads AS p ON p.call_key = k.call_key
          WHERE k.entry_key = e.entry_key) AS tool_calls
  FROM entries AS e
  LEFT JOIN entry_text AS t ON t.entry_key = e.entry_key
  LEFT JOIN assistant_reasoning AS r ON r.entry_key = e.entry_key
  LEFT JOIN tool_calls AS c ON c.result_entry_key = e.entry_key
  LEFT JOIN error_entries AS x ON x.entry_key = e.entry_key
  WHERE {FTS_MEMBERSHIP_SQL};

CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5(
  content,
  search_text,
  reasoning,
  name,
  error_kind,
  tool_calls,
  content='entries_fts_source',
  content_rowid='entry_key',
  tokenize='unicode61'
);
"""

# The trigram index covers conversation text only: User text, visible
# Assistant text and Compaction checkpoint summaries.
_FTS_TRIGRAM_SQL = f"""
CREATE VIEW IF NOT EXISTS entries_fts_trigram_source AS
  SELECT e.entry_key, t.content, t.search_text
  FROM entries AS e JOIN entry_text AS t ON t.entry_key = e.entry_key
  WHERE {FTS_TRIGRAM_MEMBERSHIP_SQL};

CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts_trigram USING fts5(
  content,
  search_text,
  content='entries_fts_trigram_source',
  content_rowid='entry_key',
  tokenize='trigram'
);
"""

FTS_SQL = _FTS_STANDARD_SQL + _FTS_TRIGRAM_SQL
# SQLite builds without the trigram tokenizer keep the standard index only.
FTS_SQL_FALLBACK = _FTS_STANDARD_SQL
