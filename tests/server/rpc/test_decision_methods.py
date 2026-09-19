from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.model_tasks.decision_types import DecisionError
from server.rpc.decision_methods import method_handlers
from server.rpc.dispatcher import dispatch_rpc


@pytest.mark.asyncio
async def test_control_start_dispatch_and_structured_error():
    start = AsyncMock(return_value={"id": "evaluation", "status": "running"})
    state = SimpleNamespace(runtime=SimpleNamespace(decisions=SimpleNamespace(start=start)))
    request = {
        "method": "decision.start",
        "params": {"id": "experiment", "revision": 2, "request_id": "request", "mode": "control"},
    }
    result = await dispatch_rpc(state, request, method_handlers())
    assert result["ok"]
    start.assert_awaited_once_with("experiment", 2, "request", "control")
    start.side_effect = DecisionError("fixture", code="conflict")
    result = await dispatch_rpc(state, request, method_handlers())
    assert result["error"]["code"] == "conflict"
    request["params"]["revision"] = True
    result = await dispatch_rpc(state, request, method_handlers())
    assert result["error"]["code"] == "invalid_request"
    assert start.await_count == 2
