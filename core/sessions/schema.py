"""Declarative schema, runtime requirements, and additive reconciliation for the canonical SQLite Session store."""
# ruff: noqa: E501

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from core.sessions.errors import SessionStoreCorruptError

SCHEMA_VERSION = 1
# Databases below this schema version cannot be reconciled additively and
# require the offline converter; additive generations at or above it heal in
# place. Raise it only for destructive schema changes.
SCHEMA_CONVERSION_FLOOR = 1
MINIMUM_SQLITE_VERSION = (3, 37, 0)
APPLICATION_ID = 0x56424F54
JOURNAL_MODE_WAL = "wal"
JOURNAL_MODE_DELETE = "delete"

SCHEMA_SQL = """
CREATE TABLE sessions (
  session_key INTEGER PRIMARY KEY,
  generation_id TEXT NOT NULL UNIQUE,
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
  ON sessions (project_id, agent_id, last_message_at DESC, session_id)
  WHERE status = 'live';

CREATE TABLE messages (
  session_key INTEGER NOT NULL,
  seq INTEGER NOT NULL CHECK (seq >= 0),
  message_id TEXT NOT NULL,
  role TEXT NOT NULL,
  timestamp TEXT NOT NULL,
  message_json TEXT NOT NULL CHECK (json_valid(message_json) AND json_type(message_json) = 'object'),
  PRIMARY KEY (session_key, seq),
  FOREIGN KEY (session_key) REFERENCES sessions (session_key) ON DELETE CASCADE
) STRICT, WITHOUT ROWID;

CREATE INDEX messages_by_session_time
  ON messages (session_key, timestamp, seq);

CREATE INDEX messages_by_message_id
  ON messages (session_key, message_id);

CREATE TABLE continuation_records (
  session_key INTEGER NOT NULL,
  seq INTEGER NOT NULL CHECK (seq >= 0),
  record_json TEXT NOT NULL CHECK (json_valid(record_json) AND json_type(record_json) = 'object'),
  PRIMARY KEY (session_key, seq),
  FOREIGN KEY (session_key) REFERENCES sessions (session_key) ON DELETE CASCADE
) STRICT, WITHOUT ROWID;
"""


def _version_tuple(parts: tuple[int, ...]) -> tuple[int, int, int]:
    values = [int(part) for part in parts]
    values.extend([0] * (3 - len(values)))
    return values[0], values[1], values[2]


def is_wal_reset_vulnerable(version_info: tuple[int, ...]) -> bool:
    """Return whether *version_info* carries SQLite's WAL-reset corruption bug.

    Affected are 3.7.0 (2010-07-21) through 3.51.2 (2026-01-09); fixed in
    3.51.3 and later, with source backports in 3.50.7 and 3.44.6
    (https://sqlite.org/wal.html, "The WAL-Reset Bug"). The race needs WAL
    plus two connections writing or checkpointing at the same instant;
    pre-WAL builds cannot hit it.
    """
    info = _version_tuple(version_info)
    if info < (3, 7, 0):
        return False
    return not (
        info >= (3, 51, 3) or (3, 50, 7) <= info < (3, 51, 0) or (3, 44, 6) <= info < (3, 45, 0)
    )


def required_journal_mode(version_info: tuple[int, ...]) -> str:
    """Rollback journal on WAL-reset-vulnerable SQLite, WAL otherwise."""
    if is_wal_reset_vulnerable(version_info):
        return JOURNAL_MODE_DELETE
    return JOURNAL_MODE_WAL


@dataclass(frozen=True)
class DeclaredColumn:
    """One declared column with the type expression ADD COLUMN needs."""

    name: str
    type_name: str
    type_expression: str
    primary_key_position: int
    generated: bool
    required_without_default: bool

    @property
    def addable(self) -> bool:
        """Whether SQLite can add the column without rewriting existing rows."""
        return (
            not self.generated
            and self.primary_key_position == 0
            and not self.required_without_default
        )


