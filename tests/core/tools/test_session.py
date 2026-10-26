"""Session coordination at production dispatch: intent, delivery and authority."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from core.chat import ChatLoop
from core.chat.errors import ChatError
from core.chat.session_peers import SessionPeers
from core.permissions import BoundaryContext, PermissionBoundary, PermissionRules
from core.sessions import SessionAddress
from core.tools.session import register_session_tool
from core.tools.tools import ToolContext, ToolRegistry
from tests.core.tools.tools_test_support import dispatch_as_executor, make_context

pytestmark = pytest.mark.asyncio


class Peers:
    def __init__(self) -> None:
        self.source = SimpleNamespace(
            id="source-reference",
            address=SessionAddress(None, "coder", "source"),
            generation_id="source-generation",
            working_project_id="project",
            title="Source task",
        )
        self.target = SimpleNamespace(
            id="target-reference",
            address=SessionAddress("project", "reviewer", "target"),
            generation_id="target-generation",
            working_project_id="project",
            title="Target task",
        )

    async def own(self, context: ToolContext) -> Any:
        return self.source

    async def resolve(self, context: ToolContext, id: str) -> Any:
        assert id == self.target.id
        return self.target

    async def read(self, context: ToolContext, id: str, *, limit: int) -> dict[str, Any]:
        assert id == self.target.id
        return {"id": id, "cursor": "before-send"}


class Sender:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.error: Exception | None = None

    async def submit_session_message(self, **arguments: Any) -> Any:
        self.calls.append(arguments)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(run=None, queued_item=SimpleNamespace(steering=True))


@pytest.fixture
def harness() -> tuple[ToolRegistry, ToolContext, Peers, Sender]:
    registry, peers, sender = ToolRegistry(), Peers(), Sender()
    register_session_tool(registry, cast(SessionPeers, peers), cast(ChatLoop, sender))
    context = make_context("session", working_project_id="project", agent_id="coder")
    return registry, context, peers, sender


@pytest.mark.parametrize(
    "arguments",
    [
        {"action": "send", "id": "target-reference", "content": "  wait\nfor review  "},
        {
            "parameters": {
                "Action": "SEND",
                "thread_id": "target-reference",
                "message": "  wait\nfor review  ",
                "timeout": "0",
            }
        },
    ],
)
async def test_send_preserves_intent_and_returns_a_pre_send_baseline(
    harness: tuple[ToolRegistry, ToolContext, Peers, Sender], arguments: dict[str, Any]
) -> None:
    registry, context, peers, sender = harness
    result = await dispatch_as_executor(registry, context, arguments)
    assert result["ok"], result
    assert result["data"]["status"] == "accepted"
    assert result["data"]["delivery"] == "steered"
    assert result["data"]["cursor"] == "before-send"
    [call] = sender.calls
    assert call["content"] == "  wait\nfor review  "
    assert call["source"] == peers.source.address
    assert call["target"] == peers.target.address
    assert call["expected_source_generation_id"] == peers.source.generation_id
    assert call["expected_generation_id"] == peers.target.generation_id
    assert call["expected_working_project_id"] == "project"
    # Replaying one admitted Tool invocation has the same receipt identity.
    await dispatch_as_executor(registry, context, arguments)
    assert sender.calls[1]["input_id"] == call["input_id"]
    assert sender.calls[1]["request_hash"] == call["request_hash"]
    # Distinct invocations remain distinct even when the Provider reuses a call id.
    for other in (
        replace(context, tool_call_id="another-call"),
        replace(context, iteration_number=context.iteration_number + 1),
        replace(context, tool_call_index=context.tool_call_index + 1),
        replace(context, run_id="another-run"),
    ):
        sent = await dispatch_as_executor(registry, other, arguments)
        assert sent["ok"], sent
        assert sender.calls[-1]["input_id"] not in {
            previous["input_id"] for previous in sender.calls[:-1]
        }
        assert sender.calls[-1]["request_hash"] == call["request_hash"]


@pytest.mark.parametrize(
    "arguments",
    [
        {"action": "send", "content": "hello"},
        {"action": "send", "id": "target-reference"},
        {"action": "send", "id": "target-reference", "content": "hello", "dry_run": True},
        {
            "action": "send",
            "id": "target-reference",
            "thread_id": "different-reference",
            "content": "hello",
        },
        {"action": "delete", "id": "target-reference"},
    ],
)
async def test_ambiguous_or_unsupported_calls_have_no_delivery(
    harness: tuple[ToolRegistry, ToolContext, Peers, Sender], arguments: dict[str, Any]
) -> None:
    registry, context, _, sender = harness
    result = await dispatch_as_executor(registry, context, arguments)
    assert result["error"]["code"] == "invalid_arguments"
    assert sender.calls == []


async def test_send_cannot_route_work_around_execution_limits(
    harness: tuple[ToolRegistry, ToolContext, Peers, Sender], tmp_path: Path
) -> None:
    registry, context, _, sender = harness
    restricted = PermissionBoundary.build(
        PermissionRules(read="work_area", write="work_area"),
        BoundaryContext(workdir=tmp_path, home=tmp_path, data_dir=tmp_path / "data"),
    )
    for bounded in (
        replace(context, permissions=restricted),
        replace(context, tool_restriction=("session",)),
        replace(context, tool_denial_resolver=lambda _: None),
    ):
        result = await dispatch_as_executor(
            registry, bounded, {"action": "send", "id": "target-reference", "content": "work"}
        )
        assert result["error"]["code"] == "permission_denied"
    assert sender.calls == []


async def test_uncertain_delivery_is_not_reported_as_success_or_retried(
    harness: tuple[ToolRegistry, ToolContext, Peers, Sender],
) -> None:
    registry, context, _, sender = harness
    sender.error = ChatError("The target Session changed during admission.")
    result = await dispatch_as_executor(
        registry, context, {"action": "send", "id": "target-reference", "content": "work"}
    )
    assert result["error"]["code"] == "session_send_failed"
    assert len(sender.calls) == 1
