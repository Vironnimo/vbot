"""Shared fixtures and fakes for swarm store behavior tests."""

from __future__ import annotations

# mypy: disable-error-code=arg-type
from pathlib import Path

import pytest_asyncio

from core.database import open_offline_database
from core.extensions.databases import Database, extension_database_spec
from resources.extensions.swarm.store import SCHEMA_SQL, SwarmStore


def open_swarm_database(directory: Path, name: str = "swarm") -> Database:
    """Open a Swarm-schema kernel database outside any data store, for store tests."""
    return open_offline_database(extension_database_spec(directory, "swarm", name, SCHEMA_SQL))


def _profile(*, slug: str = "research", count: int = 2) -> dict[str, object]:
    return {
        "schema_version": 1,
        "slug": slug,
        "name": "Research",
        "participants": [{"model": "model-a", "count": count}],
        "working_directory": {"kind": "directory", "path": "C:/work"},
        "tool_access": {"mode": "selected", "allowed": []},
        "delivery": {},
    }


def query(store: SwarmStore, sql: str, parameters: tuple[object, ...] = ()) -> list:
    """Rows of one read-only query on the store's kernel database, for assertions."""
    with store._database._database.read() as connection:  # noqa: SLF001 - test inspection
        return connection.execute(sql, parameters).fetchall()


@pytest_asyncio.fixture
async def store(tmp_path):
    database = open_swarm_database(tmp_path)
    value = SwarmStore(database)
    await value.open()
    yield value
    await value.close()
    database.close()


async def _swarm(store: SwarmStore, *, count: int = 2) -> dict[str, object]:
    profile = await store.save_profile(_profile(count=count), expected_revision=None)
    return await store.create_swarm(
        profile["id"],
        "Investigate",
        {"cwd": "C:/work"},
        request_id="start-1",
        expected_profile_revision=profile["revision"],
    )
