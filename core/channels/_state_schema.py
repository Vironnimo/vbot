"""The canonical Channel state database declaration handed to the database kernel.

``channels.db`` holds the durable Channel state that is not configuration:
group access, conversation routing pointers, Run-button origin bindings,
inbound receipts and the Telegram polling watermark. ``channel.json`` stays the
configuration document. Every state table references the ``channels`` registry,
so a Channel's rows exist only while it is registered and leave with it in one
cascading delete.
"""

from __future__ import annotations

from pathlib import Path

from core.database import APPLICATION_IDS, CANONICAL, DatabaseSpec, SnapshotFacts

DATABASE_NAME = "channels"
APPLICATION_ID = APPLICATION_IDS[DATABASE_NAME]
# The physical format generation, not a counter for additive changes: those
# reconcile in place. Raise it only for a change a converter must perform.
FORMAT_GENERATION = 1

# Timestamps are canonical fixed-width UTC text (YYYY-MM-DDTHH:MM:SS.ffffffZ),
# so text order is time order. Kinds are validated by the owning code, never by
# CHECK constraints.
SCHEMA_SQL = """
CREATE TABLE channels (
  channel_id TEXT PRIMARY KEY,
  self_user_id TEXT CHECK (self_user_id IS NULL OR length(self_user_id) > 0)
) STRICT, WITHOUT ROWID;

CREATE TABLE channel_admins (
  channel_id TEXT NOT NULL,
  access_scope_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  PRIMARY KEY (channel_id, access_scope_id, user_id),
  FOREIGN KEY (channel_id) REFERENCES channels (channel_id) ON DELETE CASCADE
) STRICT, WITHOUT ROWID;

CREATE TABLE channel_participants (
  channel_id TEXT NOT NULL,
  access_scope_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  display_name TEXT NOT NULL CHECK (length(display_name) > 0),
  last_seen_at TEXT NOT NULL,
  PRIMARY KEY (channel_id, access_scope_id, user_id),
  FOREIGN KEY (channel_id) REFERENCES channels (channel_id) ON DELETE CASCADE
) STRICT, WITHOUT ROWID;

CREATE TABLE channel_conversations (
  channel_id TEXT NOT NULL,
  conversation_id TEXT NOT NULL,
  conversation_kind TEXT NOT NULL,
  active_session_id TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (channel_id, conversation_id),
  FOREIGN KEY (channel_id) REFERENCES channels (channel_id) ON DELETE CASCADE
) STRICT, WITHOUT ROWID;

CREATE TABLE channel_run_buttons (
  channel_id TEXT NOT NULL,
  binding_id TEXT NOT NULL,
  platform_target TEXT NOT NULL,
  thread_id TEXT,
  origin_session_id TEXT NOT NULL,
  button_data_json TEXT NOT NULL
    CHECK (json_valid(button_data_json) AND json_type(button_data_json) = 'array'),
  created_at TEXT NOT NULL,
  consumed_at TEXT,
  PRIMARY KEY (channel_id, binding_id),
  FOREIGN KEY (channel_id) REFERENCES channels (channel_id) ON DELETE CASCADE
) STRICT, WITHOUT ROWID;

CREATE TABLE channel_received (
  channel_id TEXT NOT NULL,
  message_ref TEXT NOT NULL,
  received_at TEXT NOT NULL,
  PRIMARY KEY (channel_id, message_ref),
  FOREIGN KEY (channel_id) REFERENCES channels (channel_id) ON DELETE CASCADE
) STRICT, WITHOUT ROWID;

-- Reader: the FIFO prune after each recorded receipt (ChannelStateStore.record_received).
CREATE INDEX channel_received_by_time
  ON channel_received (channel_id, received_at);

CREATE TABLE channel_polling (
  channel_id TEXT PRIMARY KEY,
  last_update_id INTEGER NOT NULL CHECK (last_update_id >= 0),
  updated_at TEXT NOT NULL,
  FOREIGN KEY (channel_id) REFERENCES channels (channel_id) ON DELETE CASCADE
) STRICT, WITHOUT ROWID;
"""

# Owner facts every data snapshot records for the Channel member and
# re-verifies on its copy.
_SNAPSHOT_FACTS = SnapshotFacts(
    {
        "channel_count": "SELECT COUNT(*) FROM channels",
        "participant_count": "SELECT COUNT(*) FROM channel_participants",
        "run_button_count": "SELECT COUNT(*) FROM channel_run_buttons",
    }
)


def channel_database_spec(path: Path) -> DatabaseSpec:
    """Declare the canonical Channel state database at ``path`` (``<data-dir>/channels.db``)."""
    return DatabaseSpec(
        name=DATABASE_NAME,
        path=Path(path),
        profile=CANONICAL,
        application_id=APPLICATION_ID,
        format_generation=FORMAT_GENERATION,
        schema_sql=SCHEMA_SQL,
        snapshot_facts=_SNAPSHOT_FACTS,
    )
