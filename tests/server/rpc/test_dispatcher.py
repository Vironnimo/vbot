"""Tests for RPC envelope dispatch and failure observability."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from core.database import (
    DatabaseCorruptError,
    DatabaseFormatError,
    DatabaseUnavailableError,
    IncidentConflictError,
)
from core.performance import PerformanceService
from core.performance.performance import reset_for_tests
from core.sessions.errors import SessionStoreCorruptError
from server.rpc.dispatcher import RpcMethodHandler, dispatch_rpc
from server.rpc.error_mapping import _map_expected_error
from server.rpc.errors import RPC_ERROR_DOMAIN, RPC_ERROR_INVALID_REQUEST, RpcError


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


@pytest.mark.parametrize(
    "error",
    [
        DatabaseUnavailableError("sessions: busy"),
        DatabaseCorruptError("sessions: damaged"),
        DatabaseFormatError("sessions: newer generation"),
        SessionStoreCorruptError("stored Session rows are invalid"),
    ],
    ids=["unavailable", "corrupt", "format", "owner-subclass"],
)
def test_every_database_failure_maps_to_one_domain_error(error: Exception) -> None:
    mapped = _map_expected_error(error)

    assert mapped.code == RPC_ERROR_DOMAIN
    assert mapped.message == str(error)


def test_a_superseded_incident_acknowledgement_is_an_invalid_request() -> None:
    mapped = _map_expected_error(IncidentConflictError("the recovery incident has changed"))

    assert mapped.code == RPC_ERROR_INVALID_REQUEST


@pytest.fixture
def performance(tmp_path: Path) -> Iterator[PerformanceService]:
    reset_for_tests()
    service = PerformanceService(tmp_path / "performance")
    yield service
    service.stop()
    reset_for_tests()


@pytest.mark.asyncio
async def test_registered_methods_are_measured_and_unknown_names_are_not(
    performance: PerformanceService,
) -> None:
    async def succeed(_state: Any, _params: dict[str, Any]) -> dict[str, Any]:
        return {}

    def reject(_state: Any, _params: dict[str, Any]) -> dict[str, Any]:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "rejected")

    handlers: dict[str, RpcMethodHandler] = {"example.succeed": succeed, "example.reject": reject}
    for method in ("example.succeed", "example.reject", "example.unknown"):
        await dispatch_rpc(object(), {"method": method}, handlers)

    metrics = (await performance.snapshot())["metrics"]
    assert metrics["rpc.example.succeed"]["count"] == 1
    assert metrics["rpc.example.reject"]["count"] == 1
    assert not any(name.startswith("rpc.example.unknown") for name in metrics)