@dataclass(frozen=True)
class DeclaredSchema:
    """The schema parsed from its DDL, ready to diff against a live database.

    ``objects`` carries every CREATE statement with its sqlite_master kind and
    name in dependency order; a database missing one by name gets it created
    verbatim. ``unique_index_text`` holds the normalized definition of unique
    indexes, whose shape is a constraint the store relies on.
    """

    objects: tuple[tuple[str, str, str], ...]
    table_columns: dict[str, dict[str, DeclaredColumn]]
    table_constraints: dict[str, tuple[str, ...]]
    table_suffixes: dict[str, str]
    index_text: dict[str, str]
    unique_index_text: dict[str, str]


def _quote_identifier(name: str) -> str:
    return name.replace('"', '""')


def _normalized_ddl(sql: str) -> str:
    return " ".join(sql.split())


def declared_schema(schema_sql: str) -> DeclaredSchema:
    """Parse the declared schema by executing the DDL in an in-memory database.

    SQLite itself resolves every statement shape — defaults, CHECKs, STRICT
    types, generated columns — so the declaration cannot drift from what the
    DDL means, and PRAGMA table_xinfo reports generated columns as hidden.
    The parse runs once per store construction on a three-table schema, so
    its result is not cached.
    """
    reference = sqlite3.connect(":memory:")
    try:
        reference.executescript(schema_sql)
        objects: list[tuple[str, str, str]] = []
        table_columns: dict[str, dict[str, DeclaredColumn]] = {}
        table_constraints: dict[str, tuple[str, ...]] = {}
        table_suffixes: dict[str, str] = {}
        index_text: dict[str, str] = {}
        unique_index_text: dict[str, str] = {}
        for kind, name, sql in reference.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY rowid"
        ):
            if str(name).startswith("sqlite_"):
                continue
            objects.append((str(kind), str(name), str(sql)))
            if kind == "table":
                expressions, constraints, suffix = _table_declaration(str(sql))
                table_columns[str(name)] = {
                    column.name: column
                    for column in _declared_columns(reference, str(name), expressions)
                }
                table_constraints[str(name)] = constraints
                table_suffixes[str(name)] = suffix
            elif kind == "index":
                normalized = _normalized_ddl(str(sql))
                index_text[str(name)] = normalized
                if normalized.upper().startswith("CREATE UNIQUE INDEX"):
                    unique_index_text[str(name)] = normalized
        return DeclaredSchema(
            objects=tuple(objects),
            table_columns=table_columns,
            table_constraints=table_constraints,
            table_suffixes=table_suffixes,
            index_text=index_text,
            unique_index_text=unique_index_text,
        )
    finally:
        reference.close()


def _declared_columns(
    reference: sqlite3.Connection,
    table_name: str,
    expressions: dict[str, str],
) -> list[DeclaredColumn]:
    columns: list[DeclaredColumn] = []
    for row in reference.execute(f'PRAGMA table_xinfo("{_quote_identifier(table_name)}")'):
        # row: (cid, name, type, notnull, dflt_value, pk, hidden)
        _cid, name, type_name, notnull, default, pk, hidden = row
        expression = expressions.get(str(name))
        if expression is None:
            raise SessionStoreCorruptError(
                f"Session schema declaration could not resolve column {table_name}.{name}"
            )
        columns.append(
            DeclaredColumn(
                name=str(name),
                type_name=str(type_name or ""),
                type_expression=expression,
                primary_key_position=int(pk),
                generated=bool(hidden),
                required_without_default=bool(notnull and default is None and not pk),
            )
        )
    return columns


_TABLE_CONSTRAINT_PREFIXES = ("CONSTRAINT", "PRIMARY", "UNIQUE", "CHECK", "FOREIGN")


def _table_declaration(sql: str) -> tuple[dict[str, str], tuple[str, ...], str]:
    """Return exact column expressions, table constraints, and table suffix."""
    open_index = sql.find("(")
    if open_index < 0:
        raise SessionStoreCorruptError("Session schema declaration has no table body")
    close_index = _matching_parenthesis(sql, open_index)
    expressions: dict[str, str] = {}
    constraints: list[str] = []
    for item in _split_sql_items(sql[open_index + 1 : close_index]):
        stripped = item.strip()
        first, remainder = _first_sql_token(stripped)
        if first.upper() in _TABLE_CONSTRAINT_PREFIXES:
            constraints.append(_normalized_ddl(stripped))
            continue
        expressions[first] = remainder.strip()
    return expressions, tuple(constraints), _normalized_ddl(sql[close_index + 1 :].rstrip(";"))


