"""Public application services: explicit operations and bounded observations."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest

from core.extensions import ExtensionRegistrationIdentity
from core.extensions.services import (
    ExtensionService,
    ExtensionServiceCatalog,
    ExtensionServiceError,
    ServiceKind,
)


@pytest.mark.asyncio
async def test_services_keep_kinds_schemas_and_binding_identity() -> None:
    catalog = ExtensionServiceCatalog()
    owner = catalog.for_owner(ExtensionRegistrationIdentity("fixture", "one"), lambda: None)
    assert owner.describe()["available"] is False
    with pytest.raises(ExtensionServiceError) as unavailable:
        await owner.query("fixture.read")
    assert unavailable.value.code == "service_unavailable"

    handler = AsyncMock(return_value={"value": [1]})
    schema = {"type": "object", "properties": {}, "additionalProperties": False}
    first = catalog.bind([ExtensionService("fixture.read", "query", schema, handler)])
    for kind, name, params, code in (
        ("query", "private.operation", {}, "service_not_found"),
        ("execute", "fixture.read", {}, "service_kind_mismatch"),
        ("query", "fixture.read", {"undeclared": True}, "invalid_request"),
    ):
        with pytest.raises(ExtensionServiceError) as failure:
            await getattr(owner, kind)(name, params)
        assert failure.value.code == code
    handler.assert_not_called()
    schema["additionalProperties"] = True
    description = owner.describe()
    description["services"][0]["parameters"]["additionalProperties"] = True
    assert owner.describe()["services"][0]["parameters"]["additionalProperties"] is False
    result = await owner.query("fixture.read")
    result["value"].append(2)
    assert await owner.query("fixture.read") == {"value": [1]}

    second = catalog.bind([ExtensionService("fixture.read", "query", schema, handler)])
    first()
    assert owner.describe()["available"] is True
    second()
    assert owner.describe()["available"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["query", "execute"])
async def test_retirement_rejects_reads_but_preserves_committed_action_results(
    kind: ServiceKind,
) -> None:
    catalog = ExtensionServiceCatalog()
    current = True

    def require_current() -> None:
        if not current:
            raise ValueError("retired")

    async def handler(_params: dict[str, Any]) -> dict[str, Any]:
        nonlocal current
        current = False
        return {"committed": True}

    owner = catalog.for_owner(ExtensionRegistrationIdentity("fixture", "one"), require_current)
    catalog.bind([ExtensionService("fixture.operation", kind, {"type": "object"}, handler)])
    if kind == "query":
        with pytest.raises(ValueError):
            await owner.query("fixture.operation")
    else:
        assert await owner.execute("fixture.operation") == {"committed": True}
    with pytest.raises(ValueError):
        owner.describe()
    with pytest.raises(ValueError):
        await owner.execute("fixture.operation")


@pytest.mark.asyncio
async def test_observers_coalesce_scope_and_release_before_late_delivery() -> None:
    catalog = ExtensionServiceCatalog()
    identity = ExtensionRegistrationIdentity("fixture", "one")
    owner = catalog.for_owner(identity, lambda: None)
    received = []
    delivered = asyncio.Event()

    async def observer(event: dict[str, Any]) -> None:
        received.append(event)
        delivered.set()

    close = owner.subscribe(["sessions"], observer)

    # All events arrive before dispatch, as one burst of domain changes may.
    def changes() -> None:
        for index in range(100):
            catalog.publish("sessions", {"session_id": str(index)})

    # Publishing in the same loop turn makes coalescing deterministic.
    changes()
    await delivered.wait()
    assert received == [{"kind": "sessions", "revision": 100}]
    delivered.clear()
    await asyncio.to_thread(catalog.publish, "sessions", {"session_id": "worker"})
    await delivered.wait()
    assert received[-1]["scope"] == {"session_id": "worker"}
    await catalog.release_owner(identity)
    close()
    catalog.publish("sessions")
    assert len(received) == 2
    catalog.close()


@pytest.mark.asyncio
async def test_observer_failure_does_not_block_other_owners_or_repeat() -> None:
    catalog = ExtensionServiceCatalog()
    failed = AsyncMock(side_effect=RuntimeError("test-owned observer failure"))
    delivered = asyncio.Event()
    one = ExtensionRegistrationIdentity("one", "one")
    two = ExtensionRegistrationIdentity("two", "one")
    catalog.for_owner(one, lambda: None).subscribe(["runs"], failed)

    async def receive(_event: dict[str, Any]) -> None:
        delivered.set()

    catalog.for_owner(two, lambda: None).subscribe(["runs"], receive)
    catalog.publish("runs")
    await delivered.wait()
    await catalog.release_owner(one)
    assert failed.await_count == 1
    delivered.clear()
    catalog.publish("runs")
    await delivered.wait()
    assert failed.await_count == 1
    await catalog.release_owner(two)
    catalog.close()


@pytest.mark.asyncio
async def test_observer_can_retire_its_own_registration_without_cancelling_its_outcome() -> None:
    catalog = ExtensionServiceCatalog()
    identity = ExtensionRegistrationIdentity("self-retiring", "one")
    finished = asyncio.Event()
    received = []

    async def retire(event: dict[str, Any]) -> None:
        await catalog.release_owner(identity)
        received.append(event)
        finished.set()

    catalog.for_owner(identity, lambda: None).subscribe(["sessions"], retire)
    catalog.publish("sessions")
    await finished.wait()
    catalog.publish("sessions")
    assert received == [{"kind": "sessions", "revision": 1}]
    catalog.close()
