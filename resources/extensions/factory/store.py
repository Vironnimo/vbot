"""Factory records and atomic lifecycle events on the host-owned database."""

from __future__ import annotations

import json
import secrets
import sqlite3
from contextlib import suppress
from typing import Any

from core.extensions.databases import Database, Migration
from core.extensions.operations import ExtensionOperationError
from core.utils.ids import new_id
from core.utils.timestamps import utc_now_timestamp

from . import _budget_store, agent_text
from ._native_schema import SCHEMA_SQL as NATIVE_SCHEMA_SQL
from ._native_store import NativeFactoryStore

type Json = dict[str, Any]

STATES = frozenset({"proposal", "queued", "working", "waiting", "done", "discarded"})
FINAL_STATES = frozenset({"done", "discarded"})
ORIGINS = frozenset({"operator", "user", "automation", "agent", "trigger", "workflow"})
#: Version of a Card's runtime: per-step execution state.
RUNTIME_VERSION = 2
DATABASE_NAME = "factory"
# Tables evolve additively: a changed object gets a new name. ``workflows``
# holds the global Workflows that replaced the per-Project ``setups`` (still on
# disk for the migration); the shared template Library is gone and its tables
# stay inert. An output row's ``stage_id``/``stage_name`` hold the step and
# ``target`` the step it sends back to.
SCHEMA_SQL = """
CREATE TABLE workflows (
 id TEXT PRIMARY KEY, position REAL NOT NULL, definition_json TEXT NOT NULL,
 revision INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL
) STRICT;
CREATE TABLE cards (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL, title TEXT NOT NULL,
 description TEXT NOT NULL, origin TEXT NOT NULL, origin_session TEXT NOT NULL DEFAULT '',
 origin_run TEXT NOT NULL DEFAULT '', state TEXT NOT NULL, waiting_reason TEXT NOT NULL DEFAULT '',
 position REAL NOT NULL, summary TEXT NOT NULL DEFAULT '',
 group_id TEXT NOT NULL UNIQUE, runtime_json TEXT NOT NULL DEFAULT '{}',
 revision INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 finished_at TEXT, workflow_id TEXT NOT NULL DEFAULT '', parent_id TEXT NOT NULL DEFAULT ''
) STRICT;
CREATE INDEX cards_state_position ON cards(state, position, id);
CREATE INDEX cards_project_state ON cards(project_id, state);
CREATE INDEX cards_parent_id ON cards(parent_id);
CREATE TABLE card_triggers (
 card_id TEXT PRIMARY KEY REFERENCES cards(id) ON DELETE CASCADE, event_json TEXT NOT NULL
) STRICT;
CREATE TABLE trigger_receipts (
 receipt TEXT PRIMARY KEY
) STRICT;
CREATE TABLE outputs (
 id INTEGER PRIMARY KEY, card_id TEXT NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
 stage_id TEXT NOT NULL, stage_name TEXT NOT NULL, kind TEXT NOT NULL, lane INTEGER NOT NULL,
 pass INTEGER NOT NULL, participant_id TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '',
 result TEXT NOT NULL DEFAULT '', checks TEXT NOT NULL DEFAULT '',
 branch TEXT NOT NULL DEFAULT '', base TEXT NOT NULL DEFAULT '',
 base_sha TEXT NOT NULL DEFAULT '', head_sha TEXT NOT NULL DEFAULT '',
 verdict TEXT NOT NULL DEFAULT '', recommendation TEXT NOT NULL DEFAULT '',
 findings_json TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL,
 target TEXT NOT NULL DEFAULT ''
) STRICT;
CREATE INDEX outputs_card ON outputs(card_id, id);
CREATE TABLE card_events (
 id INTEGER PRIMARY KEY, card_id TEXT NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
 at TEXT NOT NULL, actor TEXT NOT NULL, event TEXT NOT NULL, detail_json TEXT NOT NULL
) STRICT;
CREATE INDEX card_events_card ON card_events(card_id, id);
CREATE TABLE comments (
 id INTEGER PRIMARY KEY, card_id TEXT NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
 at TEXT NOT NULL, author TEXT NOT NULL, text TEXT NOT NULL
) STRICT;
CREATE INDEX comments_card ON comments(card_id, id);
"""
SCHEMA_SQL += NATIVE_SCHEMA_SQL

