"""Factory records and atomic lifecycle events on the host-owned database."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from core.extensions.databases import Database, Migration
from core.extensions.operations import ExtensionOperationError
from core.utils.ids import new_id
from core.utils.timestamps import utc_now_timestamp

type Json = dict[str, Any]

STATES = frozenset(
    {"proposal", "queued", "working", "reviewing", "waiting", "integrating", "done", "discarded"}
)
ACTIVE_STATES = frozenset({"working", "reviewing", "integrating"})
FINAL_STATES = frozenset({"done", "discarded"})
DATABASE_NAME = "factory"
# Tables evolve additively: a changed object gets a new name. ``setups`` replaces
# the role-based ``project_setups`` and ``outputs`` replaces ``reviews``; the
# ``workflows`` migration copies both into the new tables once. An output row's
# ``stage_id``/``stage_name`` hold the step; ``target`` the step it sends back to.
SCHEMA_SQL = """
CREATE TABLE setups (
 project_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1,
 workflows_json TEXT NOT NULL DEFAULT '[]', default_workflow TEXT NOT NULL DEFAULT '',
 turn_budget INTEGER NOT NULL DEFAULT 30, base_branch TEXT NOT NULL DEFAULT 'main',
 integration_check TEXT NOT NULL DEFAULT 'merged',
 revision INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL
) STRICT;
CREATE TABLE cards (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL, title TEXT NOT NULL,
 description TEXT NOT NULL, origin TEXT NOT NULL, origin_session TEXT NOT NULL DEFAULT '',
 origin_run TEXT NOT NULL DEFAULT '', state TEXT NOT NULL, waiting_reason TEXT NOT NULL DEFAULT '',
 position REAL NOT NULL, round INTEGER NOT NULL DEFAULT 0,
 branch TEXT NOT NULL DEFAULT '', base TEXT NOT NULL DEFAULT '', base_sha TEXT NOT NULL DEFAULT '',
 head_sha TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL DEFAULT '',
 checks TEXT NOT NULL DEFAULT '',
 recommendation TEXT NOT NULL DEFAULT '', integration_reference TEXT NOT NULL DEFAULT '',
 group_id TEXT NOT NULL UNIQUE, runtime_json TEXT NOT NULL DEFAULT '{}',
 revision INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 finished_at TEXT, workflow_id TEXT NOT NULL DEFAULT '', turns INTEGER NOT NULL DEFAULT 0
) STRICT;
CREATE INDEX cards_state_position ON cards(state, position, id);
CREATE INDEX cards_project_state ON cards(project_id, state);
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
CREATE TABLE library_templates (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, revision INTEGER NOT NULL,
 latest_version INTEGER NOT NULL, archived INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
) STRICT;
CREATE TABLE library_versions (
 template_id TEXT NOT NULL REFERENCES library_templates(id), version INTEGER NOT NULL,
 name TEXT NOT NULL, description TEXT NOT NULL, workflow_json TEXT NOT NULL,
 created_at TEXT NOT NULL, PRIMARY KEY(template_id, version)
) STRICT;
"""
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
_OUTPUT_COLUMNS = (
    "id",
    *_OUTPUT_FIELDS,
    "created_at",
)


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
        "checks": value["checks"],
        "branch": value["branch"],
        "base": value["base"],
        "base_sha": value["base_sha"],
        "head_sha": value["head_sha"],
        "findings": json.loads(value["findings_json"]),
        "target": value["target"],
        "created_at": value["created_at"],
    }


def _migrate_workflows(connection: sqlite3.Connection) -> None:
    """Copy role-based setups and reviews into Workflows and outputs (idempotent)."""
    from ._workflows import legacy_workflow

    tables = {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    if "project_setups" in tables:
        cursor = connection.execute(
            "SELECT project_id, enabled, worker_profile, worker_model, worker_effort, "
            "reviewer_profile, reviewer_model, reviewer_effort, review_rounds, turn_budget, "
            "base_branch, integration_check, revision, updated_at, worker_source, "
            "reviewer_source FROM project_setups"
        )
        columns = [column[0] for column in cursor.description]
        for row in cursor.fetchall():
            values = dict(zip(columns, tuple(row), strict=True))
            workflow = legacy_workflow(values)
            connection.execute(
                "INSERT OR IGNORE INTO setups (project_id, enabled, workflows_json, "
                "default_workflow, turn_budget, base_branch, integration_check, revision, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    values["project_id"],
                    values["enabled"],
                    _dump([workflow]),
                    workflow["id"],
                    values["turn_budget"],
                    values["base_branch"],
                    values["integration_check"],
                    values["revision"],
                    values["updated_at"],
                ),
            )
    if "reviews" in tables and not connection.execute("SELECT 1 FROM outputs LIMIT 1").fetchone():
        connection.execute(
            "INSERT INTO outputs (card_id, stage_id, stage_name, kind, lane, pass, "
            "participant_id, verdict, recommendation, findings_json, head_sha, created_at) "
            "SELECT card_id, 'review', 'Review', kind, 0, round, participant_id, verdict, "
            "recommendation, findings_json, reviewed_head_sha, created_at FROM reviews "
            "WHERE card_id IN (SELECT id FROM cards) ORDER BY created_at"
        )


def _migrate_steps(connection: sqlite3.Connection) -> None:
    """Rewrite setups whose Workflows still hold stages as steps (idempotent)."""
    from ._workflows import from_stages

    rows = connection.execute("SELECT project_id, workflows_json FROM setups").fetchall()
    for project_id, raw in rows:
        workflows = json.loads(raw)
        if not any(isinstance(item, dict) and "stages" in item for item in workflows):
            continue
        converted = [
            from_stages(item) if isinstance(item, dict) and "steps" not in item else item
            for item in workflows
        ]
        connection.execute(
            "UPDATE setups SET workflows_json = ? WHERE project_id = ?",
            (_dump(converted), project_id),
        )


# ``steps`` changes the meaning of saved Workflows; older code would misread them.
MIGRATIONS = (
    Migration("workflows", apply=_migrate_workflows),
    Migration("steps", breaks_older=True, apply=_migrate_steps),
    # Older Workflow writers would silently drop links to shared templates.
    Migration("library_links", breaks_older=True),
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
    "round",
    "turns",
    "branch",
    "base",
    "base_sha",
    "head_sha",
    "summary",
    "checks",
    "recommendation",
    "integration_reference",
    "group_id",
    "runtime_json",
    "revision",
    "created_at",
    "updated_at",
    "finished_at",
    "workflow_id",
)
_SETUP_FIELDS = (
    "project_id",
    "enabled",
    "workflows_json",
    "default_workflow",
    "turn_budget",
    "revision",
    "updated_at",
)
_SETUP_KEYS = (
    "project_id",
    "enabled",
    "workflows",
    "default_workflow",
    "turn_budget",
    "revision",
    "updated_at",
)
_CARD_COLUMNS = ", ".join(_CARD_FIELDS)
_SETUP_COLUMNS = ", ".join(_SETUP_FIELDS)
_TEMPLATE_COLUMNS = (
    "t.id, t.kind, t.revision, t.latest_version, t.archived, t.created_at, t.updated_at, "
    "v.version, v.name, v.description"
)
_MUTABLE_CARD_FIELDS = set(_CARD_FIELDS) - {
    "id",
    "project_id",
    "origin",
    "origin_session",
    "origin_run",
    "group_id",
    "created_at",
    "runtime_json",
    "updated_at",
    "revision",
}


class FactoryError(ExtensionOperationError):
    """An expected refusal, with an actionable English message."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message)


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _card(row: sqlite3.Row) -> Json:
    value = dict(row)
    value["runtime"] = json.loads(value.pop("runtime_json"))
    return value


