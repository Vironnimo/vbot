"""Server integration tests using the real Runtime."""

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


class StubAdapter:
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
        return {"content": "ok", "tool_calls": None}

    def normalize_response(
        self, response: JsonObject, *, model_id: str | None = None
    ) -> JsonObject:
        return response

    async def stream(self, _messages: list[JsonObject], *, model_id: str, **_kwargs: Any) -> Any:
        self.request_started.set()
        if self._block:
            await self.release.wait()
        yield {"type": "content_delta", "text": "ok"}
        yield {"type": "finish", "reason": "stop"}


class StubRuntime(Runtime):
    def __init__(self, config: Config, adapter: StubAdapter | None = None) -> None:
        super().__init__(config)
        self.adapter = adapter or StubAdapter()

    def get_adapter(self, _provider_id: str, _connection_id: str) -> ProviderAdapter:
        return cast(ProviderAdapter, self.adapter)


def test_bootstrap_agent_and_current_history(tmp_path: Path) -> None:
    runtime = StubRuntime(Config(data_dir=tmp_path / "data"))
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
        "result": {
            "agent_id": "main",
            "session_id": agent["current_session_id"],
            "messages": [],
            "has_more": False,
            "session_usage": {
                "measured_turns": 0,
                "estimated_turns": 0,
                "cache_turns": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
            },
        },
    }


