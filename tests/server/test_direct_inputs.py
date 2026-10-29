"""The HTTP contract for caller-supplied tasks, independent of Device credentials."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from core.access.config import access_config
from server._direct_inputs import register_input_routes
from server.access import AccessService


@pytest_asyncio.fixture
async def input_http():
    app = FastAPI()
    receipt = {
        "request_id": "request",
        "session_id": "session",
        "run_id": None,
        "status": "queued",
        "status_url": "/api/inputs/input/requests/request",
    }
    completed = {**receipt, "status": "completed", "content": "answer"}
    service = SimpleNamespace(
        authenticate=AsyncMock(
            side_effect=lambda _identity, token, **_kwargs: token == "Bearer test-secret"
        ),
        submit=AsyncMock(return_value=receipt),
        result=AsyncMock(return_value=completed),
        wait=AsyncMock(return_value=completed),
    )
    app.state.runtime = SimpleNamespace(direct_inputs=service)
    app.state.agent_delete_lock = asyncio.Lock()
    register_input_routes(app, AccessService(access_config("local"), local_token=None))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        yield client, service


@pytest.mark.asyncio
async def test_submit_poll_and_optional_wait_share_one_request(input_http):
    client, service = input_http
    headers = {"Authorization": "Bearer test-secret", "Idempotency-Key": "task-1"}
    body = {"prompt": "Do this", "run_overrides": {"model": "fake/model"}}
    first = await client.post("/api/inputs/input", json=body, headers=headers)
    assert first.status_code == 202
    assert first.headers["location"] == first.json()["status_url"]
    assert first.headers["cache-control"] == "no-store"
    request_id = service.submit.call_args.kwargs["request_id"]
    waited = await client.post("/api/inputs/input", json={**body, "wait": True}, headers=headers)
    assert waited.status_code == 200 and waited.json()["content"] == "answer"
    assert service.submit.call_args.kwargs["request_id"] == request_id
    service.wait.assert_awaited_once_with("input", request_id, 30)
    polled = await client.get(first.headers["location"], headers=headers)
    assert polled.status_code == 200 and polled.json()["status"] == "completed"


@pytest.mark.asyncio
async def test_authentication_and_bounded_json_precede_submission(input_http):
    client, service = input_http
    for headers in (
        {},
        {"Authorization": "Bearer wrong"},
        [("Authorization", "Bearer test-secret"), ("Authorization", "Bearer test-secret")],
    ):
        refused = await client.post("/api/inputs/input", json={"prompt": "task"}, headers=headers)
        assert refused.status_code == 404
    headers = {"Authorization": "Bearer test-secret"}
    for body in (
        {"prompt": "task", "wait": "yes"},
        {"prompt": "task", "wait_timeout_seconds": 61},
        [],
    ):
        invalid = await client.post("/api/inputs/input", json=body, headers=headers)
        assert invalid.status_code == 400
    oversized = await client.post(
        "/api/inputs/input",
        content=b" " * 1_048_577,
        headers={**headers, "Content-Type": "application/json"},
    )
    assert oversized.status_code == 413
    service.submit.assert_not_awaited()