_OUTPUT_FIELDS = (
    "stage_id",
    "stage_name",
    "kind",
    "lane",
    "pass",
    "participant_id",
    "summary",
    "result",
    "checks",
    "branch",
    "base",
    "base_sha",
    "head_sha",
    "verdict",
    "recommendation",
    "findings_json",
    "target",
)
_OUTPUT_COLUMNS = ("id", *_OUTPUT_FIELDS, "created_at")


def _output(row: sqlite3.Row) -> Json:
    """One recorded output: ``work`` hands in a result, ``changes`` sends work back.

    Rows from the stage model recorded reviews as verdicts; an approval reads as
    the reviewer's hand-in and requested changes as a send-back.
    """
    value = dict(row)
    kind = value["kind"]
    result = value["result"]
    if kind not in {"work", "changes"}:
        if value["verdict"] == "changes_requested":
            kind = "changes"
        else:
            kind, result = "work", value["recommendation"]
    return {
        "id": value["id"],
        "step": value["stage_id"],
        "step_name": value["stage_name"],
        "kind": kind,
        "pass": value["pass"],
        "participant_id": value["participant_id"],
        "summary": value["summary"],
        "result": result,
        "branch": value["branch"],
        "findings": json.loads(value["findings_json"]),
        "target": value["target"],
        "created_at": value["created_at"],
    }


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _short_id(prefix: str) -> str:
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
    return prefix + "".join(secrets.choice(alphabet) for _ in range(7))


def _upgraded_runtime(old: Json, workflow: Json) -> Json:
    """A started Card's per-step runtime; the Card waits for the user.

    Steps handed in stay done; the step that was in progress waits for the user
    to Retry it, because its Run cannot resume across the change of execution
    groups. A Card that waited for acceptance still does.
    """
    done = set(old.get("done", []))
    current = old.get("step", "")
    steps: Json = {}
    for step in workflow["steps"]:
        if step["id"] in done:
            steps[step["id"]] = {"status": "done", "passes": 1}
        else:
            steps[step["id"]] = {"status": "pending"}
    runtime: Json = {
        "version": RUNTIME_VERSION,
        "workflow": workflow,
        "cwd": old.get("cwd", ""),
        "steps": steps,
        "budget": {
            "runs": list(old.get("budget", {}).get("runs", [])),
            "tool_calls": int(old.get("budget", {}).get("tool_calls", 0)),
        },
    }
    if old.get("card_limits") is not None:
        runtime["card_limits"] = old["card_limits"]
    if all(item["status"] == "done" for item in steps.values()):
        return runtime
    target = current if current in steps and current not in done else None
    if target is None:
        target = next(step["id"] for step in workflow["steps"] if step["id"] not in done)
    steps[target] = {
        "status": "waiting",
        "reason": "upgraded",
        "note": agent_text.error_message("workflow_upgrade"),
    }
    return runtime


