"""Shared Swarm test helpers: Store databases, Tool calls, delivered text and Run pacing."""

from __future__ import annotations

# mypy: disable-error-code=arg-type
import asyncio
import json
import re
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NamedTuple, cast

from core.chat import ChatMessage
from core.chat.messages import ToolCall
from core.database import open_offline_database
from core.extensions.databases import Database, extension_database_spec
from core.providers._tool_result_text import tool_result_text
from core.runs import RunExecutionOwner
from core.tools import ToolContext, ToolContractError, tool_failure
from core.utils.ids import new_id
from resources.extensions.swarm.store import SCHEMA_SQL, SwarmStore
from tests.core.chat.chat_loop_support import StubAdapter

# Store ------------------------------------------------------------------------------------------


def open_swarm_database(directory: Path, name: str = "swarm") -> Database:
    """Open a Swarm-schema kernel database outside any data store, for store tests."""
    return open_offline_database(extension_database_spec(directory, "swarm", name, SCHEMA_SQL))


def _profile(*, slug: str = "research", count: int = 2) -> dict[str, object]:
    return {
        "schema_version": 1,
        "slug": slug,
        "name": "Research",
        "participants": [{"model": "model-a", "count": count}],
        "working_directory": {"kind": "directory", "path": "C:/work"},
        "tool_access": {"mode": "selected", "allowed": []},
        "delivery": {},
    }


def query(database: Database, sql: str, parameters: tuple[object, ...] = ()) -> list:
    """Rows of one read-only query on a Store's kernel database, for persisted-state checks."""
    with database.read() as connection:
        return connection.execute(sql, parameters).fetchall()


async def _swarm(store: SwarmStore, *, count: int = 2) -> dict[str, object]:
    profile = await store.save_profile(_profile(count=count), expected_revision=None)
    return await store.create_swarm(
        profile["id"],
        "Investigate",
        {"cwd": "C:/work"},
        request_id="start-1",
        expected_profile_revision=profile["revision"],
    )


# Participant Tools on the ``board`` fixture ----------------------------------------------------


async def persist_carriers(sessions, address, *, messages, **kwargs):
    """Save delivered messages with their receipts, as a finished Tool batch or note would."""
    calls = [
        ToolCall(id=message.tool_call_id, name=message.name)
        for message in messages
        if message.role == "tool"
    ]
    if calls:
        assistant = ChatMessage.assistant(model="fixture", content=None, tool_calls=calls)
        await sessions.get(address).append_async(assistant)
        kwargs["assistant_message_id"] = assistant.id
    await sessions.append_messages_with_receipts_async(address, messages=messages, **kwargs)


async def call(board, arguments, peer=0, *, tool_call_id=None):
    context = replace(board.contexts[peer], tool_call_id=tool_call_id or new_id("call"))
    result = await board.tools.get("swarm_board").handler(context, arguments)
    return result, context


async def dispatch(board, arguments, peer=0, *, name="swarm_board", tool_call_id=None):
    """Run a call through production dispatch, as the Model's Tool Call would."""

    context = replace(
        board.contexts[peer],
        tool_name=name,
        tool_call_id=tool_call_id or new_id("call"),
        session_tool_grants=(name,),
    )
    try:
        result = await board.tools.dispatch(context, arguments, allowed_tools=[name])
    except ToolContractError as error:
        result = tool_failure("invalid_arguments", str(error))
    return result, context


def visible(result):
    """Return the Tool Result text the Model reads."""

    return tool_result_text(json.dumps(result))


def continuation(line):
    """Return the argument object of a result line that contains a copyable call."""

    return json.JSONDecoder().raw_decode(line[line.index("{") :])[0]


class Received(NamedTuple):
    discussion: str | None
    post_id: str
    author: str
    details: str | None
    text: str


_MESSAGE = re.compile(
    r"\[(?P<id>#\d+)\] (?P<author>[^\n(]*?)(?: \((?P<details>[^\n]*)\))?:\n"
    r"(?P<text>.*)",
    re.DOTALL,
)


def received(content):
    """Parse the messages an Agent reads from delivered text with single-paragraph posts."""

    messages, discussion = [], None
    for block in content.split("\n\n"):
        if block.startswith("In ") and block.endswith(":"):
            discussion = block[3:-1]
        elif match := _MESSAGE.fullmatch(block):
            messages.append(
                Received(discussion, match["id"], match["author"], match["details"], match["text"])
            )
    return messages


async def post_ref(board, post_id):
    """Return the reference participants read for a stored post ID, such as "#3"."""

    post = (await board.store.read_human_posts(board.swarm["id"], message_id=post_id)).entries[0]
    return f"#{post['sequence']}"


async def board_posts(board, peer=0, **query):
    return list(
        (
            await board.store.read_posts(
                board.swarm["id"], board.bindings[peer].participant_id, **query
            )
        ).entries
    )


def _name(board, peer):
    return board.swarm["participants"][peer]["display_name"]


def delivery_request(board, peer, *, run_id=None):
    """The request a participant's Session hands to the registered delivery hooks."""

    context = board.contexts[peer]
    return SimpleNamespace(
        binding=board.bindings[peer],
        execution_owner=context.execution_owner,
        run_id=run_id or context.run_id,
    )


# Wiki ------------------------------------------------------------------------------------------


