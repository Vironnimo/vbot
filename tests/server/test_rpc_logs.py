"""Server RPC log handlers."""

from __future__ import annotations

from pathlib import Path

import pytest

from server.rpc.methods import dispatch_rpc
from tests.server.rpc_test_support import (
    JsonObject,
    StubAdapter,
    _no_models_dev_fetch,
    make_state,
)

__all__ = ["_no_models_dev_fetch"]


@pytest.mark.asyncio
async def test_log_list_returns_sorted_files_with_default_selection(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir(exist_ok=True)
    (logs_dir / "2026-05-09").write_text("", encoding="utf-8")
    (logs_dir / "2026-05-11").write_text("", encoding="utf-8")
    (logs_dir / "2026-05-10").write_text("", encoding="utf-8")

    response = await dispatch_rpc(state, {"method": "log.list", "params": {}})

    assert response == {
        "ok": True,
        "result": {
            "files": ["2026-05-11", "2026-05-10", "2026-05-09"],
            "default_file": "2026-05-11",
        },
    }


@pytest.mark.asyncio
async def test_log_list_rejects_params(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(state, {"method": "log.list", "params": {"extra": True}})

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_log_read_returns_structured_entries(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir(exist_ok=True)
    (logs_dir / "2026-05-11").write_text(
        "\n".join(
            [
                "2026-05-11 09:00:00 [INFO] vbot.server.app - Ready",
                "trace line",
                "2026-05-11 09:00:01 [ERROR] vbot.server.app - Failed",
            ]
        ),
        encoding="utf-8",
    )

    response = await dispatch_rpc(
        state,
        {"method": "log.read", "params": {"file": "2026-05-11"}},
    )

    assert response == {
        "ok": True,
        "result": {
            "file": "2026-05-11",
            "entries": [
                {
                    "timestamp": "2026-05-11 09:00:00",
                    "level": "info",
                    "logger_name": "vbot.server.app",
                    "message": "Ready",
                    "continuation": "trace line",
                    "raw": "2026-05-11 09:00:00 [INFO] vbot.server.app - Ready\ntrace line",
                },
                {
                    "timestamp": "2026-05-11 09:00:01",
                    "level": "error",
                    "logger_name": "vbot.server.app",
                    "message": "Failed",
                    "continuation": "",
                    "raw": "2026-05-11 09:00:01 [ERROR] vbot.server.app - Failed",
                },
            ],
            "cursor": response["result"]["cursor"],
        },
    }
    assert isinstance(response["result"]["cursor"], str)
    assert response["result"]["cursor"]


@pytest.mark.asyncio
async def test_log_read_filters_persisted_routine_websocket_noise(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir(exist_ok=True)
    (logs_dir / "2026-05-11").write_text(
        "\n".join(
            [
                "2026-05-11 09:00:00 [INFO] vbot.server.uvicorn - "
                '127.0.0.1:55090 - "WebSocket /ws" [accepted]',
                "2026-05-11 09:00:01 [INFO] vbot.server.uvicorn - connection open",
                "2026-05-11 09:00:02 [INFO] vbot.server.uvicorn - "
                '127.0.0.1:60756 - "WebSocket /ws/logs?cursor=abc" [accepted]',
                "2026-05-11 09:00:03 [INFO] vbot.server.uvicorn - connection closed",
                "2026-05-11 09:00:04 [WARN] vbot.server.uvicorn - keepalive ping timeout",
                "2026-05-11 09:00:05 [ERROR] vbot.server.uvicorn - opening handshake failed",
                "2026-05-11 09:00:06 [INFO] vbot.server.app - Ready",
            ]
        ),
        encoding="utf-8",
    )

    response = await dispatch_rpc(
        state,
        {"method": "log.read", "params": {"file": "2026-05-11"}},
    )

    assert response["ok"] is True
    assert response["result"]["entries"] == [
        {
            "timestamp": "2026-05-11 09:00:04",
            "level": "warn",
            "logger_name": "vbot.server.uvicorn",
            "message": "keepalive ping timeout",
            "continuation": "",
            "raw": "2026-05-11 09:00:04 [WARN] vbot.server.uvicorn - keepalive ping timeout",
        },
        {
            "timestamp": "2026-05-11 09:00:05",
            "level": "error",
            "logger_name": "vbot.server.uvicorn",
            "message": "opening handshake failed",
            "continuation": "",
            "raw": "2026-05-11 09:00:05 [ERROR] vbot.server.uvicorn - opening handshake failed",
        },
        {
            "timestamp": "2026-05-11 09:00:06",
            "level": "info",
            "logger_name": "vbot.server.app",
            "message": "Ready",
            "continuation": "",
            "raw": "2026-05-11 09:00:06 [INFO] vbot.server.app - Ready",
        },
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [
        {},
        {"file": ""},
        {"file": "../2026-05-11"},
        {"file": "2026-05-11", "extra": True},
    ],
)
async def test_log_read_rejects_invalid_requests(tmp_path: Path, params: JsonObject) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(state, {"method": "log.read", "params": params})

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_log_read_rejects_missing_file_with_domain_error(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(
        state,
        {"method": "log.read", "params": {"file": "2026-05-11"}},
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "domain_error"
