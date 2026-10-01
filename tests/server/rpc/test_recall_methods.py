"""Tests for the semantic Recall index RPC handlers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from core.recall import IndexFailure, IndexStatus
from server.rpc.errors import RPC_ERROR_INVALID_REQUEST
from tests.server.rpc_test_support import StubAdapter, make_state, rpc_error, rpc_result

pytestmark = pytest.mark.asyncio

_FAILURE = IndexFailure.of("provider_rejected")
_STATUS = IndexStatus(
    semantic_enabled=True,
    state="error",
    provider="openrouter",
    model="text-embed",
    indexed=12,
    waiting=3,
    skipped=1,
    last_error=_FAILURE,
    next_attempt_at="2026-05-10T12:00:30Z",
    spent_requests=2,
    spent_input_tokens=480,
    spent_total_tokens=480,
    spent_cost=0.01,
    estimate_characters=900,
    estimate_tokens=225,
)


async def test_status_and_rebuild_return_the_index_status(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    recall: Any = state.runtime.recall
    recall.status = _STATUS

    status = await rpc_result(state, "recall.status")
    rebuilt = await rpc_result(state, "recall.rebuild_index")

    assert status == rebuilt == _STATUS.to_dict()
    assert status["last_error"] == {"code": "provider_rejected", "message": _FAILURE.message}
    assert recall.rebuilds == 1


@pytest.mark.parametrize("method", ["recall.status", "recall.rebuild_index"])
async def test_recall_methods_reject_unknown_fields(tmp_path: Path, method: str) -> None:
    state = make_state(tmp_path, StubAdapter())

    error = await rpc_error(state, method, force=True)

    assert error["code"] == RPC_ERROR_INVALID_REQUEST
    assert "force" in error["message"]
    assert state.runtime.recall.rebuilds == 0