def _matching_parenthesis(sql: str, open_index: int) -> int:
    depth = 0
    quote: str | None = None
    index = open_index
    while index < len(sql):
        character = sql[index]
        if quote is not None:
            if quote == "]" and character == "]":
                quote = None
            elif character == quote:
                if index + 1 < len(sql) and sql[index + 1] == quote:
                    index += 1
                else:
                    quote = None
        elif character in {"'", '"', "`"}:
            quote = character
        elif character == "[":
            quote = "]"
        elif character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    raise SessionStoreCorruptError("Session schema declaration has an unterminated table body")


def _split_sql_items(body: str) -> list[str]:
    items: list[str] = []
    start = 0
    depth = 0
    quote: str | None = None
    index = 0
    while index < len(body):
        character = body[index]
        if quote is not None:
            if quote == "]" and character == "]":
                quote = None
            elif character == quote:
                if index + 1 < len(body) and body[index + 1] == quote:
                    index += 1
                else:
                    quote = None
        elif character in {"'", '"', "`"}:
            quote = character
        elif character == "[":
            quote = "]"
        elif character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
        elif character == "," and depth == 0:
            items.append(body[start:index])
            start = index + 1
        index += 1
    items.append(body[start:])
    return [item for item in items if item.strip()]


def _first_sql_token(item: str) -> tuple[str, str]:
    if not item:
        raise SessionStoreCorruptError("Session schema declaration has an empty table item")
    opening = item[0]
    if opening in {'"', "`", "["}:
        closing = "]" if opening == "[" else opening
        index = 1
        token: list[str] = []
        while index < len(item):
            character = item[index]
            if character == closing:
                if closing != "]" and index + 1 < len(item) and item[index + 1] == closing:
                    token.append(closing)
                    index += 2
                    continue
                return "".join(token), item[index + 1 :]
            token.append(character)
            index += 1
        raise SessionStoreCorruptError("Session schema declaration has an unterminated identifier")
    parts = item.split(None, 1)
    if len(parts) != 2:
        raise SessionStoreCorruptError(f"Session schema declaration lacks a column type: {item}")
    return parts[0], parts[1]


def reconcile_schema(connection: sqlite3.Connection, *, schema_sql: str | None = None) -> list[str]:
    """Bring an existing database up to the declared schema, additively.

    The declared DDL is the single source of truth: missing addable columns
    are added with ADD COLUMN, missing tables, indexes, and triggers are
    created from the parsed DDL text, and a lagging ``PRAGMA user_version``
    is bumped — all in one transaction, so an interrupted reconcile leaves
    no partial state behind.

    Shape changes ADD COLUMN cannot express — a missing generated or
    primary-key column, a column type mismatch, a primary-key mismatch, a
    diverged unique index — raise ``SessionStoreCorruptError`` with an
    offline-conversion instruction instead of serving a stale shape. Extra
    live objects and columns are tolerated.

    Returns the applied changes; empty when the database was already current.
    """
    declared = declared_schema(schema_sql or SCHEMA_SQL)
    applied: list[tuple[str, str]] = []
    for table_name, columns in declared.table_columns.items():
        applied.extend(
            _missing_column_statements(
                table_name,
                columns,
                declared.table_constraints[table_name],
                declared.table_suffixes[table_name],
                connection,
            )
        )
    applied.extend(_missing_object_statements(declared, connection))
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version < SCHEMA_VERSION:
        applied.append(
            (
                f"PRAGMA user_version = {SCHEMA_VERSION};",
                f"schema version {version} -> {SCHEMA_VERSION}",
            )
        )
    if not applied:
        return []
    connection.executescript(
        "BEGIN IMMEDIATE;\n" + "\n".join(statement for statement, _change in applied) + "\nCOMMIT;"
    )
    return [change for _statement, change in applied]