async def invoke(fixture, arguments, peer=0, *, tool_call_id=None):
    """Run one swarm_wiki Tool Call through production dispatch."""

    context = replace(
        fixture.contexts[peer],
        tool_name="swarm_wiki",
        tool_call_id=tool_call_id or new_id("call"),
        session_tool_grants=("swarm_wiki",),
    )
    try:
        return await fixture.tools.dispatch(context, arguments, allowed_tools=["swarm_wiki"])
    except ToolContractError as error:
        return tool_failure("invalid_arguments", str(error))


async def stored(fixture, page_id, **query):
    """Return a page as the Store holds it."""

    return await fixture.store.wiki(
        fixture.swarm["id"], None, {"action": "read", "page_id": page_id, "limit": 20000, **query}
    )


async def pages(fixture):
    return await fixture.store.wiki_pages(fixture.swarm["id"])


async def create(fixture, content, title="Notes"):
    result = await invoke(fixture, {"action": "create", "title": title, "content": content})
    assert result["ok"], result
    return result["data"]["page_id"]


async def peer_rename(fixture, page_id, title="Peer title"):
    """Let another participant make revision 1 stale."""

    renamed = await invoke(
        fixture,
        {"action": "update", "page_id": page_id, "expected_revision": 1, "title": title},
        1,
    )
    assert renamed["ok"], renamed


# Runs on the ``lifecycle`` fixture -------------------------------------------------------------

# Generous Swarm coordination deadline: under full-gate xdist load, wakes, board
# writes and state transitions perform durable SQLite work that can take several
# seconds. These waits guard a stuck coordination step, not a latency SLA. The
# deadline stays below the gate's 30 s per-test timeout, so a real hang still
# fails as a readable TimeoutError instead of a killed worker.
SWARM_COORDINATION_TIMEOUT_SECONDS = 20.0


async def wait_idle(service: Any, swarm_id: str) -> dict[str, Any]:
    async with asyncio.timeout(SWARM_COORDINATION_TIMEOUT_SECONDS):
        while True:
            snapshot = await service.store.get_swarm(swarm_id)
            if snapshot["state"] == "idle":
                return cast(dict[str, Any], snapshot)
            await asyncio.sleep(0.01)


async def settled() -> None:
    """Wait until every other task on the loop finished: wake scans, Runs and their cleanup.

    A post that must not wake anyone is checked after this, instead of after a guessed delay.
    """

    current = asyncio.current_task()
    async with asyncio.timeout(SWARM_COORDINATION_TIMEOUT_SECONDS):
        while any(task is not current and not task.done() for task in asyncio.all_tasks()):
            await asyncio.sleep(0.001)


class PausedSwarmAdapter(StubAdapter):
    def __init__(self, *, pause_at: int = 2):
        super().__init__(
            [
                {"content": "initial"},
                {"tool_calls": [{"id": "review-state", "name": "swarm_state", "arguments": {}}]},
                {"content": "wake finished"},
                {"content": "later wake finished"},
            ]
        )
        self.pause_at = pause_at
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.followed = asyncio.Event()

    async def send(self, messages, *, model_id, **kwargs):
        response = await super().send(messages, model_id=model_id, **kwargs)
        ordinal = len(self.requests)
        if ordinal == self.pause_at:
            self.started.set()
            await self.release.wait()
        if ordinal == 3:
            self.followed.set()
        return response


async def single_participant_profile(env: Any, tmp_path: Any, *, mode: str = "all"):
    return await env.service.store.save_profile(
        {
            "schema_version": 1,
            "name": "Review",
            "participants": [{"model": "fixture/model", "count": 1}],
            "working_directory": {"kind": "directory", "path": str(tmp_path)},
            "tool_access": {"mode": "selected", "allowed": []},
            "delivery": {
                "main": {"mode": mode, "wake_idle": True},
                "discussion": {"mode": "all", "wake_idle": True},
                "ping": {"mode": "all", "wake_idle": True},
                "coalesce_ms": 10,
                "batch_messages": 20,
                "batch_chars": 24000,
            },
        },
        expected_revision=None,
    )


async def participant_post(env: Any, swarm_id: str, participant_id: str, text: str) -> None:
    """Post as a participant through its swarm_board Tool, which also scans for wakes."""

    swarm = await env.service.store.get_swarm(swarm_id)
    binding = next(
        item for item in await env.groups.list(swarm_id) if item.participant_id == participant_id
    )
    context = ToolContext(
        agent_id=binding.address.agent_id,
        session_id=binding.address.session_id,
        project_id=binding.address.project_id,
        run_id="participant-post",
        tool_call_id=new_id("call"),
        tool_name="swarm_board",
        tool_call_index=0,
        workspace=env.workspace,
        vbot_root=env.workspace,
        data_root=env.workspace,
        execution_owner=RunExecutionOwner(
            extension="swarm",
            group_id=swarm_id,
            participant_id=participant_id,
            epoch=swarm["execution_epoch"],
            generation_id=binding.generation_id,
        ),
        delivery_receipt_hook=lambda *_: None,
    )
    result = await env.tools.get("swarm_board").handler(context, {"action": "post", "text": text})
    assert result["ok"], result


class QuietTimers:
    """Stands in for the event loop's ``call_later`` so a test ends quiet periods itself."""

    def __init__(self) -> None:
        self.timers: list[QuietTimer] = []

    def call_later(self, delay: float, callback: Callable[..., None], *args: Any) -> QuietTimer:
        self.timers.append(QuietTimer(delay, callback, args))
        return self.timers[-1]


class QuietTimer:
    def __init__(self, delay: float, callback: Callable[..., None], args: tuple[Any, ...]):
        self.delay, self.callback, self.args = delay, callback, args
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True

    def fire(self) -> None:
        self.callback(*self.args)
