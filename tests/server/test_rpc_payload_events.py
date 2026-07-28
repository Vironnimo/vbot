"""Server RPC payload and event translation."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.chat import (
    ChatMessage,
)
from core.runs import ChatRunManager
from server.rpc import (
    event_bridge,
    payloads,
)
from server.rpc.methods import dispatch_rpc
from tests.core.chat.chat_loop_support import build_chat_loop
from tests.server.rpc_test_support import (
    JsonObject,
    StubAdapter,
    StubRuntime,
    _no_models_dev_fetch,
    make_state,
)

__all__ = ["_no_models_dev_fetch"]


@pytest.mark.asyncio
async def test_dispatch_validates_unknown_method_and_required_params(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    unknown = await dispatch_rpc(state, {"method": "unknown", "params": {}})
    missing = await dispatch_rpc(state, {"method": "session.create", "params": {}})

    assert unknown["error"]["code"] == "method_not_found"
    assert missing["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_agent_create_publishes_agents_resource_changed(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(
        state,
        {"method": "agent.create", "params": {"id": "writer", "name": "Writer"}},
    )

    assert response["ok"] is True
    assert len(state.event_bus.events) == 1
    event = state.event_bus.events[0]
    assert event["type"] == "resource_changed"
    assert event["payload"] == {"kind": "agents"}
    assert event["sequence"] == 1


@pytest.mark.asyncio
async def test_agent_update_publishes_agents_resource_changed(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(
        state,
        {"method": "agent.update", "params": {"id": "coder", "name": "Updated Coder"}},
    )

    assert response["ok"] is True
    assert len(state.event_bus.events) == 1
    event = state.event_bus.events[0]
    assert event["type"] == "resource_changed"
    assert event["payload"] == {"kind": "agents"}


@pytest.mark.asyncio
async def test_agent_delete_publishes_agents_resource_changed(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.agents.create("writer", "Writer")

    response = await dispatch_rpc(
        state,
        {"method": "agent.delete", "params": {"id": "writer"}},
    )

    assert response["ok"] is True
    # The remaining-agents detail lives on the RPC response; the event is a bare
    # "agents changed → reload" signal carrying no agent data.
    assert response["result"]["remaining_agents"][0]["id"] == "coder"
    assert len(state.event_bus.events) == 1
    event = state.event_bus.events[0]
    assert event["type"] == "resource_changed"
    assert event["payload"] == {"kind": "agents"}


@pytest.mark.asyncio
async def test_agent_crud_events_not_published_without_event_bus(tmp_path: Path) -> None:
    runtime: Any = StubRuntime(tmp_path, StubAdapter())
    chat_runs = ChatRunManager()
    runtime.chat_runs = chat_runs
    state = SimpleNamespace(
        runtime=runtime,
        chat_runs=chat_runs,
        chat_loop=build_chat_loop(runtime),
        agent_delete_lock=asyncio.Lock(),
    )
    # No event_bus attribute — should not crash

    response = await dispatch_rpc(
        state,
        {"method": "agent.create", "params": {"id": "writer", "name": "Writer"}},
    )

    assert response["ok"] is True


class TestRemoveOpaqueProviderMetadata:
    """Tests for remove_opaque_provider_metadata preserving canonical fields."""

    def test_strips_reasoning_meta(self) -> None:
        result = payloads.remove_opaque_provider_metadata(
            {"role": "assistant", "reasoning_meta": {"secret": "opaque"}}
        )
        assert result == {"role": "assistant"}

    def test_strips_reasoning_scope(self) -> None:
        result = payloads.remove_opaque_provider_metadata(
            {
                "role": "assistant",
                "reasoning_scope": "openai/gpt-5.6-sol::api-key:work",
            }
        )
        assert result == {"role": "assistant"}

    def test_preserves_usage(self) -> None:
        result = payloads.remove_opaque_provider_metadata(
            {
                "role": "assistant",
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cache_write_tokens": 10,
                    "reasoning_tokens": 30,
                },
            }
        )
        assert result == {
            "role": "assistant",
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_write_tokens": 10,
                "reasoning_tokens": 30,
            },
        }

    def test_preserves_usage_and_strips_reasoning_meta(self) -> None:
        result = payloads.remove_opaque_provider_metadata(
            {
                "role": "assistant",
                "content": "Hello",
                "reasoning": "thinking",
                "reasoning_meta": {"secret": "opaque"},
                "usage": {"input_tokens": 100, "output_tokens": 50},
            }
        )
        assert result == {
            "role": "assistant",
            "content": "Hello",
            "reasoning": "thinking",
            "usage": {"input_tokens": 100, "output_tokens": 50},
        }

    def test_strips_nested_reasoning_meta(self) -> None:
        result = payloads.remove_opaque_provider_metadata(
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "name": "read",
                        "arguments": {"path": "file.txt"},
                        "reasoning_meta": {"secret": "nested"},
                    }
                ],
            }
        )
        assert result == {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call_1",
                    "name": "read",
                    "arguments": {"path": "file.txt"},
                }
            ],
        }

    def test_preserves_usage_nested_in_dict(self) -> None:
        result = payloads.remove_opaque_provider_metadata(
            {
                "role": "assistant",
                "content": "file contents",
                "usage": {"input_tokens": 10},
            }
        )
        assert result == {
            "role": "assistant",
            "content": "file contents",
            "usage": {"input_tokens": 10},
        }


class TestVisibleMessage:
    """Tests for _visible_message preserving usage and stripping reasoning_meta."""

    def test_visible_message_includes_usage_on_assistant(self) -> None:
        message = ChatMessage.assistant(
            model="openai/gpt-5.2",
            content="Hello",
            usage={"input_tokens": 200, "output_tokens": 30},
        )
        result = payloads._visible_message(message)
        assert result["usage"] == {"input_tokens": 200, "output_tokens": 30}

    def test_visible_message_strips_reasoning_meta(self) -> None:
        message = ChatMessage.assistant(
            model="openai/gpt-5.2",
            content="Hello",
            reasoning_meta={"secret": "opaque"},
            reasoning_scope="openai/gpt-5.2::api-key:work",
        )
        result = payloads._visible_message(message)
        assert "reasoning_meta" not in result
        assert "reasoning_scope" not in result

    def test_visible_message_preserves_usage_and_strips_reasoning_meta(self) -> None:
        message = ChatMessage.assistant(
            model="openai/gpt-5.2",
            content="Hello",
            reasoning="visible thinking",
            reasoning_meta={"secret": "opaque"},
            usage={"input_tokens": 100, "output_tokens": 50},
        )
        result = payloads._visible_message(message)
        assert result["usage"] == {"input_tokens": 100, "output_tokens": 50}
        assert result["reasoning"] == "visible thinking"
        assert "reasoning_meta" not in result

    def test_visible_message_excludes_usage_when_none(self) -> None:
        message = ChatMessage.assistant(
            model="openai/gpt-5.2",
            content="Hello",
        )
        result = payloads._visible_message(message)
        assert "usage" not in result

    def test_visible_message_preserves_tool_timing(self) -> None:
        timing = {
            "started_at": "2026-05-03T14:30:01+00:00",
            "completed_at": "2026-05-03T14:30:02+00:00",
            "duration_ms": 1000,
        }
        message = ChatMessage.tool(
            tool_call_id="call-one",
            name="read",
            content='{"ok":true,"error":null,"data":{},"artifacts":[]}',
            timing=timing,
        )
        result = payloads._visible_message(message)
        assert result["timing"] == timing


class TestServerEventFromRunEvent:
    """Tests for _server_event_from_run_event preserving usage in run_completed."""

    def _make_event(
        self,
        event_type: str,
        payload: JsonObject | None = None,
        sequence: int = 1,
    ) -> Any:
        """Create a minimal RunEvent for testing."""
        from core.runs import RunEvent

        return RunEvent(
            sequence=sequence,
            run_id="run-test",
            agent_id="agent-test",
            session_id="session-test",
            type=event_type,
            payload=payload or {},
        )

    def test_run_completed_includes_usage_when_present(self) -> None:
        event = self._make_event(
            event_bridge.RUN_COMPLETED_EVENT,
            {"status": "completed", "usage": {"input_tokens": 100, "output_tokens": 50}},
        )
        result = event_bridge._server_event_from_run_event(event)

        assert result["payload"]["usage"] == {"input_tokens": 100, "output_tokens": 50}

    def test_run_completed_omits_usage_when_absent(self) -> None:
        event = self._make_event(
            event_bridge.RUN_COMPLETED_EVENT,
            {"status": "completed"},
        )
        result = event_bridge._server_event_from_run_event(event)

        assert "usage" not in result["payload"]

    def test_run_completed_strips_reasoning_meta_from_usage(self) -> None:
        event = self._make_event(
            event_bridge.RUN_COMPLETED_EVENT,
            {
                "status": "completed",
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "reasoning_meta": {"secret": "opaque"},
                },
            },
        )
        result = event_bridge._server_event_from_run_event(event)

        assert result["payload"]["usage"] == {"input_tokens": 100, "output_tokens": 50}
        assert "reasoning_meta" not in result["payload"]["usage"]

    def test_run_completed_preserves_usage_with_estimated_flag(self) -> None:
        event = self._make_event(
            event_bridge.RUN_COMPLETED_EVENT,
            {
                "status": "completed",
                "usage": {"input_tokens": 100, "output_tokens": 50, "estimated": True},
            },
        )
        result = event_bridge._server_event_from_run_event(event)

        assert result["payload"]["usage"] == {
            "input_tokens": 100,
            "output_tokens": 50,
            "estimated": True,
        }

    def test_run_completed_includes_status_alongside_usage(self) -> None:
        event = self._make_event(
            event_bridge.RUN_COMPLETED_EVENT,
            {"status": "completed", "usage": {"input_tokens": 200, "output_tokens": 30}},
        )
        result = event_bridge._server_event_from_run_event(event)

        assert result["payload"]["status"] == "completed"
        assert result["payload"]["usage"] == {"input_tokens": 200, "output_tokens": 30}

    def test_terminal_event_includes_timing(self) -> None:
        timing = {
            "started_at": "2026-05-03T14:30:01+00:00",
            "completed_at": "2026-05-03T14:30:02+00:00",
            "duration_ms": 1000,
        }
        event = self._make_event(
            event_bridge.RUN_FAILED_EVENT,
            {"status": "failed", "timing": timing},
        )
        result = event_bridge._server_event_from_run_event(event)

        assert result["payload"]["status"] == "failed"
        assert result["payload"]["timing"] == timing

    def test_non_completed_terminal_event_excludes_usage(self) -> None:
        """run_failed and run_cancelled should not carry usage even if payload has it."""
        event = self._make_event(
            event_bridge.RUN_FAILED_EVENT,
            {"status": "failed", "usage": {"input_tokens": 10}},
        )
        result = event_bridge._server_event_from_run_event(event)

        assert "usage" not in result["payload"]
        assert result["payload"]["status"] == "failed"

    def test_run_started_includes_output_with_queue_item_id(self) -> None:
        """run_started WS summary surfaces queue_item_id so the client can drop the queued item."""
        from core.runs import RUN_STARTED_EVENT

        event = self._make_event(
            RUN_STARTED_EVENT,
            {"status": "running", "queue_item_id": "qi-abc-123"},
        )
        result = event_bridge._server_event_from_run_event(event)

        assert result["type"] == event_bridge.SERVER_EVENT_TYPES[RUN_STARTED_EVENT]
        assert result["payload"]["output"] == {
            "status": "running",
            "queue_item_id": "qi-abc-123",
        }

    def test_run_started_includes_output_without_queue_item_id(self) -> None:
        """run_started remains backward compatible when no queue_item_id is present."""
        from core.runs import RUN_STARTED_EVENT

        event = self._make_event(
            RUN_STARTED_EVENT,
            {"status": "running"},
        )
        result = event_bridge._server_event_from_run_event(event)

        assert result["type"] == event_bridge.SERVER_EVENT_TYPES[RUN_STARTED_EVENT]
        assert result["payload"]["output"] == {"status": "running"}


# ---------------------------------------------------------------------------
# prompt.* RPC handlers
# ---------------------------------------------------------------------------
