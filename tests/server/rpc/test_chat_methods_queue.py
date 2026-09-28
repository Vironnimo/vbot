"""Queue RPCs: ``chat.queue_list``, ``chat.queue_remove``, ``chat.queue_update`` and
``chat.queue_steer``.

Every mutation publishes a ``resource_changed`` Queue signal scoped to the Session
so other windows reload the Queue live. Internal items stay invisible: they are
treated exactly like missing ones.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio

from core.chat.content_blocks import TextBlock
from core.runs import ChatRunManager, QueuedRunItem, Run
from core.sessions import SessionAddress
from tests.server.rpc.chat_methods_test_support import (
    _RecordingLoop,
    call,
    chat_state,
    resource_changes,
)


def _item(item_id: str, *, internal: bool = False, editable: bool = True) -> QueuedRunItem:
    async def executor(_run: Run) -> None:
        return None

    return QueuedRunItem(
        item_id=item_id,
        display_content=f"{item_id} text",
        executor=executor,
        internal=internal,
        future=asyncio.get_running_loop().create_future(),
        editable=editable,
        created_at="2026-05-22T00:00:00+00:00",
    )


class _QueueRuns:
    """Run manager Queue double: holds items and records scoped mutations."""

    def __init__(self, *items: QueuedRunItem) -> None:
        self.items = list(items)
        self.list_calls: list[tuple[str, str, str | None]] = []
        self.remove_calls: list[tuple[str, str, str, str | None]] = []
        self.update_calls: list[tuple[Any, ...]] = []

    def list_queued(
        self, agent_id: str, session_id: str, *, project_id: str | None
    ) -> list[QueuedRunItem]:
        self.list_calls.append((agent_id, session_id, project_id))
        return list(self.items)

    def remove_queued(
        self, agent_id: str, session_id: str, item_id: str, *, project_id: str | None
    ) -> bool:
        self.remove_calls.append((agent_id, session_id, item_id, project_id))
        return True

    def update_queued(
        self,
        agent_id: str,
        session_id: str,
        item_id: str,
        new_executor: Any,
        new_display_content: str,
        *,
        project_id: str | None,
        editable: bool | None = None,
    ) -> bool:
        self.update_calls.append(
            (agent_id, session_id, item_id, new_display_content, project_id, editable)
        )
        return True


def _queue_state(runs: Any, loop: _RecordingLoop | None = None) -> SimpleNamespace:
    state = chat_state(loop)
    state.chat_runs = runs
    return state


def _queue_signal(session_id: str) -> list[dict[str, Any]]:
    return [{"kind": "queue", "scope": {"agent_id": "builder", "session_id": session_id}}]


@pytest.mark.asyncio
async def test_queue_list_returns_only_public_items() -> None:
    public, internal = _item("queue-public"), _item("queue-internal", internal=True)
    runs = _QueueRuns(public, internal)

    response = await call(
        _queue_state(runs), "chat.queue_list", agent_id="builder@vbot", session_id="s1"
    )

    assert response == {"ok": True, "result": {"items": [public.to_dict()]}}
    assert runs.list_calls == [("builder", "s1", "vbot")]


@pytest.mark.asyncio
async def test_queue_remove_deletes_a_public_item_and_signals_the_queue() -> None:
    runs = _QueueRuns(_item("queue-1"))
    state = _queue_state(runs)

    response = await call(
        state, "chat.queue_remove", agent_id="builder", session_id="s1", item_id="queue-1"
    )

    assert response == {"ok": True, "result": {"ok": True}}
    assert runs.remove_calls == [("builder", "s1", "queue-1", None)]
    assert resource_changes(state, "queue") == _queue_signal("s1")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "item_id"),
    [
        ("chat.queue_remove", "queue-404"),
        ("chat.queue_remove", "queue-internal"),
        ("chat.queue_update", "queue-internal"),
    ],
)
async def test_queue_mutations_treat_unknown_and_internal_items_as_missing(
    method: str, item_id: str
) -> None:
    runs = _QueueRuns(_item("queue-1"), _item("queue-internal", internal=True))
    loop = _RecordingLoop()
    state = _queue_state(runs, loop)

    params: dict[str, Any] = {"agent_id": "builder", "session_id": "s1", "item_id": item_id}
    if method == "chat.queue_update":
        params["content"] = "Edit"

    response = await call(state, method, **params)

    assert response["ok"] is False
    assert response["error"]["code"] == "queue_item_not_found"
    assert runs.remove_calls == []
    assert runs.update_calls == []
    assert loop.build_calls == []
    assert resource_changes(state, "queue") == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("agent_address", "project_id", "content", "expected_content", "editable"),
    [
        pytest.param(
            "builder",
            None,
            [{"type": "text", "text": "Edited text"}],
            [TextBlock(type="text", text="Edited text")],
            False,
            id="identity-blocks",
        ),
        pytest.param("builder@vbot", "vbot", "Edited text", "Edited text", True, id="project"),
    ],
)
async def test_queue_update_rebuilds_the_item_under_its_address_and_resolved_session(
    agent_address: str,
    project_id: str | None,
    content: Any,
    expected_content: Any,
    editable: bool,
) -> None:
    # The queue key carries the project anchor, so the rebuild runs against the
    # addressed project; the signal names the Session the rebuild resolved.
    item = _item("queue-1")
    runs = _QueueRuns(item)
    loop = _RecordingLoop(resolved_session_id="resolved-s1")
    state = _queue_state(runs, loop)

    response = await call(
        state,
        "chat.queue_update",
        agent_id=agent_address,
        session_id="s1",
        item_id="queue-1",
        content=content,
    )

    assert response == {"ok": True, "result": {"ok": True}}
    assert loop.build_calls == [
        {
            "agent_id": "builder",
            "session_id": "s1",
            "content": expected_content,
            "queued_item": item,
            "input_origin": None,
            "project_id": project_id,
        }
    ]
    assert runs.list_calls == [("builder", "s1", project_id)]
    assert runs.update_calls == [
        ("builder", "resolved-s1", "queue-1", "Edited preview", project_id, editable)
    ]
    assert resource_changes(state, "queue") == _queue_signal("resolved-s1")


@pytest.mark.asyncio
async def test_queue_update_refuses_content_that_cannot_be_edited_losslessly() -> None:
    runs = _QueueRuns(_item("queue-1", editable=False))
    state = _queue_state(runs)

    response = await call(
        state,
        "chat.queue_update",
        agent_id="builder",
        session_id="s1",
        item_id="queue-1",
        content="Edit",
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"
    assert runs.update_calls == []


@dataclass
class _Steering:
    manager: ChatRunManager
    run: Run
    item: QueuedRunItem
    state: SimpleNamespace


@pytest_asyncio.fixture
async def steering() -> AsyncIterator[_Steering]:
    """A real active Run and one queued item for the project Session."""
    manager = ChatRunManager()
    release = asyncio.Event()
    address = SessionAddress(project_id="project", agent_id="builder", session_id="s1")

    async def execute(_run: Run) -> str:
        await release.wait()
        return "done"

    run = await manager.start(address, execute)
    await asyncio.sleep(0)
    item = await manager.enqueue(
        address, execute, steerable=True, editable=True, display_content="Correction"
    )
    try:
        yield _Steering(manager, run, item, _queue_state(manager))
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_queue_steer_selects_a_public_item(steering: _Steering) -> None:
    params = {"agent_id": "builder@project", "session_id": "s1", "item_id": steering.item.item_id}

    wrong_scope = await call(
        steering.state, "chat.queue_steer", **{**params, "agent_id": "builder"}
    )
    response = await call(steering.state, "chat.queue_steer", **params)

    assert wrong_scope["ok"] is False
    assert response["result"]["item"]["steering"] is True
    assert response["result"]["item"]["editable"] is False
    assert resource_changes(steering.state, "queue") == _queue_signal("s1")
    steering.item.internal = True
    assert (await call(steering.state, "chat.queue_steer", **params))["ok"] is False


@pytest.mark.asyncio
async def test_a_steered_item_can_neither_be_edited_nor_removed(steering: _Steering) -> None:
    address = {"agent_id": "builder@project", "session_id": "s1", "item_id": steering.item.item_id}
    steering.manager.steer_queued("builder", "s1", steering.item.item_id, project_id="project")

    update = await call(steering.state, "chat.queue_update", **address, content="x")
    removals: list[dict[str, Any]] = []

    async def remove_during_delivery(_item: Any) -> None:
        removals.append(await call(steering.state, "chat.queue_remove", **address))

    delivered = await steering.manager.deliver_steering(steering.run, remove_during_delivery)

    assert update["error"]["code"] == "queue_item_steering"
    assert delivered
    assert [removal["error"]["code"] for removal in removals] == ["queue_item_steering"]
    assert steering.item.future.result() is steering.run
