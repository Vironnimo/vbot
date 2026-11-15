"""Public application services: explicit operations and bounded observations."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from core.extensions import ExtensionRegistrationIdentity
from core.extensions import _callbacks as extension_callbacks
from core.extensions.extensions import ExtensionDeclarations, ExtensionRecord, ExtensionRegistry
from core.extensions.services import (
    ExtensionService,
    ExtensionServiceCatalog,
    ExtensionServiceError,
    ExtensionSubscription,
    ServiceKind,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("exported", [False, True])
async def test_services_accept_only_bounded_json_objects(
    tmp_path: Path, exported: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.core.extensions.extension_test_support import write_extension

    catalog = ExtensionServiceCatalog()
    handler = AsyncMock(return_value={})
    schema = {"type": "object"}
    name = "fixture.read"
    if exported:
        write_extension(
            tmp_path,
            "fixture",
            "handler = None\n"
            "async def read(params): return await handler(params)\n"
            "def register(api):\n"
            "    api.register_service('read', 'query', {'type': 'object'}, read)\n",
        )
        registry = await ExtensionRegistry.aload(tmp_path)
        import sys

        monkeypatch.setattr(sys.modules["vbot_ext.fixture"], "handler", handler)
        catalog.install_exports(registry)
        name = "extension:fixture:read"
    else:
        catalog.bind([ExtensionService(name, "query", schema, handler)])
    services = catalog.for_owner(ExtensionRegistrationIdentity("consumer", "one"), lambda: None)
    try:
        for params in ({"value": object()}, {"value": float("nan")}, {"value": "x" * (256 * 1024)}):
            with pytest.raises(ExtensionServiceError) as invalid:
                await services.query(name, params)
            assert invalid.value.code == "invalid_request"
        handler.assert_not_called()
        results: tuple[Any, ...] = (
            [],
            {"value": object()},
            {"value": float("nan")},
            {"value": "x" * (8 * 1024 * 1024)},
        )
        for value in results:
            handler.return_value = value
            with pytest.raises(ExtensionServiceError) as invalid:
                await services.query(name)
            assert invalid.value.code == "invalid_result"
        handler.return_value = {"value": "x" * (8 * 1024 * 1024 - len('{"value": ""}'))}
        assert await services.query(name) == handler.return_value
        handler.return_value = {"value": (1, 2)}
        assert await services.query(name) == {"value": [1, 2]}
    finally:
        catalog.close()


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
@pytest.mark.parametrize(
    ("payload", "path", "constraint"),
    [
        ({"options": {}}, ["options", "limit"], "required"),
        ({"options": {"limit": "private-value"}}, ["options", "limit"], "type"),
        ({"options": {"limit": -1}}, ["options", "limit"], "minimum"),
        (
            {"options": {"limit": 1, "extra": "private-value"}},
            ["options", "extra"],
            "additionalProperties",
        ),
    ],
)
async def test_validation_reports_field_paths_without_submitted_values(
    payload: dict[str, Any], path: list[str], constraint: str
) -> None:
    catalog = ExtensionServiceCatalog()
    handler = AsyncMock(return_value={})
    catalog.bind(
        [
            ExtensionService(
                "fixture.read",
                "query",
                {
                    "type": "object",
                    "properties": {
                        "options": {
                            "type": "object",
                            "required": ["limit"],
                            "properties": {"limit": {"type": "integer", "minimum": 0}},
                            "additionalProperties": False,
                        }
                    },
                },
                handler,
            )
        ]
    )
    owner = catalog.for_owner(ExtensionRegistrationIdentity("fixture", "one"), lambda: None)
    with pytest.raises(ExtensionServiceError) as invalid:
        await owner.query("fixture.read", payload)
    assert invalid.value.code == "invalid_request"
    assert invalid.value.data["path"] == path
    assert invalid.value.data["validator"] == constraint
    assert invalid.value.data["schema_path"][-1] == constraint
    assert "private-value" not in str(invalid.value)
    assert "private-value" not in str(invalid.value.data)
    handler.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["query", "execute"])
@pytest.mark.parametrize("retirement", ["caller", "binding"])
async def test_retirement_rejects_reads_but_preserves_committed_action_results(
    kind: ServiceKind,
    retirement: str,
) -> None:
    catalog = ExtensionServiceCatalog()
    current = True
    entered, finish = asyncio.Event(), asyncio.Event()

    def require_current() -> None:
        if not current:
            raise ValueError("retired")

    async def handler(_params: dict[str, Any]) -> dict[str, Any]:
        entered.set()
        await finish.wait()
        return {"committed": True}

    owner = catalog.for_owner(ExtensionRegistrationIdentity("fixture", "one"), require_current)
    unbind = catalog.bind(
        [ExtensionService("fixture.operation", kind, {"type": "object"}, handler)]
    )
    pending = asyncio.create_task(getattr(owner, kind)("fixture.operation"))
    await entered.wait()
    if retirement == "caller":
        current = False
    else:
        unbind()
    finish.set()
    if kind == "query":
        with pytest.raises(ValueError):
            await pending
    else:
        assert await pending == {"committed": True}
    if retirement == "caller":
        with pytest.raises(ValueError):
            owner.describe()
    else:
        assert owner.describe()["available"] is False
    with pytest.raises(ValueError):
        await owner.execute("fixture.operation")
    catalog.close()


@pytest.mark.asyncio
async def test_observers_coalesce_scope_and_release_before_late_delivery(tmp_path: Path) -> None:
    # A Runtime may be assembled before entering its serving loop.
    catalog = await asyncio.to_thread(ExtensionServiceCatalog)
    registry = ExtensionRegistry()
    declarations = ExtensionDeclarations()
    registry._records.append(  # noqa: SLF001 - install the completed fixture registration
        ExtensionRecord(
            name="fixture",
            root_path=tmp_path,
            entry_path=tmp_path / "fixture.py",
            status="loaded",
            declarations=declarations,
        )
    )
    identity = registry.registration_identity("fixture")
    owner = catalog.for_owner(identity, lambda: None)
    received = []
    delivered = asyncio.Event()

    async def observer(event: dict[str, Any]) -> None:
        received.append(event)
        delivered.set()

    subscriptions: list[ExtensionSubscription] = []

    def startup() -> None:
        # Real synchronous lifecycle dispatch runs this on an Extension worker.
        with pytest.raises(RuntimeError):
            asyncio.get_running_loop()
        subscriptions.append(owner.subscribe(["sessions", "runs"], observer))

    declarations.startup.append(startup)
    await registry.fire_startup()
    assert len(subscriptions) == 1
    close = subscriptions[0]

    # All events arrive before dispatch, as one burst of domain changes may.
    def changes() -> None:
        for index in range(100):
            catalog.publish("sessions", {"session_id": str(index)})
            catalog.publish("runs", {"run_id": "same"})

    # Publishing in the same loop turn makes coalescing deterministic.
    changes()
    while len(received) < 2:
        delivered.clear()
        await delivered.wait()
    assert received == [
        {"kind": "sessions", "revision": 199},
        {"kind": "runs", "revision": 200, "scope": {"run_id": "same"}},
    ]
    delivered.clear()
    await asyncio.to_thread(catalog.publish, "sessions", {"session_id": "worker"})
    await delivered.wait()
    assert received[-1]["scope"] == {"session_id": "worker"}
    await catalog.release_owner(identity)
    await asyncio.to_thread(close)
    assert close.status["state"] == "closed"
    catalog.publish("sessions")
    assert len(received) == 3
    catalog.close()


@pytest.mark.asyncio
async def test_observer_failure_is_observable_and_recovers_without_blocking_other_owners() -> None:
    previous_tasks = asyncio.all_tasks()
    catalog = ExtensionServiceCatalog()
    delivered = asyncio.Event()
    errors = asyncio.Event()
    diagnostics: list[dict[str, Any]] = []
    calls = 0
    one = ExtensionRegistrationIdentity("one", "one")
    two = ExtensionRegistrationIdentity("two", "one")

    async def sometimes_failed(_event: dict[str, Any]) -> None:
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise RuntimeError("private-error-value")

    async def on_error(diagnostic: dict[str, Any]) -> None:
        diagnostics.append(diagnostic)
        errors.set()

    subscription = catalog.for_owner(one, lambda: None).subscribe(
        ["runs"], sometimes_failed, on_error=on_error
    )

    async def receive(_event: dict[str, Any]) -> None:
        delivered.set()

    catalog.for_owner(two, lambda: None).subscribe(["runs"], receive)
    for revision in (1, 2):
        delivered.clear()
        errors.clear()
        catalog.publish("runs")
        await errors.wait()
        await delivered.wait()
        assert subscription.status == {
            "state": "degraded",
            "failures": revision,
            "consecutive_failures": revision,
            "last_error": {
                "code": "callback_failed",
                "error_type": "RuntimeError",
                "resource": "runs",
                "revision": revision,
            },
        }
    diagnostics[-1]["code"] = "modified"
    assert subscription.status["last_error"]["code"] == "callback_failed"
    delivered.clear()
    catalog.publish("runs")
    await delivered.wait()
    # Await just this publication's observer Tasks, including their health update.
    await asyncio.gather(
        *(
            task
            for task in asyncio.all_tasks() - previous_tasks
            if task.get_name().startswith("extension-observer:")
        )
    )
    assert subscription.status == {
        "state": "active",
        "failures": 2,
        "consecutive_failures": 0,
        "last_error": None,
    }
    await catalog.release_owner(one)
    assert calls == 3
    delivered.clear()
    catalog.publish("runs")
    await delivered.wait()
    assert calls == 3
    await catalog.release_owner(two)
    catalog.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("retire_while_timed_out", [False, True])
async def test_timeout_retains_latest_invalidation_until_callback_settles(
    monkeypatch: pytest.MonkeyPatch, retire_while_timed_out: bool, tmp_path: Path
) -> None:
    catalog = ExtensionServiceCatalog()
    identity = ExtensionRegistrationIdentity("slow", "one")
    entered, cancelled, finish, recovered = (asyncio.Event() for _ in range(4))
    received: list[dict[str, Any]] = []
    callback_tasks: set[asyncio.Task[Any]] = set()
    real_wait = extension_callbacks._wait  # noqa: SLF001 - callback deadline seam

    async def expire_first(
        tasks: set[asyncio.Task[Any]], *, timeout: float | None = None
    ) -> tuple[set[asyncio.Task[Any]], set[asyncio.Task[Any]]]:
        if not callback_tasks:
            callback_tasks.update(tasks)
            await entered.wait()
            return set(), tasks
        return await real_wait(tasks, timeout=timeout)

    monkeypatch.setattr(extension_callbacks, "_wait", expire_first)

    async def callback(event: dict[str, Any]) -> None:
        received.append(event)
        if len(received) == 1:
            entered.set()
            try:
                await finish.wait()
            except asyncio.CancelledError:
                cancelled.set()
                await finish.wait()
        else:
            recovered.set()

    from tests.core.extensions.extension_test_support import write_extension

    write_extension(
        tmp_path,
        "slow",
        "async def read(params): return {}\n"
        "def register(api):\n"
        "    api.register_service('read', 'query', {'type': 'object'}, read)\n"
        "    api.register_service('write', 'execute', {'type': 'object'}, read)\n",
    )
    registry = await ExtensionRegistry.aload(tmp_path)
    catalog.install_exports(registry)
    services = catalog.for_owner(identity, lambda: None)
    subscription = services.subscribe(["runs"], callback)
    try:
        catalog.publish("runs")
        await cancelled.wait()
        assert subscription.status["state"] == "degraded"
        assert subscription.status["last_error"]["code"] == "callback_timeout"
        for kind, name in (("query", "read"), ("execute", "write")):
            with pytest.raises(ExtensionServiceError) as unavailable:
                await getattr(services, kind)(f"extension:slow:{name}")
            assert unavailable.value.code == "callback_unavailable"
        catalog.publish("runs", {"run_id": "first"})
        catalog.publish("runs", {"run_id": "latest"})
        if retire_while_timed_out:
            # Retirement never waits for an uncooperative detached callback.
            await catalog.release_owner(identity)
            assert subscription.status["state"] == "closed"
        finish.set()
        await asyncio.gather(*callback_tasks)
        if not retire_while_timed_out:
            await recovered.wait()
            assert received[-1] == {"kind": "runs", "revision": 3}
        assert len(received) == (1 if retire_while_timed_out else 2)
    finally:
        finish.set()
        await asyncio.gather(*callback_tasks)
        await catalog.release_owner(identity)
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
