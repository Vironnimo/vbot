"""Cancellation-safe mutation settlement keeps failures observable."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any

import pytest

from server.rpc._mutations import serialized_mutation, serialized_read
from server.rpc.errors import RPC_ERROR_INVALID_REQUEST, RpcError


@pytest.mark.asyncio
@pytest.mark.parametrize("expected_error", [False, True])
async def test_cancelled_mutation_logs_failure_after_settlement(
    caplog: pytest.LogCaptureFixture, expected_error: bool
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    state = SimpleNamespace()
    error = (
        RpcError(RPC_ERROR_INVALID_REQUEST, "private validation detail")
        if expected_error
        else RuntimeError("refresh failed")
    )

    async def mutate(state: Any, params: dict[str, Any]) -> dict[str, Any]:
        entered.set()
        await release.wait()
        raise error

    handler = serialized_mutation(mutate, lock_attribute="mutation_lock")
    with caplog.at_level(logging.WARNING, logger="vbot.server.rpc.mutations"):
        caller = asyncio.ensure_future(handler(state, {"private": "request content"}))
        try:
            await entered.wait()
            caller.cancel()
            await asyncio.sleep(0)
            assert not caller.done()
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await caller

    assert not state.mutation_lock.locked()
    records = [record for record in caplog.records if record.name == "vbot.server.rpc.mutations"]
    assert len(records) == 1
    assert records[0].levelno == (logging.WARNING if expected_error else logging.ERROR)
    assert (records[0].exc_info is not None) is not expected_error
    assert "request content" not in records[0].getMessage()
    assert "private validation detail" not in records[0].getMessage()
    assert not [record for record in caplog.records if record.name == "asyncio"]


@pytest.mark.asyncio
async def test_uncancelled_mutation_preserves_failure_for_dispatcher(
    caplog: pytest.LogCaptureFixture,
) -> None:
    error = RuntimeError("refresh failed")

    async def mutate(state: Any, params: dict[str, Any]) -> dict[str, Any]:
        raise error

    handler = serialized_mutation(mutate, lock_attribute="mutation_lock")
    with pytest.raises(RuntimeError) as raised:
        await handler(SimpleNamespace(), {})

    assert raised.value is error
    assert not [record for record in caplog.records if record.name == "vbot.server.rpc.mutations"]


@pytest.mark.asyncio
async def test_mutation_callbacks_share_read_lease_and_cannot_reenter_mutation() -> None:
    state = SimpleNamespace(value="before")
    callback_reading = asyncio.Event()
    release_callback = asyncio.Event()

    async def read(_state: Any, params: dict[str, Any]) -> dict[str, Any]:
        if params.get("callback"):
            callback_reading.set()
            await release_callback.wait()
        return {"value": state.value}

    read_handler = serialized_read(read, lock_attribute="mutation_lock")

    async def mutate(_state: Any, _params: dict[str, Any]) -> dict[str, Any]:
        state.value = "callback"
        with pytest.raises(RpcError, match="cannot start another mutation") as nested:
            await mutation_handler(state, {})
        assert nested.value.code == "mutation_in_progress"
        value = await asyncio.ensure_future(read_handler(state, {"callback": True}))
        state.value = "published"
        return value

    mutation_handler = serialized_mutation(mutate, lock_attribute="mutation_lock")
    mutation = asyncio.ensure_future(mutation_handler(state, {}))
    async with asyncio.timeout(2):
        await callback_reading.wait()
        unrelated = asyncio.ensure_future(read_handler(state, {}))
        await asyncio.sleep(0)
        assert not unrelated.done()
        release_callback.set()
        assert await mutation == {"value": "callback"}
        assert await unrelated == {"value": "published"}


@pytest.mark.asyncio
async def test_mutation_drains_admitted_reads_and_expires_inherited_lease() -> None:
    state = SimpleNamespace(value="first")
    reading = asyncio.Event()
    release_read = asyncio.Event()
    start_late_read = asyncio.Event()
    late_read_started = asyncio.Event()
    next_mutation_entered = asyncio.Event()
    release_next_mutation = asyncio.Event()
    children: list[asyncio.Future[dict[str, Any]]] = []

    async def read(_state: Any, params: dict[str, Any]) -> dict[str, Any]:
        if params.get("hold"):
            reading.set()
            await release_read.wait()
        return {"value": state.value}

    read_handler = serialized_read(read, lock_attribute="mutation_lock")

    async def later() -> dict[str, Any]:
        await start_late_read.wait()
        late_read_started.set()
        return await read_handler(state, {})

    async def mutate(_state: Any, params: dict[str, Any]) -> dict[str, Any]:
        if params.get("next"):
            next_mutation_entered.set()
            await release_next_mutation.wait()
            state.value = "second"
        else:
            children.extend(
                [
                    asyncio.ensure_future(read_handler(state, {"hold": True})),
                    asyncio.create_task(later()),
                ]
            )
            await reading.wait()
        return {}

    handler = serialized_mutation(mutate, lock_attribute="mutation_lock")
    first = asyncio.ensure_future(handler(state, {}))
    async with asyncio.timeout(2):
        await reading.wait()
        await asyncio.sleep(0)
        assert not first.done()
        assert state.mutation_lock.locked()
        release_read.set()
        await first
        assert await children[0] == {"value": "first"}
        second = asyncio.ensure_future(handler(state, {"next": True}))
        await next_mutation_entered.wait()
        start_late_read.set()
        await late_read_started.wait()
        assert not children[1].done()
        release_next_mutation.set()
        await second
        assert await children[1] == {"value": "second"}
