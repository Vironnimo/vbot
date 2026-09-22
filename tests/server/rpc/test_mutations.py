"""Cancellation-safe mutation settlement keeps failures observable."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any

import pytest

from server.rpc._mutations import serialized_mutation
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
