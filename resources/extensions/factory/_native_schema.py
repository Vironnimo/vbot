"""Additive native Factory records; Workflow Card tables keep their meaning."""

from ._budget_store import SCHEMA_SQL as BUDGET_SCHEMA_SQL

SCHEMA_SQL = """
CREATE TABLE native_factories (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL UNIQUE,
 state TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, body_json TEXT NOT NULL
) STRICT;
CREATE INDEX native_factories_state ON native_factories(state, id);
CREATE TABLE native_members (
 id TEXT PRIMARY KEY, factory_id TEXT NOT NULL REFERENCES native_factories(id),
 agent_id TEXT NOT NULL, state TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, body_json TEXT NOT NULL
) STRICT;
CREATE INDEX native_members_factory ON native_members(factory_id, state, id);
CREATE INDEX native_members_agent ON native_members(agent_id, state, id);
CREATE TABLE native_tasks (
 id TEXT PRIMARY KEY, factory_id TEXT NOT NULL REFERENCES native_factories(id),
 parent_id TEXT NOT NULL DEFAULT '', assignee_id TEXT NOT NULL DEFAULT '',
 state TEXT NOT NULL, priority REAL NOT NULL DEFAULT 0,
 epoch INTEGER NOT NULL DEFAULT 1, revision INTEGER NOT NULL DEFAULT 1,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, body_json TEXT NOT NULL
) STRICT;
CREATE INDEX native_tasks_factory ON native_tasks(factory_id, state, id);
CREATE INDEX native_tasks_assignee ON native_tasks(factory_id, assignee_id, state, id);
CREATE INDEX native_tasks_parent ON native_tasks(parent_id, id);
CREATE TABLE native_task_dependencies (
 task_id TEXT NOT NULL REFERENCES native_tasks(id),
 dependency_id TEXT NOT NULL REFERENCES native_tasks(id),
 PRIMARY KEY(task_id, dependency_id)
) STRICT;
CREATE INDEX native_dependencies_reverse ON native_task_dependencies(dependency_id, task_id);
CREATE TABLE native_topics (
 id TEXT PRIMARY KEY, factory_id TEXT NOT NULL REFERENCES native_factories(id),
 task_id TEXT NOT NULL DEFAULT '', state TEXT NOT NULL,
 revision INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL, body_json TEXT NOT NULL
) STRICT;
CREATE INDEX native_topics_factory ON native_topics(factory_id, state, id);
CREATE INDEX native_topics_task ON native_topics(task_id, id);
CREATE TABLE native_topic_participants (
 topic_id TEXT NOT NULL REFERENCES native_topics(id),
 member_id TEXT NOT NULL REFERENCES native_members(id),
 read_sequence INTEGER NOT NULL DEFAULT 0, joined_at TEXT NOT NULL,
 PRIMARY KEY(topic_id, member_id)
) STRICT;
CREATE TABLE native_posts (
 id TEXT PRIMARY KEY, topic_id TEXT NOT NULL REFERENCES native_topics(id),
 sequence INTEGER NOT NULL, author TEXT NOT NULL, text TEXT NOT NULL,
 recipients_json TEXT NOT NULL, receipt TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL, UNIQUE(topic_id, sequence)
) STRICT;
CREATE UNIQUE INDEX native_posts_receipt ON native_posts(topic_id, receipt) WHERE receipt != '';
CREATE TABLE native_results (
 id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL REFERENCES native_tasks(id),
 actor TEXT NOT NULL, epoch INTEGER NOT NULL, task_revision INTEGER NOT NULL,
 created_at TEXT NOT NULL, body_json TEXT NOT NULL
) STRICT;
CREATE INDEX native_results_task ON native_results(task_id, id);
CREATE TABLE native_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, factory_id TEXT NOT NULL REFERENCES native_factories(id),
 kind TEXT NOT NULL, scope_id TEXT NOT NULL, actor TEXT NOT NULL,
 event TEXT NOT NULL, created_at TEXT NOT NULL, detail_json TEXT NOT NULL
) STRICT;
CREATE INDEX native_events_scope ON native_events(kind, scope_id, id);
CREATE INDEX native_events_factory ON native_events(factory_id, id);
CREATE TABLE native_executions (
 id TEXT PRIMARY KEY, factory_id TEXT NOT NULL REFERENCES native_factories(id),
 kind TEXT NOT NULL, scope_id TEXT NOT NULL, member_id TEXT NOT NULL REFERENCES native_members(id),
 epoch INTEGER NOT NULL, state TEXT NOT NULL, group_id TEXT NOT NULL,
 participant_id TEXT NOT NULL, input_id TEXT NOT NULL,
 revision INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL, body_json TEXT NOT NULL,
 UNIQUE(kind, scope_id, member_id, epoch)
) STRICT;
CREATE INDEX native_executions_factory ON native_executions(factory_id, state, id);
CREATE INDEX native_executions_scope ON native_executions(kind, scope_id, id);
CREATE INDEX native_executions_member ON native_executions(member_id, state, id);
CREATE TABLE native_deliveries (
 id TEXT PRIMARY KEY, factory_id TEXT NOT NULL REFERENCES native_factories(id),
 execution_id TEXT NOT NULL REFERENCES native_executions(id),
 state TEXT NOT NULL, receipt TEXT NOT NULL UNIQUE,
 revision INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL, body_json TEXT NOT NULL
) STRICT;
CREATE INDEX native_deliveries_execution ON native_deliveries(execution_id, state, id);
CREATE INDEX native_deliveries_factory ON native_deliveries(factory_id, state, id);
CREATE TABLE native_identity_reservations (
 request_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL UNIQUE,
 state TEXT NOT NULL, payload_json TEXT NOT NULL, detail_json TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
) STRICT;
"""

SCHEMA_SQL += BUDGET_SCHEMA_SQL