def test_invalid_agent_config_does_not_block_application_startup(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    invalid_agent_dir = data_dir / "agents" / "main"
    invalid_agent_dir.mkdir(parents=True)
    (invalid_agent_dir / "agent.json").write_text(
        json.dumps({"name": "Missing id"}), encoding="utf-8"
    )
    runtime = StubRuntime(Config(data_dir=data_dir))
    app = create_app(runtime=runtime)

    with TestClient(app) as client:
        response = client.post("/api/rpc", json={"method": "agent.list", "params": {}})

    assert response.status_code == 200
    assert [agent["id"] for agent in response.json()["result"]["agents"]] == ["main-2"]


def test_invalid_settings_do_not_block_application_startup(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    (data_dir / "settings.json").write_text(
        json.dumps({"server_port": 8500, "compaction": {"enabled": "yes"}}),
        encoding="utf-8",
    )
    app = create_app(runtime=StubRuntime(Config(data_dir=data_dir)))

    with TestClient(app) as client:
        assert app.state.server_bind["listen_port"] == 8500
        response = client.post("/api/rpc", json={"method": "agent.list", "params": {}})

    assert response.status_code == 200
    assert [agent["id"] for agent in response.json()["result"]["agents"]] == ["main"]


def test_invalid_cron_storage_does_not_block_application_startup(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    jobs_path = data_dir / "cron" / "jobs.json"
    jobs_path.parent.mkdir(parents=True)
    jobs_path.write_text("{", encoding="utf-8")
    app = create_app(runtime=StubRuntime(Config(data_dir=data_dir)))

    with TestClient(app) as client:
        agent_response = client.post("/api/rpc", json={"method": "agent.list", "params": {}})
        cron_response = client.post("/api/rpc", json={"method": "cron.list", "params": {}})

    assert agent_response.status_code == 200
    assert [agent["id"] for agent in agent_response.json()["result"]["agents"]] == ["main"]
    assert cron_response.status_code == 200
    assert cron_response.json()["result"]["jobs"] == []
    assert jobs_path.read_text(encoding="utf-8") == "{"


def test_agent_crud_minimum_one_and_new_current_session(tmp_path: Path) -> None:
    runtime = StubRuntime(Config(data_dir=tmp_path / "data"))
    app = create_app(runtime=runtime)

    with TestClient(app) as client:
        last_delete_response = client.post(
            "/api/rpc", json={"method": "agent.delete", "params": {"id": "main"}}
        )
        create_response = client.post(
            "/api/rpc",
            json={
                "method": "agent.create",
                "params": {"id": "coder"},
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
    assert created_agent["name"] == "coder"
    assert update_response.json()["result"]["name"] == "Updated Coder"
    assert new_session_response.json()["result"] == {
        "agent_id": "coder",
        "session_id": "fresh-session",
    }
    assert agents_by_id["coder"]["current_session_id"] == "fresh-session"
    assert delete_response.json()["result"]["agent_id"] == "coder"


def test_agent_rename_keeps_complete_identity_tree_usable_through_real_runtime(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    runtime = StubRuntime(Config(data_dir=data_dir))
    app = create_app(runtime=runtime)

    with TestClient(app) as client:
        client.post(
            "/api/rpc",
            json={"method": "agent.create", "params": {"id": "coder", "name": "Coder"}},
        )
        client.post(
            "/api/rpc",
            json={
                "method": "session.create",
                "params": {
                    "agent_id": "coder",
                    "session_id": "kept-session",
                    "make_current": True,
                },
            },
        )
        prompts_dir = data_dir / "agents" / "coder" / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "runtime.md").write_text("custom", encoding="utf-8")
        private_skill = data_dir / "agents" / "coder" / "skills" / "private" / "SKILL.md"
        private_skill.parent.mkdir(parents=True)
        private_skill.write_text("# Private\n", encoding="utf-8")

        rename_response = client.post(
            "/api/rpc",
            json={
                "method": "agent.rename",
                "params": {"id": "coder", "new_id": "researcher"},
            },
        )
        history_response = client.post(
            "/api/rpc",
            json={
                "method": "chat.history",
                "params": {"agent_id": "researcher", "session_id": "kept-session"},
            },
        )
        old_agent_response = client.post(
            "/api/rpc",
            json={"method": "agent.get", "params": {"id": "coder"}},
        )

    assert rename_response.json()["result"]["id"] == "researcher"
    assert history_response.json()["result"]["session_id"] == "kept-session"
    assert old_agent_response.json()["error"]["code"] == "domain_error"
    assert (data_dir / "agents" / "researcher" / "prompts" / "runtime.md").is_file()
    assert (data_dir / "agents" / "researcher" / "skills" / "private" / "SKILL.md").is_file()
    assert not (data_dir / "agents" / "coder").exists()


def test_agent_update_rpc_accepts_workspace_mutation(tmp_path: Path) -> None:
    runtime = StubRuntime(Config(data_dir=tmp_path / "data"))
    app = create_app(runtime=runtime)
    workspace = tmp_path / "outside"

    with TestClient(app) as client:
        update_response = client.post(
            "/api/rpc",
            json={
                "method": "agent.update",
                "params": {"id": "main", "workspace": str(workspace)},
            },
        )
        create_response = client.post(
            "/api/rpc",
            json={
                "method": "agent.create",
                "params": {"id": "coder", "name": "Coder", "workspace": str(tmp_path / "outside")},
            },
        )
        updated_agent = client.post("/api/rpc", json={"method": "agent.list", "params": {}}).json()[
            "result"
        ]["agents"][0]

    assert update_response.json()["result"]["workspace"] == str(workspace.resolve())
    assert create_response.json()["error"]["code"] == "invalid_request"
    assert updated_agent["workspace"] == str(workspace.resolve())
    assert (workspace / "SOUL.md").exists()


def test_history_strips_opaque_provider_metadata(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / ".env").write_text("OPENAI_API_KEY=test-key\n", encoding="utf-8")
    runtime = StubRuntime(Config(data_dir=data_dir))
    app = create_app(runtime=runtime)

    with TestClient(app) as client:
        client.post(
            "/api/rpc",
            json={
                "method": "agent.update",
                "params": {
                    "id": "main",
                    "model": "openai/gpt-5.2::api-key",
                },
            },
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
        "run_summary",
    ]


def test_stream_cancel_path_remains_compatible(tmp_path: Path) -> None:
    adapter = StubAdapter(block=True)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / ".env").write_text("OPENAI_API_KEY=test-key\n", encoding="utf-8")
    runtime = StubRuntime(Config(data_dir=data_dir), adapter=adapter)
    app = create_app(runtime=cast(Any, runtime))

    with TestClient(app) as client:
        client.post(
            "/api/rpc",
            json={
                "method": "agent.update",
                "params": {
                    "id": "main",
                    "model": "openai/gpt-5.2::api-key",
                },
            },
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