def _migrate_global_workflows(connection: sqlite3.Connection) -> None:
    """Turn each Project setup's Workflows into global Workflows (idempotent).

    A copied Workflow keeps its Project fixed, waits for final acceptance like
    every Card did before, and keeps its id unless another Project's Workflow
    took it. Cards follow the renamed ids; started unfinished Cards move to the
    per-step runtime.
    """
    tables = {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    if connection.execute("SELECT 1 FROM workflows LIMIT 1").fetchone():
        return
    ids: dict[tuple[str, str], str] = {}
    defaults: dict[str, str] = {}
    snapshots: dict[str, Json] = {}
    if "setups" in tables:
        taken_ids: set[str] = set()
        taken_names: set[str] = set()
        position = 0.0
        now = utc_now_timestamp()
        for project_id, enabled, raw, default in connection.execute(
            "SELECT project_id, enabled, workflows_json, default_workflow FROM setups "
            "ORDER BY project_id"
        ).fetchall():
            workflows = [item for item in json.loads(raw) if isinstance(item, dict)]
            workflows.sort(key=lambda item: item.get("id") != default)
            for old in workflows:
                if not isinstance(old.get("steps"), list) or not old["steps"]:
                    continue
                workflow_id = str(old.get("id") or "")
                if not workflow_id or workflow_id in taken_ids:
                    workflow_id = _short_id("w")
                    while workflow_id in taken_ids:
                        workflow_id = _short_id("w")
                name = str(old.get("name") or "Workflow")
                if name.casefold() in taken_names:
                    name = f"{name} ({project_id})"
                    while name.casefold() in taken_names:
                        name += " 2"
                taken_ids.add(workflow_id)
                taken_names.add(name.casefold())
                definition = {
                    "id": workflow_id,
                    "name": name,
                    "description": str(old.get("description") or ""),
                    "enabled": bool(enabled),
                    "project_id": project_id,
                    "final_approval": True,
                    "steps": [
                        {**step, "kind": "agent"} for step in old["steps"] if isinstance(step, dict)
                    ],
                    "triggers": [],
                }
                if old.get("limits"):
                    definition["limits"] = old["limits"]
                position += 1
                connection.execute(
                    "INSERT INTO workflows (id, position, definition_json, revision, updated_at) "
                    "VALUES (?, ?, ?, 1, ?)",
                    (workflow_id, position, _dump(definition), now),
                )
                ids[(project_id, str(old.get("id") or ""))] = workflow_id
                defaults.setdefault(project_id, workflow_id)
                snapshots[workflow_id] = definition
    for card_id, project_id, workflow_id, state, raw in connection.execute(
        "SELECT id, project_id, workflow_id, state, runtime_json FROM cards"
    ).fetchall():
        old = json.loads(raw)
        new_workflow = ids.get((project_id, workflow_id)) or (
            workflow_id or defaults.get(project_id, workflow_id)
        )
        if old.get("version") == RUNTIME_VERSION:
            continue
        started = isinstance(old.get("workflow"), dict) and old["workflow"].get("steps")
        patch_state = state
        if state in FINAL_STATES:
            runtime: Json = {"version": RUNTIME_VERSION, "cwd": old.get("cwd", ""), "legacy": True}
            if started:
                runtime["workflow"] = {
                    **old["workflow"],
                    "steps": [{**step, "kind": "agent"} for step in old["workflow"]["steps"]],
                }
        elif started:
            snapshot = {
                **old["workflow"],
                "id": new_workflow,
                "project_id": project_id,
                "final_approval": True,
                "enabled": True,
                "triggers": [],
                "steps": [{**step, "kind": "agent"} for step in old["workflow"]["steps"]],
            }
            runtime, patch_state = _upgraded_runtime(old, snapshot), "waiting"
            if old.get("card_limits") is None and old["workflow"].get("limits"):
                runtime["card_limits"] = old["workflow"]["limits"]
        else:
            runtime = {"version": RUNTIME_VERSION, "cwd": old.get("cwd", "")}
            patch_state = "queued" if state != "proposal" else state
        waiting_reason = ""
        if patch_state == "waiting":
            steps = runtime.get("steps", {})
            waiting_reason = (
                "upgraded"
                if any(item.get("reason") == "upgraded" for item in steps.values())
                else "finished"
            )
        connection.execute(
            "UPDATE cards SET workflow_id = ?, state = ?, waiting_reason = ?, runtime_json = ? "
            "WHERE id = ?",
            (
                new_workflow,
                patch_state,
                waiting_reason if patch_state == "waiting" else "",
                _dump(runtime),
                card_id,
            ),
        )


def _remove_model_overrides(connection: sqlite3.Connection) -> None:
    # Revision changes also fence old Session bindings with a frozen Model.
    # Native recovery carries pending work over to the new execution epoch.
    for table in ("native_factories", "native_members"):
        connection.execute(
            f"UPDATE {table} SET body_json=json_remove(body_json, "
            "'$.config.model', '$.config.thinking_effort'), revision=revision+1 "
            "WHERE json_type(body_json,'$.config.model') IS NOT NULL "
            "OR json_type(body_json,'$.config.thinking_effort') IS NOT NULL"
        )


# Every migration an earlier Factory recorded stays declared: an undeclared
# ``breaks_older`` migration makes the database refuse to open. Their data
# steps are folded into ``global_workflows``, which reads any earlier setup.
MIGRATIONS = (
    Migration("workflows"),
    Migration("steps", breaks_older=True),
    Migration("library_links", breaks_older=True),
    Migration("execution_limits", breaks_older=True),
    Migration("execution_limit_history", breaks_older=True),
    Migration("global_workflows", breaks_older=True, apply=_migrate_global_workflows),
    Migration("native_factory_teams"),
    Migration("factory_budget_changes", apply=_budget_store.backfill),
    Migration("agent_profile_models", apply=_remove_model_overrides),
)

_CARD_FIELDS = (
    "id",
    "project_id",
    "title",
    "description",
    "origin",
    "origin_session",
    "origin_run",
    "state",
    "waiting_reason",
    "position",
    "summary",
    "group_id",
    "runtime_json",
    "revision",
    "created_at",
    "updated_at",
    "finished_at",
    "workflow_id",
    "parent_id",
)
_CARD_COLUMNS = ", ".join(_CARD_FIELDS)
_MUTABLE_CARD_FIELDS = {
    "project_id",
    "title",
    "description",
    "state",
    "waiting_reason",
    "position",
    "summary",
    "finished_at",
}


class FactoryError(ExtensionOperationError):
    """An expected refusal, with an actionable English message."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message)


def _card(row: sqlite3.Row) -> Json:
    value = dict(row)
    value["runtime"] = json.loads(value.pop("runtime_json"))
    return value


def _workflow(row: sqlite3.Row) -> Json:
    value: Json = json.loads(row["definition_json"])
    value.update(revision=row["revision"], updated_at=row["updated_at"])
    return value


def _get(connection: sqlite3.Connection, card_id: str) -> Json:
    row = connection.execute(
        f"SELECT {_CARD_COLUMNS} FROM cards WHERE id = ?", (card_id,)
    ).fetchone()
    if row is None:
        raise FactoryError(
            "card_not_found", f"Card {card_id!r} does not exist. List the Board to choose a Card."
        )
    return _card(row)


def _get_workflow(connection: sqlite3.Connection, workflow_id: str) -> Json:
    row = connection.execute(
        "SELECT definition_json, revision, updated_at FROM workflows WHERE id = ?", (workflow_id,)
    ).fetchone()
    if row is None:
        raise FactoryError(
            "workflow_not_found",
            f"Workflow {workflow_id!r} does not exist. List the Workflows to choose one.",
        )
    return _workflow(row)


def _event(
    connection: sqlite3.Connection, card_id: str, at: str, actor: str, event: str, detail: Json
) -> None:
    connection.execute(
        "INSERT INTO card_events (card_id, at, actor, event, detail_json) VALUES (?, ?, ?, ?, ?)",
        (card_id, at, actor, event, _dump(detail)),
    )


class FactoryStore:
    """Persist domain state without opening or inspecting canonical Session storage."""

    def __init__(self, database: Database) -> None:
        self.database = database
        self.native = NativeFactoryStore(database)

    async def budget_changes(
        self,
        after: int = 0,
        *,
        ledger_id: str | None = None,
        limit: int = 1000,
    ) -> Json:
        current = f"{self.database.database_id}:{self.database.restore_id or ''}"
        page = await self.database.read_async(
            lambda connection: _budget_store.read_changes(
                connection,
                0 if ledger_id != current else after,
                limit,
            )
        )
        return {"ledger_id": current, **page}

    # Workflows

    async def list_workflows(self) -> list[Json]:
        return await self.database.read_async(
            lambda connection: [
                _workflow(row)
                for row in connection.execute(
                    "SELECT definition_json, revision, updated_at FROM workflows "
                    "ORDER BY position, id"
                )
            ]
        )

    async def get_workflow(self, workflow_id: str) -> Json:
        return await self.database.read_async(
            lambda connection: _get_workflow(connection, workflow_id)
        )

    async def save_workflow(self, definition: Json, *, expected_revision: int | None) -> Json:
        """Insert or replace a Workflow; ``expected_revision`` 0 creates it."""
        at = utc_now_timestamp()
        workflow_id = definition["id"]

        def write(connection: sqlite3.Connection) -> Json:
            row = connection.execute(
                "SELECT revision FROM workflows WHERE id = ?", (workflow_id,)
            ).fetchone()
            revision = int(row[0]) if row is not None else 0
            if expected_revision is not None and expected_revision != revision:
                raise FactoryError(
                    "revision_conflict",
                    "The Workflow changed elsewhere. Refresh it before saving your changes.",
                )
            for other_id, raw in connection.execute(
                "SELECT id, definition_json FROM workflows WHERE id != ?", (workflow_id,)
            ):
                if json.loads(raw)["name"].casefold() == definition["name"].casefold():
                    raise FactoryError(
                        "workflow_name_taken",
                        f"Workflow {other_id} already has the name {definition['name']!r}. "
                        "Choose another name.",
                    )
            stored = {
                key: value
                for key, value in definition.items()
                if key not in {"revision", "updated_at"}
            }
            if row is None:
                position = connection.execute(
                    "SELECT COALESCE(MAX(position), 0) + 1 FROM workflows"
                ).fetchone()[0]
                connection.execute(
                    "INSERT INTO workflows (id, position, definition_json, revision, updated_at) "
                    "VALUES (?, ?, ?, 1, ?)",
                    (workflow_id, position, _dump(stored), at),
                )
            else:
                connection.execute(
                    "UPDATE workflows SET definition_json = ?, revision = revision + 1, "
                    "updated_at = ? WHERE id = ?",
                    (_dump(stored), at, workflow_id),
                )
            return _get_workflow(connection, workflow_id)

        return await self.database.write_async(write)

    async def delete_workflow(self, workflow_id: str) -> None:
        """Delete an unselected Workflow; started Cards retain their own snapshot."""

        def write(connection: sqlite3.Connection) -> None:
            _get_workflow(connection, workflow_id)
            for raw in connection.execute(
                "SELECT definition_json FROM workflows WHERE id != ?", (workflow_id,)
            ):
                other = json.loads(raw[0])
                if any(
                    step.get("kind") == "workflow" and step.get("workflow") == workflow_id
                    for step in other["steps"]
                ):
                    raise FactoryError(
                        "workflow_in_use",
                        f"Workflow {other['name']!r} starts this Workflow in one of its steps. "
                        "Change that step before deleting this Workflow.",
                    )
            queued = connection.execute(
                "SELECT COUNT(*) FROM cards WHERE workflow_id = ? "
                "AND state IN ('proposal', 'queued')",
                (workflow_id,),
            ).fetchone()[0]
            if queued:
                raise FactoryError(
                    "workflow_in_use",
                    f"{queued} Card(s) wait to start with this Workflow. "
                    "Discard them or let them start before deleting it.",
                )
            selected = connection.execute(
                "SELECT id FROM native_tasks WHERE json_extract(body_json, '$.workflow_id') = ? "
                "LIMIT 1",
                (workflow_id,),
            ).fetchone()
            if selected is not None:
                raise FactoryError(
                    "workflow_in_use",
                    f"Task {selected[0]!r} selects this Workflow. "
                    "Clear or change the task's Workflow before deleting it.",
                )
            connection.execute("DELETE FROM workflows WHERE id = ?", (workflow_id,))

        await self.database.write_async(write)

    # Cards

    async def create_card(
        self,
        project_id: str,
        title: str,
        description: str,
        *,
        origin: str,
        origin_session: str = "",
        origin_run: str = "",
        runtime: Json | None = None,
        workflow_id: str = "",
        parent_id: str = "",
        trigger_event: Json | None = None,
        trigger_receipt: str = "",
    ) -> Json:
        if origin not in ORIGINS:
            raise ValueError("invalid card origin")
        at = utc_now_timestamp()

        def write(connection: sqlite3.Connection) -> Json:
            row = connection.execute("SELECT MAX(position) AS position FROM cards").fetchone()
            position = float(row["position"] or 0) + 1

            def claim(card_id: str) -> bool:
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO cards "
                    "(id, project_id, title, description, origin, origin_session, "
                    "origin_run, state, position, group_id, runtime_json, created_at, updated_at, "
                    "workflow_id, parent_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        card_id,
                        project_id,
                        title,
                        description,
                        origin,
                        origin_session,
                        origin_run,
                        "proposal" if origin == "agent" else "queued",
                        position,
                        card_id,
                        _dump({"version": RUNTIME_VERSION, **(runtime or {})}),
                        at,
                        at,
                        workflow_id,
                        parent_id,
                    ),
                )
                return cursor.rowcount == 1

            card_id = new_id("fct", claim=claim)
            if trigger_event is not None:
                connection.execute(
                    "INSERT INTO card_triggers (card_id, event_json) VALUES (?, ?)",
                    (card_id, _dump(trigger_event)),
                )
            if trigger_receipt:
                connection.execute(
                    "INSERT INTO trigger_receipts (receipt) VALUES (?)", (trigger_receipt,)
                )
            _event(
                connection,
                card_id,
                at,
                "user" if origin in {"operator", "user"} else "factory",
                "created",
                {"origin": origin, **({"parent": parent_id} if parent_id else {})},
            )
            return _get(connection, card_id)

        return await self.database.write_async(write)

    async def get_card(self, card_id: str) -> Json:
        return await self.database.read_async(lambda connection: _get(connection, card_id))

    async def trigger_event(self, card_id: str) -> Json | None:
        """The trigger event that created a Card, if any."""

        def read(connection: sqlite3.Connection) -> Json | None:
            row = connection.execute(
                "SELECT event_json FROM card_triggers WHERE card_id = ?", (card_id,)
            ).fetchone()
            return None if row is None else json.loads(row[0])

        return await self.database.read_async(read)

    async def received_trigger(self, receipt: str) -> bool:
        """Whether a fire already created its Card, including a Card since deleted."""
        return await self.database.read_async(
            lambda connection: (
                connection.execute(
                    "SELECT 1 FROM trigger_receipts WHERE receipt = ?", (receipt,)
                ).fetchone()
                is not None
            )
        )

    async def find_child_card(self, parent_id: str, intent: str) -> Json | None:
        """Recover a child whose creation committed before the parent's step update."""

        def read(connection: sqlite3.Connection) -> Json | None:
            child = connection.execute(
                f"SELECT {_CARD_COLUMNS} FROM cards WHERE parent_id = ? "
                "AND json_extract(runtime_json, '$.parent_intent') = ? LIMIT 1",
                (parent_id, intent),
            ).fetchone()
            return None if child is None else _card(child)

        return await self.database.read_async(read)

    async def attention_count(self) -> int:
        return await self.database.read_async(
            lambda connection: int(
                connection.execute(
                    "SELECT COUNT(*) FROM cards WHERE state IN ('waiting', 'proposal')"
                ).fetchone()[0]
            )
        )

    async def project_reply(self, project_id: str, post_id: str) -> Json:
        def read(connection: sqlite3.Connection) -> Json:
            row = connection.execute(
                """SELECT p.text, p.topic_id, p.created_at, m.agent_id,
                       COALESCE(NULLIF(json_extract(m.body_json,'$.name'),''),
                                json_extract(m.body_json,'$.role'),m.agent_id) AS author
                   FROM native_posts p JOIN native_topics t ON t.id=p.topic_id
                   JOIN native_factories f ON f.id=t.factory_id
                   JOIN native_members m ON m.id=json_extract(t.body_json,'$.direct_member_id')
                   WHERE f.project_id=? AND p.id=? AND p.author='member:'||m.id""",
                (project_id, post_id),
            ).fetchone()
            if row is None:
                raise FactoryError("reply_not_found", "Choose a reply in this Project.")
            return dict(row)

        return await self.database.read_async(read)

    async def project_work(
        self,
        project_id: str,
        after: str,
        limit: int,
        query: str = "",
        *,
        include_replies: bool = False,
    ) -> Json:
        """Page the combined Project work history, with outstanding decisions first."""

        def read(connection: sqlite3.Connection) -> Json:
            cursor = None
            if after:
                with suppress(ValueError):
                    cursor = json.loads(after)
                if (
                    not isinstance(cursor, list)
                    or len(cursor) != 3
                    or cursor[0] not in (0, 1)
                    or not all(isinstance(value, str) for value in cursor[1:])
                ):
                    raise FactoryError(
                        "invalid_cursor", "Use the next cursor returned by the Project history."
                    )
            rows = [
                dict(row)
                for row in connection.execute(
                    """
                WITH work AS (
                  SELECT t.id, json_extract(t.body_json, '$.title') AS title, t.state,
                      'task' AS kind, t.updated_at,
                      (t.state='review' AND json_extract(t.body_json, '$.user_approval')=1
                       AND COALESCE(json_extract(t.body_json, '$.submission.user_approved'),0)=0
                       OR t.state='blocked' AND json_extract(t.body_json, '$.reason') NOT IN
                           ('dependencies','children','peer','workflow')) AS attention,
                       NULL AS author
                  FROM native_tasks t JOIN native_factories f ON f.id=t.factory_id
                  WHERE f.project_id=? AND NOT EXISTS (
                      SELECT 1 FROM cards c
                      WHERE c.id=json_extract(t.body_json,'$.workflow_card_id')
                          AND c.state IN ('proposal','waiting'))
                  UNION ALL
                  SELECT c.id, c.title, c.state, 'card', c.updated_at,
                      c.state IN ('proposal','waiting'), NULL FROM cards c WHERE c.project_id=?
                      AND (c.state IN ('proposal','waiting') OR
                          COALESCE(json_extract(c.runtime_json, '$.native_task_id'),'')='')
                  UNION ALL
                  SELECT p.id, COALESCE(NULLIF(substr(trim(p.text, char(9,10,13,32,35)),1,140),''),
                                            json_extract(t.body_json,'$.title')),
                      '', 'reply', p.created_at, 0,
                      COALESCE(NULLIF(json_extract(m.body_json,'$.name'),''),
                               json_extract(m.body_json,'$.role'),m.agent_id)
                  FROM native_topics t JOIN native_factories f ON f.id=t.factory_id
                  JOIN native_members m ON m.id=json_extract(t.body_json,'$.direct_member_id')
                  JOIN native_posts p ON p.topic_id=t.id AND p.author='member:'||m.id
                  WHERE ? AND f.project_id=? AND trim(p.text)!=''
                )
                SELECT * FROM work WHERE instr(lower(title),lower(?))>0
                    AND (? IS NULL OR (attention, updated_at, id) < (?,?,?))
                ORDER BY attention DESC, updated_at DESC, id DESC LIMIT ?
                """,
                    (
                        project_id,
                        project_id,
                        include_replies,
                        project_id,
                        query,
                        after or None,
                        *(cursor or [0, "", ""]),
                        limit + 1,
                    ),
                )
            ]
            shown = rows[:limit]
            for item in shown:
                if item["kind"] == "reply":
                    item["title"] = item["title"].partition("\n")[0].strip()
                else:
                    item.pop("author")
            last = shown[-1] if len(rows) > limit else None
            return {
                "work": shown,
                "next_after": json.dumps([last["attention"], last["updated_at"], last["id"]])
                if last
                else "",
            }

        return await self.database.read_async(read)

    async def list_cards(
        self, project_id: str | None = None, state: str | None = None
    ) -> list[Json]:
        if state is not None and state not in STATES:
            raise FactoryError(
                "invalid_state",
                "Unknown Card state. Use proposal, queued, working, waiting, done or discarded.",
            )
        clauses: list[str] = []
        parameters: list[str] = []
        for name, value in (("project_id", project_id), ("state", state)):
            if value is not None:
                clauses.append(f"{name} = ?")
                parameters.append(value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        return await self.database.read_async(
            lambda connection: [
                _card(row)
                for row in connection.execute(
                    f"SELECT {_CARD_COLUMNS} FROM cards{where} ORDER BY position, created_at, id",
                    parameters,
                )
            ]
        )

    async def change(
        self,
        card_id: str,
        patch: Json,
        *,
        actor: str,
        event: str | None,
        detail: Json | None = None,
        comment: str | None = None,
        output: list[Json] | None = None,
        expected_revision: int | None = None,
    ) -> Json:
        if set(patch) - (_MUTABLE_CARD_FIELDS | {"runtime"}):
            raise ValueError("invalid card patch")
        if "state" in patch and patch["state"] not in STATES:
            raise ValueError("invalid card state")
        at = utc_now_timestamp()

        def write(connection: sqlite3.Connection) -> Json:
            previous = _get(connection, card_id)
            if expected_revision is not None and previous["revision"] != expected_revision:
                raise FactoryError(
                    "revision_conflict",
                    "The Card changed elsewhere. Refresh it before trying again.",
                )
            changes = dict(patch)
            if "runtime" in changes:
                changes["runtime_json"] = _dump(changes.pop("runtime"))
            changes.update(revision=previous["revision"] + 1, updated_at=at)
            if patch.get("state") in FINAL_STATES and previous["state"] not in FINAL_STATES:
                changes["finished_at"] = at
            connection.execute(
                f"UPDATE cards SET {', '.join(f'{name} = ?' for name in changes)} WHERE id = ?",
                (*changes.values(), card_id),
            )
            event_detail = dict(detail or {})
            if "state" in patch and patch["state"] != previous["state"]:
                event_detail.update(previous_state=previous["state"], state=patch["state"])
            if event is not None:
                _event(connection, card_id, at, actor, event, event_detail)
            if comment is not None:
                connection.execute(
                    "INSERT INTO comments (card_id, at, author, text) VALUES (?, ?, ?, ?)",
                    (card_id, at, actor, comment),
                )
            for item in output or []:
                # Columns of earlier Factory versions stay empty.
                values = (
                    dict.fromkeys(_OUTPUT_FIELDS, "")
                    | {
                        field: item.get(field, "")
                        for field in ("kind", "participant_id", "summary", "result", "target")
                    }
                    | {
                        "stage_id": item["step"],
                        "stage_name": item["step_name"],
                        "lane": 0,
                        "pass": item["pass"],
                        "findings_json": _dump(item.get("findings", [])),
                    }
                )
                connection.execute(
                    f"INSERT INTO outputs (card_id, {', '.join(_OUTPUT_FIELDS)}, created_at) "
                    f"VALUES (?, {', '.join('?' for _ in _OUTPUT_FIELDS)}, ?)",
                    (card_id, *(values[name] for name in _OUTPUT_FIELDS), at),
                )
            return _get(connection, card_id)

        return await self.database.write_async(write)

    async def recent_outputs(self, card_id: str) -> list[Json]:
        def read(connection: sqlite3.Connection) -> list[Json]:
            rows = connection.execute(
                f"SELECT {', '.join(_OUTPUT_COLUMNS)} FROM outputs WHERE card_id=? "
                "ORDER BY id DESC LIMIT 5",
                (card_id,),
            ).fetchall()
            return [_output(row) for row in reversed(rows)]

        return await self.database.read_async(read)

    async def history(self, card_id: str) -> Json:
        def read(connection: sqlite3.Connection) -> Json:
            _get(connection, card_id)
            events = []
            for row in connection.execute(
                "SELECT id, at, actor, event, detail_json "
                "FROM card_events WHERE card_id = ? ORDER BY id",
                (card_id,),
            ):
                item = dict(row)
                item["detail"] = json.loads(item.pop("detail_json"))
                events.append(item)
            outputs = [
                _output(row)
                for row in connection.execute(
                    f"SELECT {', '.join(_OUTPUT_COLUMNS)} FROM outputs "
                    "WHERE card_id = ? ORDER BY id",
                    (card_id,),
                )
            ]
            comments = [
                dict(row)
                for row in connection.execute(
                    "SELECT at, author, text FROM comments WHERE card_id = ? ORDER BY id",
                    (card_id,),
                )
            ]
            return {"events": events, "outputs": outputs, "comments": comments}

        return await self.database.read_async(read)

    async def delete_card(self, card_id: str) -> None:
        def write(connection: sqlite3.Connection) -> None:
            card = _get(connection, card_id)
            if card["state"] not in FINAL_STATES:
                raise FactoryError(
                    "card_not_final", "The Card is unfinished. Discard it before deleting it."
                )
            self._check_native_card_deletion(connection, card_id)
            connection.execute("DELETE FROM cards WHERE id = ?", (card_id,))

        await self.database.write_async(write)

    async def check_card_deletion(self, card_id: str) -> None:
        await self.database.read_async(
            lambda connection: self._check_native_card_deletion(connection, card_id)
        )

    @staticmethod
    def _check_native_card_deletion(connection: sqlite3.Connection, card_id: str) -> None:
        linked = connection.execute(
            "WITH RECURSIVE ancestry(id, parent_id, runtime_json) AS ("
            "SELECT id, parent_id, runtime_json FROM cards WHERE id = ? UNION "
            "SELECT c.id, c.parent_id, c.runtime_json FROM cards c "
            "JOIN ancestry a ON c.id = a.parent_id) "
            "SELECT 1 FROM ancestry a JOIN native_tasks t ON "
            "t.id = json_extract(a.runtime_json, '$.native_task_id') OR "
            "json_extract(t.body_json, '$.workflow_card_id') = a.id "
            "JOIN native_factories f ON f.id = t.factory_id "
            "WHERE f.state != 'archived' LIMIT 1",
            (card_id,),
        ).fetchone()
        if linked:
            raise FactoryError(
                "card_in_use",
                "This Card retains a Factory task's results and resource consumption. "
                "Archive its Factory before deleting it.",
            )

    async def move_card(self, card_id: str, position: float) -> Json:
        at = utc_now_timestamp()

        def write(connection: sqlite3.Connection) -> Json:
            card = _get(connection, card_id)
            if card["state"] not in {"proposal", "queued"}:
                raise FactoryError(
                    "invalid_state",
                    "Only Proposals and queued Cards can be reordered. Refresh the Board.",
                )
            rows = list(
                connection.execute(
                    "SELECT id, position, revision FROM cards "
                    "WHERE state IN ('proposal', 'queued') ORDER BY position, created_at, id"
                )
            )
            target = next(
                (index for index, row in enumerate(rows) if row["position"] >= position),
                len(rows) - 1,
            )
            moved = next(row for row in rows if row["id"] == card_id)
            rows = [row for row in rows if row["id"] != card_id]
            rows.insert(target, moved)
            for index, row in enumerate(rows, 1):
                if row["position"] == index:
                    continue
                connection.execute(
                    "UPDATE cards SET position = ?, revision = ?, updated_at = ? WHERE id = ?",
                    (index, row["revision"] + 1, at, row["id"]),
                )
                _event(connection, row["id"], at, "user", "moved", {"position": index})
            return _get(connection, card_id)

        return await self.database.write_async(write)
