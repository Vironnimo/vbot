"""Factory records and atomic lifecycle events on the host-owned database."""

from __future__ import annotations

import json
import secrets
import sqlite3
from typing import Any

from core.extensions.databases import Database, Migration
from core.extensions.operations import ExtensionOperationError
from core.utils.timestamps import utc_now_timestamp

from ._jobs_schema import SCHEMA_SQL as JOBS_SCHEMA_SQL
from ._jobs_store import FactoryJobsStore
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
SCHEMA_SQL += JOBS_SCHEMA_SQL

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
        "note": (
            "The Factory upgraded while this Card was running. Review its work and retry the step."
        ),
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
    Migration("factory_budget_changes"),
    Migration("agent_profile_models", apply=_remove_model_overrides),
)


class FactoryError(ExtensionOperationError):
    """An expected refusal, with an actionable English message."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message)


class FactoryStore:
    """Persist domain state without opening or inspecting canonical Session storage."""

    def __init__(self, database: Database) -> None:
        self.database = database
        self.native = NativeFactoryStore(database)
        self.jobs = FactoryJobsStore(database)
