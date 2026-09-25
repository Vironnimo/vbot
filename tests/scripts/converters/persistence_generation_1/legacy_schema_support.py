"""Frozen pre-Generation-1 schemas, for building converter test sources.

Copied verbatim from the stores before Generation 1; never update them to
follow the application's current schema.
"""

# ruff: noqa: E501

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

LEGACY_DECISIONS_DDL = """
CREATE TABLE IF NOT EXISTS experiments (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, revision INTEGER NOT NULL,
    draft TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS evaluations (
    sequence INTEGER PRIMARY KEY, id TEXT UNIQUE NOT NULL,
    experiment_id TEXT NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
    request_id TEXT UNIQUE NOT NULL, status TEXT NOT NULL,
    snapshot TEXT NOT NULL, result TEXT, error TEXT,
    created_at TEXT NOT NULL, completed_at TEXT
) STRICT;
CREATE INDEX IF NOT EXISTS evaluation_history
    ON evaluations(experiment_id, sequence DESC);
"""

LEGACY_SWARM_DDL = """
CREATE TABLE IF NOT EXISTS swarm_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL) STRICT;
CREATE TABLE IF NOT EXISTS profiles(id TEXT PRIMARY KEY,slug TEXT NOT NULL UNIQUE,name TEXT NOT NULL,revision INTEGER NOT NULL,payload TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL) STRICT;
CREATE TABLE IF NOT EXISTS swarms(id TEXT PRIMARY KEY,prompt TEXT NOT NULL,profile_snapshot TEXT NOT NULL,effective_configuration TEXT NOT NULL,state TEXT NOT NULL,created_at TEXT NOT NULL) STRICT;
CREATE TABLE IF NOT EXISTS participants(id TEXT PRIMARY KEY,swarm_id TEXT NOT NULL REFERENCES swarms(id),model TEXT NOT NULL,display_name TEXT NOT NULL,ordinal INTEGER NOT NULL,state TEXT NOT NULL,idle_boundary INTEGER,wake_announced_seq INTEGER NOT NULL DEFAULT 0,wake_epoch INTEGER NOT NULL DEFAULT 0,wake_pending INTEGER NOT NULL DEFAULT 0,wake_pending_seq INTEGER NOT NULL DEFAULT 0,lifecycle_run_id TEXT,UNIQUE(swarm_id,ordinal),UNIQUE(swarm_id,display_name COLLATE NOCASE)) STRICT;
CREATE TABLE IF NOT EXISTS participant_sessions(participant_id TEXT PRIMARY KEY REFERENCES participants(id),project_id TEXT,agent_id TEXT NOT NULL,session_id TEXT NOT NULL,generation_id TEXT NOT NULL,owner_name TEXT NOT NULL) STRICT;
CREATE TABLE IF NOT EXISTS swarm_settings(swarm_id TEXT PRIMARY KEY REFERENCES swarms(id),revision INTEGER NOT NULL,delivery_json TEXT NOT NULL) STRICT;
CREATE TABLE IF NOT EXISTS swarm_epochs(swarm_id TEXT PRIMARY KEY REFERENCES swarms(id),epoch INTEGER NOT NULL,is_open INTEGER NOT NULL CHECK(is_open IN(0,1))) STRICT;
CREATE TABLE IF NOT EXISTS swarm_execution_epochs(swarm_id TEXT NOT NULL REFERENCES swarms(id),epoch INTEGER NOT NULL,execution_epoch TEXT NOT NULL,PRIMARY KEY(swarm_id,epoch)) STRICT;
CREATE TABLE IF NOT EXISTS swarm_events(id INTEGER PRIMARY KEY,swarm_id TEXT NOT NULL REFERENCES swarms(id),kind TEXT NOT NULL,actor TEXT NOT NULL,old_json TEXT,new_json TEXT,settings_revision INTEGER,created_at TEXT NOT NULL) STRICT;
CREATE TABLE IF NOT EXISTS discussions(id TEXT PRIMARY KEY,swarm_id TEXT NOT NULL REFERENCES swarms(id),title TEXT NOT NULL,sequence INTEGER NOT NULL,is_main INTEGER NOT NULL CHECK(is_main IN(0,1)),created_at TEXT NOT NULL,UNIQUE(swarm_id,sequence)) STRICT;
CREATE TABLE IF NOT EXISTS memberships(discussion_id TEXT NOT NULL REFERENCES discussions(id),participant_id TEXT NOT NULL REFERENCES participants(id),PRIMARY KEY(discussion_id,participant_id)) STRICT;
CREATE TABLE IF NOT EXISTS posts(id TEXT PRIMARY KEY,swarm_id TEXT NOT NULL REFERENCES swarms(id),discussion_id TEXT NOT NULL REFERENCES discussions(id),sequence INTEGER NOT NULL,author_kind TEXT NOT NULL,author_id TEXT NOT NULL,author_name TEXT NOT NULL,text TEXT NOT NULL,reply_to TEXT,recipients_json TEXT NOT NULL,created_at TEXT NOT NULL,UNIQUE(swarm_id,sequence)) STRICT;
CREATE TABLE IF NOT EXISTS recipients(post_id TEXT NOT NULL REFERENCES posts(id),participant_id TEXT NOT NULL REFERENCES participants(id),route_class TEXT NOT NULL,delivered_at TEXT,receipt_id TEXT,content_hash TEXT,effect_kind TEXT,carrier_kind TEXT,carrier_sequence INTEGER,prepared_at TEXT,PRIMARY KEY(post_id,participant_id)) STRICT;
CREATE TABLE IF NOT EXISTS delivery_batches(receipt_id TEXT PRIMARY KEY,participant_id TEXT NOT NULL REFERENCES participants(id),content_hash TEXT NOT NULL,effect_kind TEXT NOT NULL,created_at TEXT NOT NULL,acknowledged_at TEXT,carrier_kind TEXT,carrier_sequence INTEGER,settings_revision INTEGER) STRICT;
CREATE TABLE IF NOT EXISTS delivery_batch_entries(receipt_id TEXT NOT NULL REFERENCES delivery_batches(receipt_id),post_id TEXT NOT NULL REFERENCES posts(id),participant_id TEXT NOT NULL REFERENCES participants(id),PRIMARY KEY(receipt_id,post_id,participant_id)) STRICT;
CREATE TABLE IF NOT EXISTS requests(scope TEXT NOT NULL,request_id TEXT NOT NULL,payload_hash TEXT NOT NULL,outcome TEXT NOT NULL,PRIMARY KEY(scope,request_id)) STRICT;
CREATE TABLE IF NOT EXISTS swarm_goals(swarm_id TEXT PRIMARY KEY REFERENCES swarms(id),post_id TEXT NOT NULL REFERENCES posts(id)) STRICT;
-- Retained question history has no callable API; it follows explicit Swarm deletion.
CREATE TABLE IF NOT EXISTS decision_questions(id TEXT PRIMARY KEY,swarm_id TEXT NOT NULL REFERENCES swarms(id),document TEXT NOT NULL) STRICT;
CREATE TABLE IF NOT EXISTS decision_positions(question_id TEXT NOT NULL REFERENCES decision_questions(id),actor_id TEXT NOT NULL,document TEXT NOT NULL,PRIMARY KEY(question_id,actor_id)) STRICT;
CREATE TABLE IF NOT EXISTS decision_events(id INTEGER PRIMARY KEY,swarm_id TEXT NOT NULL REFERENCES swarms(id),question_id TEXT NOT NULL REFERENCES decision_questions(id),kind TEXT NOT NULL,actor_id TEXT NOT NULL,author TEXT NOT NULL,created_at TEXT NOT NULL,document TEXT NOT NULL) STRICT;
CREATE INDEX IF NOT EXISTS decision_event_page ON decision_events(swarm_id,question_id,id DESC);
CREATE TABLE IF NOT EXISTS wiki_pages(id TEXT PRIMARY KEY,swarm_id TEXT NOT NULL REFERENCES swarms(id),revision INTEGER NOT NULL) STRICT;
CREATE TABLE IF NOT EXISTS wiki_revisions(id INTEGER PRIMARY KEY,swarm_id TEXT NOT NULL REFERENCES swarms(id),page_id TEXT NOT NULL REFERENCES wiki_pages(id),revision INTEGER NOT NULL,title TEXT NOT NULL,content TEXT NOT NULL,deleted INTEGER NOT NULL CHECK(deleted IN(0,1)),author_id TEXT NOT NULL,author_name TEXT NOT NULL,author_kind TEXT NOT NULL,created_at TEXT NOT NULL,UNIQUE(page_id,revision)) STRICT;
CREATE INDEX IF NOT EXISTS wiki_revision_page ON wiki_revisions(swarm_id,page_id,id DESC);
CREATE INDEX IF NOT EXISTS recipients_pending_participant ON recipients(participant_id,post_id) WHERE delivered_at IS NULL;
CREATE INDEX IF NOT EXISTS posts_discussion_page ON posts(swarm_id,discussion_id,sequence DESC);
CREATE INDEX IF NOT EXISTS discussions_page ON discussions(swarm_id,is_main DESC,sequence);
CREATE UNIQUE INDEX IF NOT EXISTS discussions_one_main ON discussions(swarm_id) WHERE is_main=1;
"""


def create_legacy_database(path: Path, ddl: str) -> None:
    """Create a pre-Generation-1 database at ``path`` with ``ddl`` and no rows."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(ddl)
