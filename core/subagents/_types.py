"""Sub-Agent tree read and control contracts."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from core.sessions import (
    SessionAddress,
)
from core.tools.tools import (
    ToolContext,
)

# ``working``: its turn runs; ``queued``: a message waits to start its next turn;
SubAgentStatus = Literal["working", "queued", "idle"]

# What a stop of a Sub-Agent did. ``stopped``: something ran and was stopped;
# ``idle`` means that nothing needed stopping.
SubAgentStopOutcome = Literal["stopped", "idle"]


@dataclass(frozen=True)
class SubAgentState:
    """One Sub-Agent below a caller, as it is now.

    ``agent_id`` is the address ``subagent`` accepts for its Agent. ``parent_id``
    names the caller's Sub-Agent it works for, ``None`` for a direct Sub-Agent of
    the caller. ``last_tool`` is the Tool its working turn called last.
    ``reply_target`` is independent of membership, status and control.
    """

    id: str
    title: str
    agent_id: str
    session: SessionAddress
    parent_id: str | None
    status: SubAgentStatus
    last_tool: str | None
    activity_file: str | None
    reply_target: str = "parent"


class SubAgentTree(Protocol):
    """The Sub-Agents below a caller, what they run, and how to stop one.

    ``background_tasks`` lists, checks and stops them and the background commands
    of their Sessions; ``terminal`` names the Sub-Agent whose Session holds a
    terminal. The complete tree stays visible and controllable.
    """

    async def subagents(self, caller: SessionAddress) -> Sequence[SubAgentState]:
        """*caller*'s Sub-Agents at every depth, breadth first, as they are now."""
        ...

    async def running_work(self, subagent: SubAgentState) -> Sequence[str]:
        """What *subagent* still runs besides its own turn, each as ``kind id (label)``."""
        ...

    async def stop_subagent(
        self, context: ToolContext, subagent: SubAgentState
    ) -> SubAgentStopOutcome:
        """Stop *subagent* with its queue, background work and own Sub-Agents.

        The complete subtree is stopped regardless of where its answers go.
        """
        ...
