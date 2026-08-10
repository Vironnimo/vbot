"""Tests for RPC envelope dispatch and failure observability."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from server.rpc.dispatcher import dispatch_rpc
from server.rpc.error_mapping import _map_expected_error
from server.rpc.errors import RPC_ERROR_INVALID_REQUEST, RpcError


@pytest.mark.asyncio
async def test_expected_rpc_error_is_logged_without_request_params(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def reject(_state: Any, _params: dict[str, Any]) -> dict[str, Any]:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "secret request detail")

    with caplog.at_level(logging.WARNING, logger="vbot.server.rpc.dispatcher"):
        response = await dispatch_rpc(
            object(),
            {"method": "example.reject", "params": {"token": "do-not-log"}},
            {"example.reject": reject},
        )

    assert response["ok"] is False
    assert response["error"]["code"] == RPC_ERROR_INVALID_REQUEST
    assert caplog.messages == ["RPC request rejected (method=example.reject code=invalid_request)"]
    assert "do-not-log" not in caplog.text
    assert "secret request detail" not in caplog.text


@pytest.mark.asyncio
async def test_unexpected_rpc_error_is_logged_with_traceback_and_reraised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def fail(_state: Any, _params: dict[str, Any]) -> dict[str, Any]:
        raise KeyError("missing internal setting")

    with (
        caplog.at_level(logging.ERROR, logger="vbot.server.rpc.dispatcher"),
        pytest.raises(KeyError, match="missing internal setting"),
    ):
        await dispatch_rpc(
            object(),
            {"method": "example.fail", "params": {}},
            {"example.fail": fail},
        )

    assert caplog.messages == ["Unexpected RPC request failure (method=example.fail)"]
    assert caplog.records[0].exc_info is not None
    assert caplog.records[0].exc_info[0] is KeyError


def test_key_error_is_not_an_expected_domain_error() -> None:
    with pytest.raises(KeyError, match="missing internal setting"):
        _map_expected_error(KeyError("missing internal setting"))
