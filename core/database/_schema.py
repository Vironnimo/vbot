"""Declarative additive reconcile, kernel tables and the migration ledger.

The declared DDL is the single source of truth. The reconciler creates missing
tables, views, indexes and triggers from it and adds missing addable columns;
it never drops or rewrites an object. A live object whose declared shape
differs fails closed, because within one format generation a changed object
gets a new name. Extra live columns, tables and objects are tolerated: a newer
vBot may have added them, and runtime reads always name their columns.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from functools import cache

from core.database.errors import DatabaseCorruptError

KERNEL_SCHEMA_SQL = """
CREATE TABLE kernel_meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
) STRICT;

CREATE TABLE kernel_migrations (
  name               TEXT PRIMARY KEY,
  applied_at         TEXT NOT NULL,
  applied_by_version TEXT NOT NULL,
  breaks_older       INTEGER NOT NULL CHECK (breaks_older IN (0, 1))
) STRICT;
"""
KERNEL_TABLES = ("kernel_meta", "kernel_migrations")

_TABLE_CONSTRAINT_PREFIXES = ("CONSTRAINT", "PRIMARY", "UNIQUE", "CHECK", "FOREIGN")


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
    name in dependency order; ``object_text`` holds the normalized definition of
    every index, view and trigger, which must match a live object of the same
    name exactly.
    """

    objects: tuple[tuple[str, str, str], ...]
    table_columns: dict[str, dict[str, DeclaredColumn]]
    table_constraints: dict[str, tuple[str, ...]]
    table_suffixes: dict[str, str]
    object_text: dict[str, str]

    @property
    def readable_relations(self) -> tuple[str, ...]:
        """Every declared table and view, probed for readability on open."""
        return tuple(name for kind, name, _sql in self.objects if kind in {"table", "view"})


def quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _normalized_ddl(sql: str) -> str:
    return " ".join(sql.split())


@cache
def declared_schema(schema_sql: str) -> DeclaredSchema:
    """Parse the declared schema by executing the DDL in an in-memory database.

    SQLite itself resolves every statement shape - defaults, CHECKs, STRICT
    types, generated columns - so the declaration cannot drift from what the
    DDL means, and PRAGMA table_xinfo reports generated columns as hidden.
    Declarations are immutable process constants, so the parsed contract is
    reused across opens instead of rebuilding an in-memory database each time.
    """
    reference = sqlite3.connect(":memory:")
    try:
        reference.executescript(schema_sql)
        objects: list[tuple[str, str, str]] = []
        table_columns: dict[str, dict[str, DeclaredColumn]] = {}
        table_constraints: dict[str, tuple[str, ...]] = {}
        table_suffixes: dict[str, str] = {}
        object_text: dict[str, str] = {}
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
            else:
                object_text[str(name)] = _normalized_ddl(str(sql))
        return DeclaredSchema(
            objects=tuple(objects),
            table_columns=table_columns,
            table_constraints=table_constraints,
            table_suffixes=table_suffixes,
            object_text=object_text,
        )
    finally:
        reference.close()