def _missing_column_statements(
    table_name: str,
    columns: dict[str, DeclaredColumn],
    declared_constraints: tuple[str, ...],
    declared_suffix: str,
    connection: sqlite3.Connection,
) -> list[tuple[str, str]]:
    """ADD COLUMN statements for addable gaps; destructive shape fails closed."""
    live_rows = connection.execute(
        f'PRAGMA table_xinfo("{_quote_identifier(table_name)}")'
    ).fetchall()
    if not live_rows:
        # The table is created whole by _missing_object_statements.
        return []
    live_sql_row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table_name,)
    ).fetchone()
    if live_sql_row is None or live_sql_row[0] is None:
        raise SessionStoreCorruptError(
            f"Session database requires offline conversion: {table_name} has no declared SQL"
        )
    live_expressions, live_constraints, live_suffix = _table_declaration(str(live_sql_row[0]))
    if live_constraints != declared_constraints or live_suffix.lower() != declared_suffix.lower():
        raise SessionStoreCorruptError(
            f"Session database requires offline conversion: {table_name} constraints or options "
            "do not match the declared shape"
        )
    live_types = {str(row[1]): str(row[2] or "") for row in live_rows}
    live_generated = {str(row[1]): bool(row[6]) for row in live_rows}
    declared_pk = [
        column.name
        for column in sorted(
            (column for column in columns.values() if column.primary_key_position),
            key=lambda column: column.primary_key_position,
        )
    ]
    live_pk = [
        str(row[1]) for row in sorted((row for row in live_rows if row[5]), key=lambda row: row[5])
    ]
    if declared_pk != live_pk:
        raise SessionStoreCorruptError(
            f"Session database requires offline conversion: {table_name} primary key "
            f"{live_pk} does not match declared {declared_pk}"
        )
    statements: list[tuple[str, str]] = []
    for column in columns.values():
        if column.name in live_types:
            _verify_declared_shape(
                table_name,
                column,
                live_types[column.name],
                live_generated[column.name],
                live_expressions.get(column.name),
            )
            continue
        if not column.addable:
            raise SessionStoreCorruptError(
                f"Session database requires offline conversion: "
                f"{table_name}.{column.name} is missing and cannot be added with ADD COLUMN"
            )
        statements.append(
            (
                f'ALTER TABLE "{_quote_identifier(table_name)}" ADD COLUMN '
                f'"{_quote_identifier(column.name)}" {column.type_expression};',
                f"added column {table_name}.{column.name}",
            )
        )
    return statements


def _verify_declared_shape(
    table_name: str,
    column: DeclaredColumn,
    live_type: str,
    live_generated: bool,
    live_expression: str | None,
) -> None:
    expression_matches = (
        live_expression is not None
        and _normalized_ddl(live_expression).lower()
        == _normalized_ddl(column.type_expression).lower()
    )
    if (
        column.type_name.lower() != live_type.lower()
        or column.generated != live_generated
        or not expression_matches
    ):
        raise SessionStoreCorruptError(
            f"Session database requires offline conversion: {table_name}.{column.name} "
            f"does not match the declared shape (live type {live_type or 'none'}, "
            f"generated {live_generated}; declared type {column.type_name or 'none'}, "
            f"generated {column.generated})"
        )


def _missing_object_statements(
    declared: DeclaredSchema, connection: sqlite3.Connection
) -> list[tuple[str, str]]:
    """CREATE statements for declared objects the database lacks by name."""
    live_names = {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE sql IS NOT NULL")
    }
    live_index_text = {
        str(row[0]): str(row[1] or "")
        for row in connection.execute("SELECT name, sql FROM sqlite_master WHERE type = 'index'")
    }
    statements: list[tuple[str, str]] = []
    for kind, name, sql in declared.objects:
        if name not in live_names:
            statements.append((f"{sql};", f"created {kind} {name}"))
            continue
        if kind == "index":
            declared_text = declared.index_text[name]
            live_text = _normalized_ddl(live_index_text.get(name, ""))
            if live_text == declared_text:
                continue
            if name in declared.unique_index_text:
                _verify_unique_index_shape(name, declared_text, live_text)
            statements.extend(
                (
                    (f'DROP INDEX "{_quote_identifier(name)}";', f"dropped stale index {name}"),
                    (f"{sql};", f"recreated index {name}"),
                )
            )
    return statements


def _verify_unique_index_shape(name: str, declared_text: str, live_text: str) -> None:
    if _normalized_ddl(live_text) != declared_text:
        raise SessionStoreCorruptError(
            f"Session database requires offline conversion: unique index "
            f"{name} does not match the declared definition"
        )
