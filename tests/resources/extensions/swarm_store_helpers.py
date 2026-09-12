"""Shared fixtures and fakes for swarm store behavior tests."""

from __future__ import annotations

# mypy: disable-error-code=arg-type
import pytest_asyncio

from resources.extensions.swarm.store import SwarmStore


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


@pytest_asyncio.fixture
async def store(tmp_path):
    value = SwarmStore(tmp_path / "swarm.db")
    await value.open()
    yield value
    await value.close()


async def _swarm(store: SwarmStore, *, count: int = 2) -> dict[str, object]:
    profile = await store.save_profile(_profile(count=count), expected_revision=None)
    return await store.create_swarm(
        profile["id"],
        "Investigate",
        {"cwd": "C:/work"},
        request_id="start-1",
        expected_profile_revision=profile["revision"],
    )