def _setup(row: sqlite3.Row) -> Json:
    value = dict(row)
    value["enabled"] = bool(value["enabled"])
    value["workflows"] = json.loads(value.pop("workflows_json"))
    return value


def _template(row: sqlite3.Row) -> Json:
    value = dict(row)
    value["archived"] = bool(value["archived"])
    if "workflow_json" in value:
        value["workflow"] = json.loads(value.pop("workflow_json"))
    return value


def _get_template(connection: sqlite3.Connection, template_id: str, version: int | None) -> Json:
    row = connection.execute(
        f"SELECT {_TEMPLATE_COLUMNS}, v.workflow_json FROM library_templates t "
        "JOIN library_versions v ON v.template_id = t.id "
        "AND v.version = COALESCE(?, t.latest_version) WHERE t.id = ?",
        (version, template_id),
    ).fetchone()
    if row is None:
        raise FactoryError(
            "template_not_found",
            f"Template {template_id!r}"
            + (f" version {version}" if version is not None else "")
            + " does not exist. Read library.list or library.get to choose an existing version.",
        )
    value = _template(row)
    value["versions"] = [
        item[0]
        for item in connection.execute(
            "SELECT version FROM library_versions WHERE template_id = ? ORDER BY version DESC",
            (template_id,),
        )
    ]
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

    async def list_templates(
        self, *, include_archived: bool = False, after: str = "", limit: int = 50
    ) -> Json:
        def read(connection: sqlite3.Connection) -> Json:
            rows = connection.execute(
                f"SELECT {_TEMPLATE_COLUMNS} FROM library_templates t "
                "JOIN library_versions v ON v.template_id = t.id AND v.version = t.latest_version "
                "WHERE t.id > ? AND (? OR t.archived = 0) ORDER BY t.id LIMIT ?",
                (after, int(include_archived), limit + 1),
            ).fetchall()
            values = [_template(row) for row in rows[:limit]]
            return {
                "templates": values,
                "next_cursor": values[-1]["id"] if len(rows) > limit else "",
            }

        return await self.database.read_async(read)

    async def get_template(self, template_id: str, version: int | None = None) -> Json:
        return await self.database.read_async(
            lambda connection: _get_template(connection, template_id, version)
        )

    async def publish_template(self, values: Json, *, expected_revision: int) -> Json:
        """Append a version without changing any Project's adopted copy."""
        at = utc_now_timestamp()

        def write(connection: sqlite3.Connection) -> Json:
            template_id = values.get("id")
            if template_id:
                previous = _get_template(connection, template_id, None)
                if expected_revision != previous["revision"]:
                    raise FactoryError(
                        "revision_conflict",
                        "The template changed elsewhere. Read library.get before publishing.",
                    )
                if previous["archived"]:
                    raise FactoryError(
                        "template_archived",
                        "The template is archived. Restore it before publishing another version.",
                    )
                if values["kind"] != previous["kind"]:
                    raise FactoryError(
                        "template_kind_changed",
                        "A template keeps its kind. Publish a separate template for another kind.",
                    )
                version = previous["latest_version"] + 1
                connection.execute(
                    "UPDATE library_templates SET revision = revision + 1, latest_version = ?, "
                    "updated_at = ? WHERE id = ?",
                    (version, at, template_id),
                )
            else:
                if expected_revision != 0:
                    raise FactoryError(
                        "revision_conflict", "A new template has revision 0. Supply 0 or omit it."
                    )
                version = 1

                def claim(candidate: str) -> bool:
                    cursor = connection.execute(
                        "INSERT OR IGNORE INTO library_templates "
                        "(id, kind, revision, latest_version, archived, created_at, updated_at) "
                        "VALUES (?, ?, 1, 1, 0, ?, ?)",
                        (candidate, values["kind"], at, at),
                    )
                    return cursor.rowcount == 1

                template_id = new_id("ftpl", claim=claim)
            connection.execute(
                "INSERT INTO library_versions "
                "(template_id, version, name, description, workflow_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    template_id,
                    version,
                    values["name"],
                    values["description"],
                    _dump(values["workflow"]),
                    at,
                ),
            )
            return _get_template(connection, template_id, None)

        return await self.database.write_async(write)

    async def archive_template(
        self, template_id: str, *, expected_revision: int, archived: bool = True
    ) -> Json:
        """Hide a template from insertion while keeping versions and Project links readable."""
        at = utc_now_timestamp()

        def write(connection: sqlite3.Connection) -> Json:
            previous = _get_template(connection, template_id, None)
            if expected_revision != previous["revision"]:
                raise FactoryError(
                    "revision_conflict",
                    "The template changed elsewhere. Read library.get before "
                    "archiving or restoring.",
                )
            if previous["archived"] != archived:
                connection.execute(
                    "UPDATE library_templates SET archived = ?, revision = revision + 1, "
                    "updated_at = ? WHERE id = ?",
                    (int(archived), at, template_id),
                )
            return _get_template(connection, template_id, None)

        return await self.database.write_async(write)

    async def list_setups(self) -> list[Json]:
        return await self.database.read_async(
            lambda connection: [
                _setup(row)
                for row in connection.execute(
                    f"SELECT {_SETUP_COLUMNS} FROM setups ORDER BY project_id"
                )
            ]
        )

    async def get_setup(self, project_id: str) -> Json:
        def read(connection: sqlite3.Connection) -> Json:
            row = connection.execute(
                f"SELECT {_SETUP_COLUMNS} FROM setups WHERE project_id = ?", (project_id,)
            ).fetchone()
            if row is None:
                raise FactoryError(
                    "setup_not_found",
                    f"Project {project_id!r} has no Factory setup. Configure it in Setups first.",
                )
            return _setup(row)

        return await self.database.read_async(read)

    async def save_setup(self, values: Json, *, expected_revision: int | None = None) -> Json:
        at = utc_now_timestamp()

        def write(connection: sqlite3.Connection) -> Json:
            previous = connection.execute(
                f"SELECT {_SETUP_COLUMNS} FROM setups WHERE project_id = ?",
                (values["project_id"],),
            ).fetchone()
            revision = int(previous["revision"]) if previous is not None else 0
            if expected_revision is not None and expected_revision != revision:
                raise FactoryError(
                    "revision_conflict",
                    "The setup changed elsewhere. Refresh it before saving your changes.",
                )
            saved = {**values, "revision": revision + 1, "updated_at": at}
            saved["enabled"] = int(saved["enabled"])
            saved["workflows_json"] = _dump(saved.pop("workflows"))
            assignments = ", ".join(
                f"{name} = excluded.{name}" for name in _SETUP_FIELDS if name != "project_id"
            )
            connection.execute(
                f"INSERT INTO setups ({_SETUP_COLUMNS}) "
                f"VALUES ({', '.join('?' for _ in _SETUP_FIELDS)}) "
                f"ON CONFLICT(project_id) DO UPDATE SET {assignments}",
                tuple(saved[name] for name in _SETUP_FIELDS),
            )
            saved["enabled"] = bool(saved["enabled"])
            saved["workflows"] = json.loads(saved.pop("workflows_json"))
            return {name: saved[name] for name in (*_SETUP_KEYS,)}

        return await self.database.write_async(write)

    async def delete_setup(self, project_id: str) -> None:
        def write(connection: sqlite3.Connection) -> None:
            active = connection.execute(
                "SELECT id FROM cards WHERE project_id = ? "
                "AND state NOT IN ('done', 'discarded') LIMIT 1",
                (project_id,),
            ).fetchone()
            if active is not None:
                raise FactoryError(
                    "setup_in_use",
                    "This Project has unfinished Cards. "
                    "Finish or discard them before deleting its setup.",
                )
            connection.execute("DELETE FROM setups WHERE project_id = ?", (project_id,))

        await self.database.write_async(write)

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
    ) -> Json:
        if origin not in {"operator", "user", "automation", "agent"}:
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
                    "workflow_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                        _dump(runtime or {}),
                        at,
                        at,
                        workflow_id,
                    ),
                )
                return cursor.rowcount == 1

            card_id = new_id("fct", claim=claim)
            _event(
                connection,
                card_id,
                at,
                "user" if origin != "agent" else "factory",
                "created",
                {"origin": origin},
            )
            return _get(connection, card_id)

        return await self.database.write_async(write)

    async def get_card(self, card_id: str) -> Json:
        return await self.database.read_async(lambda connection: _get(connection, card_id))

    async def attention_count(self) -> int:
        return await self.database.read_async(
            lambda connection: int(
                connection.execute(
                    "SELECT COUNT(*) FROM cards WHERE state IN ('waiting', 'proposal')"
                ).fetchone()[0]
            )
        )

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
        event: str,
        detail: Json | None = None,
        comment: str | None = None,
        output: Json | None = None,
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
            if patch.get("state") in FINAL_STATES:
                changes["finished_at"] = at
            connection.execute(
                f"UPDATE cards SET {', '.join(f'{name} = ?' for name in changes)} WHERE id = ?",
                (*changes.values(), card_id),
            )
            event_detail = dict(detail or {})
            if "state" in patch:
                event_detail.update(previous_state=previous["state"], state=patch["state"])
            _event(connection, card_id, at, actor, event, event_detail)
            if comment is not None:
                connection.execute(
                    "INSERT INTO comments (card_id, at, author, text) VALUES (?, ?, ?, ?)",
                    (card_id, at, actor, comment),
                )
            if output is not None:
                # Columns of earlier Factory versions stay empty.
                values = (
                    dict.fromkeys(_OUTPUT_FIELDS, "")
                    | {
                        field: output.get(field, "")
                        for field in ("kind", "participant_id", "summary", "result", "target")
                    }
                    | {
                        "stage_id": output["step"],
                        "stage_name": output["step_name"],
                        "lane": 0,
                        "pass": output["pass"],
                        "findings_json": _dump(output.get("findings", [])),
                    }
                )
                connection.execute(
                    f"INSERT INTO outputs (card_id, {', '.join(_OUTPUT_FIELDS)}, created_at) "
                    f"VALUES (?, {', '.join('?' for _ in _OUTPUT_FIELDS)}, ?)",
                    (card_id, *(values[name] for name in _OUTPUT_FIELDS), at),
                )
            return _get(connection, card_id)

        return await self.database.write_async(write)

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
            connection.execute("DELETE FROM cards WHERE id = ?", (card_id,))

        await self.database.write_async(write)

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