def _declared_columns(
    reference: sqlite3.Connection,
    table_name: str,
    expressions: dict[str, str],
) -> list[DeclaredColumn]:
    columns: list[DeclaredColumn] = []
    for row in reference.execute(f"PRAGMA table_xinfo({quote_identifier(table_name)})"):
        # row: (cid, name, type, notnull, dflt_value, pk, hidden)
        _cid, name, type_name, notnull, default, pk, hidden = row
        expression = expressions.get(str(name))
        if expression is None:
            raise DatabaseCorruptError(
                f"schema declaration could not resolve column {table_name}.{name}"
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


def _table_declaration(sql: str) -> tuple[dict[str, str], tuple[str, ...], str]:
    """Return exact column expressions, table constraints, and table suffix."""
    open_index = sql.find("(")
    if open_index < 0:
        raise DatabaseCorruptError("schema declaration has no table body")
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
    raise DatabaseCorruptError("schema declaration has an unterminated table body")


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
        raise DatabaseCorruptError("schema declaration has an empty table item")
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
        raise DatabaseCorruptError("schema declaration has an unterminated identifier")
    parts = item.split(None, 1)
    if len(parts) != 2:
        raise DatabaseCorruptError(f"schema declaration lacks a column type: {item}")
    return parts[0], parts[1]


def schema_changes(
    connection: sqlite3.Connection,
    declared: DeclaredSchema,
    *,
    retired_indexes: tuple[str, ...] = (),
) -> list[tuple[str, str]]:
    """Plan the additive changes, and retired-index drops, for one live snapshot.

    Returns ``(statement, description)`` pairs; empty when the database is
    current. Raises ``DatabaseCorruptError`` for every shape the additive
    reconcile cannot express.
    """
    planned: list[tuple[str, str]] = []
    for table_name, columns in declared.table_columns.items():
        planned.extend(
            _missing_column_statements(
                table_name,
                columns,
                declared.table_constraints[table_name],
                declared.table_suffixes[table_name],
                connection,
            )
        )
    planned.extend(_missing_object_statements(declared, connection))
    if retired_indexes:
        live_indexes = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
        }
        for name in retired_indexes:
            if name in live_indexes:
                planned.append(
                    (f"DROP INDEX {quote_identifier(name)};", f"dropped retired index {name}")
                )
    return planned


def _missing_column_statements(
    table_name: str,
    columns: dict[str, DeclaredColumn],
    declared_constraints: tuple[str, ...],
    declared_suffix: str,
    connection: sqlite3.Connection,
) -> list[tuple[str, str]]:
    """ADD COLUMN statements for addable gaps; any other shape change fails closed."""
    live_rows = connection.execute(f"PRAGMA table_xinfo({quote_identifier(table_name)})").fetchall()
    if not live_rows:
        # The table is created whole by _missing_object_statements.
        return []
    live_sql_row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table_name,)
    ).fetchone()
    if live_sql_row is None or live_sql_row[0] is None:
        raise DatabaseCorruptError(f"schema mismatch: {table_name} has no declared SQL")
    live_expressions, live_constraints, live_suffix = _table_declaration(str(live_sql_row[0]))
    if live_constraints != declared_constraints or live_suffix.lower() != declared_suffix.lower():
        raise DatabaseCorruptError(
            f"schema mismatch: {table_name} constraints or options do not match the declared shape"
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
        raise DatabaseCorruptError(
            f"schema mismatch: {table_name} primary key {live_pk} does not match "
            f"declared {declared_pk}"
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
            raise DatabaseCorruptError(
                f"schema mismatch: {table_name}.{column.name} is missing and cannot be added "
                "with ADD COLUMN"
            )
        statements.append(
            (
                f"ALTER TABLE {quote_identifier(table_name)} ADD COLUMN "
                f"{quote_identifier(column.name)} {column.type_expression};",
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
        raise DatabaseCorruptError(
            f"schema mismatch: {table_name}.{column.name} does not match the declared shape "
            f"(live type {live_type or 'none'}, generated {live_generated}; declared type "
            f"{column.type_name or 'none'}, generated {column.generated})"
        )


def _missing_object_statements(
    declared: DeclaredSchema, connection: sqlite3.Connection
) -> list[tuple[str, str]]:
    """CREATE statements for declared objects the database lacks by name.

    A live index, view or trigger with a declared name but a different
    definition fails closed; the reconciler never drops it.
    """
    live = {
        str(row[1]): (str(row[0]), _normalized_ddl(str(row[2] or "")))
        for row in connection.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL"
        )
    }
    statements: list[tuple[str, str]] = []
    for kind, name, sql in declared.objects:
        live_object = live.get(name)
        if live_object is None:
            statements.append((f"{sql};", f"created {kind} {name}"))
            continue
        live_kind, live_text = live_object
        if live_kind != kind:
            raise DatabaseCorruptError(
                f"schema mismatch: {name} is a live {live_kind}, declared as a {kind}"
            )
        if kind != "table" and live_text != declared.object_text[name]:
            raise DatabaseCorruptError(
                f"schema mismatch: {kind} {name} does not match the declared definition; "
                "a changed definition needs a new name"
            )
    return statements
