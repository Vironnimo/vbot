"""Immutable v1 schema for the canonical SQLite Session store."""
# ruff: noqa: E501

from __future__ import annotations

SCHEMA_VERSION = 1
MINIMUM_SQLITE_VERSION = (3, 37, 0)

SCHEMA_SQL = """
CREATE TABLE sessions (
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
  activity_json TEXT CHECK (activity_json IS NULL OR (json_valid(activity_json) AND json_type(activity_json) = 'object')),
  PRIMARY KEY (project_id, agent_id, session_id)
) STRICT, WITHOUT ROWID;

CREATE INDEX sessions_live_scope_order
  ON sessions (project_id, agent_id, last_message_at DESC, session_id)
  WHERE status = 'live';

CREATE TABLE messages (
  project_id TEXT NOT NULL,
  agent_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  seq INTEGER NOT NULL CHECK (seq >= 0),
  message_id TEXT NOT NULL,
  role TEXT NOT NULL,
  timestamp TEXT NOT NULL,
  message_json TEXT NOT NULL CHECK (json_valid(message_json) AND json_type(message_json) = 'object'),
  PRIMARY KEY (project_id, agent_id, session_id, seq),
  UNIQUE (project_id, agent_id, session_id, message_id),
  FOREIGN KEY (project_id, agent_id, session_id)
    REFERENCES sessions (project_id, agent_id, session_id)
    ON DELETE CASCADE ON UPDATE CASCADE
) STRICT, WITHOUT ROWID;

CREATE INDEX messages_by_session_time
  ON messages (project_id, agent_id, session_id, timestamp, seq);

CREATE TABLE continuation_records (
  project_id TEXT NOT NULL,
  agent_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  seq INTEGER NOT NULL CHECK (seq >= 0),
  record_json TEXT NOT NULL CHECK (json_valid(record_json) AND json_type(record_json) = 'object'),
  PRIMARY KEY (project_id, agent_id, session_id, seq),
  FOREIGN KEY (project_id, agent_id, session_id)
    REFERENCES sessions (project_id, agent_id, session_id)
    ON DELETE CASCADE ON UPDATE CASCADE
) STRICT, WITHOUT ROWID;
"""
