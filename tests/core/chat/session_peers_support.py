"""Real Sessions/Run manager behind production-dispatched peer Tool tests."""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest_asyncio

from core.activity import SessionActivity, SessionActivityState
from core.chat import ChatLoop
from core.chat.session_peers import SessionPeers
from core.runs import ChatRunManager
from core.sessions import ChatSession, ChatSessionManager, SessionAddress
from core.tools import ToolContext, ToolRegistry
from core.tools.session import register_session_tool
from tests.core.tools.tools_test_support import dispatch_as_executor, make_context


class PeerActivity:
    def __init__(self) -> None:
        self.states: dict[SessionAddress, SessionActivityState] = {}
        self.callbacks: list[Callable[..., None]] = []
        self.subscribed = asyncio.Event()

    def state(self, address: SessionAddress) -> SessionActivityState:
        return self.states.get(address, SessionActivityState("idle"))

    def snapshot(self) -> list[tuple[SessionAddress, SessionActivityState]]:
        return list(self.states.items())

    def add_changed_callback(self, callback: Callable[..., None]) -> Callable[[], None]:
        self.callbacks.append(callback)
        self.subscribed.set()
        return lambda: self.callbacks.remove(callback)


@dataclass
class PeerHarness:
    sessions: ChatSessionManager
    runs: ChatRunManager
    activity: PeerActivity
    peers: SessionPeers
    registry: ToolRegistry
    root: Path
    caller: ChatSession

    def create(
        self, agent: str, session: str, *, project: str | None = "shared", **kwargs: Any
    ) -> ChatSession:
        return self.sessions.create(agent, session_id=session, working_project_id=project, **kwargs)

    def reference(self, session: ChatSession) -> str:
        assert session.generation_id is not None
        return self.peers.reference(session.address, session.generation_id)

    def context(self, session: ChatSession | None = None, **fields: Any) -> ToolContext:
        session = session or self.caller
        return make_context(
            "session",
            agent_id=session.address.agent_id,
            session_id=session.id,
            project_id=session.address.project_id,
            working_project_id="shared",
            workspace=self.root,
            data_root=self.root,
            **fields,
        )

    async def call(
        self, arguments: dict[str, Any], *, context: ToolContext | None = None
    ) -> dict[str, Any]:
        return await dispatch_as_executor(self.registry, context or self.context(), arguments)


@pytest_asyncio.fixture
async def peer_harness(
    tmp_path: Path, current_session_store_template: Path
) -> AsyncIterator[PeerHarness]:
    for name in ("data-store.json", "sessions.db"):
        shutil.copy2(current_session_store_template / name, tmp_path)
    sessions = ChatSessionManager(tmp_path)
    runs = ChatRunManager(persistence=sessions)
    activity = PeerActivity()
    peers = SessionPeers(sessions, cast(SessionActivity, activity), runs)
    registry = ToolRegistry()
    register_session_tool(registry, peers, cast(ChatLoop, SimpleNamespace()))
    caller = sessions.create("coder", session_id="caller", working_project_id="shared")
    harness = PeerHarness(sessions, runs, activity, peers, registry, tmp_path, caller)
    try:
        yield harness
    finally:
        await runs.aclose()
        sessions.close()
