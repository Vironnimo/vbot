"""Phase 4 backend contract coverage for the minimal WebUI."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast

from fastapi.testclient import TestClient  # type: ignore[import-not-found]

from core.providers.adapter import ProviderAdapter
from core.runtime import Runtime
from core.utils.config import Config
from server.app import create_app

JsonObject = dict[str, Any]


class Phase4Adapter:
    def __init__(self, *, block: bool = False) -> None:
        self._block = block
        self.request_started = asyncio.Event()
        self.release = asyncio.Event()

    async def send(
        self, _messages: list[JsonObject], *, model_id: str, **_kwargs: Any
    ) -> JsonObject:
        self.request_started.set()
        if self._block:
            await self.release.wait()
        return {"content": "Phase 4 response", "tool_calls": None}

    def normalize_response(self, response: JsonObject) -> JsonObject:
        return response


class Phase4Runtime(Runtime):
    def __init__(self, config: Config, adapter: Phase4Adapter | None = None) -> None:
        super().__init__(config)
        self.adapter = adapter or Phase4Adapter()

    def get_adapter(self, _provider_id: str) -> ProviderAdapter:
        return cast(ProviderAdapter, self.adapter)


def test_phase4_bootstrap_agent_and_current_history(tmp_path: Path) -> None:
    runtime = Phase4Runtime(Config(data_dir=tmp_path / "data"))
    app = create_app(runtime=runtime)

    with TestClient(app) as client:
        list_response = client.post("/api/rpc", json={"method": "agent.list", "params": {}})
        agent = list_response.json()["result"]["agents"][0]
        history_response = client.post(
            "/api/rpc",
            json={"method": "chat.history", "params": {"agent_id": agent["id"]}},
        )

    assert agent["id"] == "main"
    assert agent["name"] == "Main"
    assert agent["current_session_id"]
    assert history_response.json() == {
        "ok": True,
        "result": {"agent_id": "main", "session_id": agent["current_session_id"], "messages": []},
    }


def test_phase4_agent_crud_minimum_one_and_new_current_session(tmp_path: Path) -> None:
    runtime = Phase4Runtime(Config(data_dir=tmp_path / "data"))
    app = create_app(runtime=runtime)

    with TestClient(app) as client:
        last_delete_response = client.post(
            "/api/rpc", json={"method": "agent.delete", "params": {"id": "main"}}
        )
        create_response = client.post(
            "/api/rpc",
            json={
                "method": "agent.create",
                "params": {"id": "coder", "name": "Coder", "model": "openai/gpt-5.2"},
            },
        )
        created_agent = create_response.json()["result"]
        update_response = client.post(
            "/api/rpc",
            json={"method": "agent.update", "params": {"id": "coder", "name": "Updated Coder"}},
        )
        new_session_response = client.post(
            "/api/rpc",
            json={
                "method": "session.create",
                "params": {
                    "agent_id": "coder",
                    "session_id": "fresh-session",
                    "make_current": True,
                },
            },
        )
        list_response = client.post("/api/rpc", json={"method": "agent.list", "params": {}})
        delete_response = client.post(
            "/api/rpc", json={"method": "agent.delete", "params": {"id": "coder"}}
        )

    agents_by_id = {agent["id"]: agent for agent in list_response.json()["result"]["agents"]}
    assert last_delete_response.json()["error"]["code"] == "last_agent"
    assert created_agent["current_session_id"]
    assert update_response.json()["result"]["name"] == "Updated Coder"
    assert new_session_response.json()["result"] == {
        "agent_id": "coder",
        "session_id": "fresh-session",
    }
    assert agents_by_id["coder"]["current_session_id"] == "fresh-session"
    assert delete_response.json()["result"]["agent_id"] == "coder"


def test_phase4_history_strips_opaque_provider_metadata(tmp_path: Path) -> None:
    runtime = Phase4Runtime(Config(data_dir=tmp_path / "data"))
    app = create_app(runtime=runtime)

    with TestClient(app) as client:
        client.post(
            "/api/rpc",
            json={"method": "agent.update", "params": {"id": "main", "model": "openai/gpt-5.2"}},
        )
        agent = client.post("/api/rpc", json={"method": "agent.list", "params": {}}).json()[
            "result"
        ]["agents"][0]
        client.post(
            "/api/rpc",
            json={
                "method": "chat.send",
                "params": {
                    "agent_id": "main",
                    "session_id": agent["current_session_id"],
                    "content": "Hi",
                },
            },
        )
        history_response = client.post(
            "/api/rpc", json={"method": "chat.history", "params": {"agent_id": "main"}}
        )

    assert history_response.json()["ok"] is True
    assert "reasoning_meta" not in json.dumps(history_response.json())
    assert [message["role"] for message in history_response.json()["result"]["messages"]] == [
        "user",
        "assistant",
    ]


def test_phase4_stream_cancel_path_remains_compatible(tmp_path: Path) -> None:
    adapter = Phase4Adapter(block=True)
    runtime = Phase4Runtime(Config(data_dir=tmp_path / "data"), adapter=adapter)
    app = create_app(runtime=cast(Any, runtime))

    with TestClient(app) as client:
        client.post(
            "/api/rpc",
            json={"method": "agent.update", "params": {"id": "main", "model": "openai/gpt-5.2"}},
        )
        agent = client.post("/api/rpc", json={"method": "agent.list", "params": {}}).json()[
            "result"
        ]["agents"][0]
        stream_response = client.post(
            "/api/rpc",
            json={
                "method": "chat.stream",
                "params": {
                    "agent_id": "main",
                    "session_id": agent["current_session_id"],
                    "content": "Hi",
                },
            },
        )
        cancel_response = client.post(
            "/api/rpc",
            json={
                "method": "chat.cancel",
                "params": {"run_id": stream_response.json()["result"]["run_id"]},
            },
        )
        adapter.release.set()

    assert stream_response.json()["ok"] is True
    assert stream_response.json()["result"]["sse_url"].startswith("/api/runs/")
    assert cancel_response.json()["ok"] is True
    assert cancel_response.json()["result"]["status"] == "cancelled"
