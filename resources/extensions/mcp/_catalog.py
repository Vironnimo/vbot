"""What the connection Tool's description shows of each connection's catalog.

The description of ``mcp_<id>`` names the application and lists the names of its
remote Tools. A catalog summary (server title and Tool names in server order) is
kept per connection, in memory and in the Extension database ``catalog``, so a
restart or reconnect publishes the same names before the server answers. Only a
new catalog changes a summary; removing the connection drops it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from core.extensions.databases import Database
from core.extensions.operations import ExtensionHost
from core.utils.errors import VBotError
from core.utils.timestamps import utc_now_timestamp

from ._definitions import (
    DESCRIPTION_TITLE_CHARACTERS,
    DESCRIPTION_TOOLS_CHARACTERS,
    MCP_DESCRIPTION_MORE_TOOLS,
    MCP_DESCRIPTION_NO_TOOLS,
    MCP_DESCRIPTION_TOOLS,
    MCP_DESCRIPTION_USAGE,
    SUMMARY_CHARACTERS,
)

CATALOG_DATABASE = "catalog"

# One row per connection. Changes stay additive; reads name their columns.
CATALOG_SCHEMA = """
CREATE TABLE connection_catalogs(
    connection_id TEXT PRIMARY KEY,
    server_title TEXT NOT NULL,
    tool_names TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;
"""


@dataclass(frozen=True)
class CatalogSummary:
    """The server's title ("" when it reports none) and its Tool names in server order."""

    title: str
    tools: tuple[str, ...]


def catalog_summary(catalog: dict[str, Any]) -> CatalogSummary:
    """Summarize a catalog the connection reported."""
    info = catalog.get("server_info")
    title = ""
    if isinstance(info, dict):
        for key in ("title", "name"):
            value = info.get(key)
            if isinstance(value, str) and value.strip():
                title = _capped(value, DESCRIPTION_TITLE_CHARACTERS)
                break
    return CatalogSummary(title, tuple(str(tool["name"]) for tool in catalog.get("tools", [])))


def connection_description(connection: str, about: str, tools: Sequence[str]) -> str:
    """Render the connection Tool's description.

    *about* is the user's description of the connection, else the server's title;
    empty leaves it out. *tools* are the remote Tool names in server order.
    """
    heading = _heading(connection, about)
    if not tools:
        listing = MCP_DESCRIPTION_NO_TOOLS
    else:
        shown, hidden = _shown(tools, DESCRIPTION_TOOLS_CHARACTERS)
        names = ", ".join(shown)
        if hidden:
            more = MCP_DESCRIPTION_MORE_TOOLS.format(count=hidden)
            names = f"{names}, {more}" if names else more
        listing = MCP_DESCRIPTION_TOOLS.format(names=names)
    return f"{heading} {MCP_DESCRIPTION_USAGE} {listing}"


def connection_summary(connection: str, about: str) -> str:
    """The line that lists the connection Tool while it is loaded on demand.

    It is the description's heading, which changes only with *about*, not with
    the catalog, so a catalog change does not re-list the Tool.
    """
    return _capped(_heading(connection, about), SUMMARY_CHARACTERS)


def _heading(connection: str, about: str) -> str:
    about = " ".join(about.split())
    heading = f"MCP connection {connection}"
    if about:
        heading = f"{heading}: {about}"
    if not heading.endswith((".", "!", "?")):
        heading += "."
    return heading


class CatalogSummaries:
    """Catalog summaries per connection and their durable copy."""

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger
        self._summaries: dict[str, CatalogSummary] = {}
        self._database: Database | None = None
        self._write_lock = asyncio.Lock()
        self._writes: set[asyncio.Task[None]] = set()

    async def open(self, host: ExtensionHost) -> None:
        """Load the saved summaries; without the database they live in memory only."""
        if host.open_database is None:
            return
        try:
            database = await host.open_database(CATALOG_DATABASE, CATALOG_SCHEMA)
            rows = await database.read_async(_read_rows)
        except (VBotError, ValueError, OSError, sqlite3.Error) as error:
            self._logger.warning("MCP catalog summaries are unavailable: %s", error)
            return
        self._database = database
        for connection, title, names in rows:
            try:
                tools = json.loads(names)
            except ValueError:
                tools = None
            if not isinstance(tools, list) or not all(isinstance(name, str) for name in tools):
                self._logger.warning(
                    "MCP catalog summary ignored: invalid Tool names (connection=%s)", connection
                )
                continue
            self._summaries[connection] = CatalogSummary(title, tuple(tools))

    async def close(self) -> None:
        """Finish the pending writes; later changes stay in memory."""
        while self._writes:
            await asyncio.gather(*tuple(self._writes), return_exceptions=True)
        self._database = None

    def get(self, connection: str) -> CatalogSummary | None:
        return self._summaries.get(connection)

    def record(self, connection: str, summary: CatalogSummary) -> None:
        """Keep the summary of a new catalog."""
        if self._summaries.get(connection) == summary:
            return
        self._summaries[connection] = summary
        self._save(connection)

    def forget(self, connection: str) -> None:
        """Drop a removed connection's summary."""
        if self._summaries.pop(connection, None) is not None:
            self._save(connection)

    def description(self, connection: str, about: str | None) -> str:
        """The connection Tool's description; *about* is the user's description."""
        summary = self._summaries.get(connection)
        tools = summary.tools if summary is not None else ()
        title = summary.title if summary is not None else ""
        return connection_description(connection, about or title, tools)

    def summary(self, connection: str, about: str | None) -> str:
        """The connection Tool's summary; *about* is the user's description."""
        known = self._summaries.get(connection)
        return connection_summary(connection, about or (known.title if known else ""))

    def _save(self, connection: str) -> None:
        if self._database is None:
            return
        task = asyncio.create_task(self._write(connection), name=f"mcp-catalog:{connection}")
        self._writes.add(task)
        task.add_done_callback(self._writes.discard)

    async def _write(self, connection: str) -> None:
        # Writes run one at a time, each saving the summary current when it starts,
        # so the last write always stores the latest summary.
        async with self._write_lock:
            database = self._database
            if database is None:
                return
            summary = self._summaries.get(connection)

            def write(connection_db: sqlite3.Connection) -> None:
                if summary is None:
                    connection_db.execute(
                        "DELETE FROM connection_catalogs WHERE connection_id=?", (connection,)
                    )
                    return
                connection_db.execute(
                    "INSERT INTO connection_catalogs(connection_id,server_title,tool_names,"
                    "updated_at) VALUES(?,?,?,?) ON CONFLICT(connection_id) DO UPDATE SET "
                    "server_title=excluded.server_title,tool_names=excluded.tool_names,"
                    "updated_at=excluded.updated_at",
                    (
                        connection,
                        summary.title,
                        json.dumps(list(summary.tools), ensure_ascii=False),
                        utc_now_timestamp(),
                    ),
                )

            try:
                await database.write_async(write)
            except (VBotError, OSError, sqlite3.Error) as error:
                self._logger.warning(
                    "MCP catalog summary was not saved (connection=%s): %s", connection, error
                )


def _read_rows(connection: sqlite3.Connection) -> list[tuple[str, str, str]]:
    rows = connection.execute(
        "SELECT connection_id,server_title,tool_names FROM connection_catalogs"
    ).fetchall()
    return [(str(row[0]), str(row[1]), str(row[2])) for row in rows]


def _capped(text: str, limit: int) -> str:
    line = " ".join(text.split())
    return line if len(line) <= limit else line[: limit - 3].rstrip() + "..."


def _shown(names: Sequence[str], limit: int) -> tuple[list[str], int]:
    """The leading names whose joined list fits *limit*, and how many are left out."""
    shown: list[str] = []
    length = 0
    for name in names:
        length += len(name) + (2 if shown else 0)
        if length > limit:
            break
        shown.append(name)
    return shown, len(names) - len(shown)
