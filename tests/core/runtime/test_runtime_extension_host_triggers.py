"""Owner-bound Extension triggers: Cron jobs an Extension owns and handles."""

from __future__ import annotations

from typing import Any

import pytest

from core.automation.cron import CronJobNotFoundError
from core.runtime import Runtime


@pytest.mark.asyncio
async def test_extension_triggers_are_owner_bound_and_retire_with_their_registration(
    runtime: Runtime,
) -> None:
    registry = runtime.extensions
    assert registry is not None
    root = runtime._extension_host()
    assert root.for_owner is not None
    swarm_identity = registry.registration_identity("swarm")
    swarm = root.for_owner(swarm_identity)
    factory = root.for_owner(registry.registration_identity("factory"))
    cron = runtime._cron_service
    assert cron is not None
    try:
        triggers = swarm.triggers
        assert triggers is not None and factory.triggers is not None
        created = await triggers.create("nightly", name="Nightly", schedule="0 3 * * *")
        assert {key: created[key] for key in ("key", "name", "schedule", "enabled")} == {
            "key": "nightly",
            "name": "Nightly",
            "schedule": "0 3 * * *",
            "enabled": True,
        }
        paused = await triggers.update(created["id"], enabled=False, name="Nightly run")
        assert (paused["enabled"], paused["name"]) == (False, "Nightly run")
        assert [item["id"] for item in triggers.list()] == [created["id"]]
        triggers.check_schedule("webhook")
        with pytest.raises(ValueError):
            triggers.check_schedule("every blue moon")
        # Another Extension and the user's Cron surfaces do not see it.
        assert factory.triggers.list() == []
        with pytest.raises(CronJobNotFoundError):
            factory.triggers.get(created["id"])
        assert cron.list_jobs() == []

        async def handler(_fire: dict[str, Any]) -> None:
            return None

        triggers.set_handler(handler)
        assert "swarm" in cron._owner_handlers
        registry.retire_registration()
        with pytest.raises(ValueError):
            triggers.list()
        with pytest.raises(ValueError):
            await triggers.create("late", name="Late", schedule="every 1h")
        assert root.release_owner is not None
        await root.release_owner(swarm_identity)
        # The retired registration's handler no longer receives fires.
        assert "swarm" not in cron._owner_handlers

        # Triggers of an Extension that is no longer installed are deleted.
        await runtime._archive_uninstalled_extension_groups(frozenset({"factory"}))
        assert cron.list_jobs(owner="swarm") == []
    finally:
        await runtime.aclose()
