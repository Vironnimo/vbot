"""Additive Simplified Software Factory Jobs schema (Generation 1)."""

from __future__ import annotations

SCHEMA_SQL = """
CREATE TABLE factory_jobs (
 id TEXT PRIMARY KEY,
 factory_id TEXT NOT NULL REFERENCES native_factories(id),
 name TEXT NOT NULL,
 enabled INTEGER NOT NULL DEFAULT 1,
 agent_member_id TEXT NOT NULL DEFAULT '',
 revision INTEGER NOT NULL DEFAULT 1,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 body_json TEXT NOT NULL
) STRICT;
CREATE INDEX factory_jobs_factory ON factory_jobs(factory_id, id);

CREATE TABLE factory_job_runs (
 id TEXT PRIMARY KEY,
 factory_id TEXT NOT NULL REFERENCES native_factories(id),
 job_id TEXT NOT NULL DEFAULT '',
 member_id TEXT NOT NULL REFERENCES native_members(id),
 state TEXT NOT NULL,
 origin TEXT NOT NULL,
 group_id TEXT NOT NULL UNIQUE,
 participant_id TEXT NOT NULL,
 revision INTEGER NOT NULL DEFAULT 1,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 finished_at TEXT,
 body_json TEXT NOT NULL
) STRICT;
CREATE INDEX factory_job_runs_factory ON factory_job_runs(factory_id, state, created_at DESC, id);
CREATE INDEX factory_job_runs_job ON factory_job_runs(job_id, created_at DESC, id);

CREATE TABLE factory_job_trigger_receipts (
 receipt TEXT PRIMARY KEY,
 created_at TEXT NOT NULL
) STRICT;
"""
