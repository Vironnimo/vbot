"""The update_goal Tool through production dispatch."""

import json
from pathlib import Path
from typing import Any, cast
from unittest.mock import Mock

import pytest

from core.automation.goals import GoalService
from core.chat import ChatSessionManager
from core.permissions import PermissionBoundary
from core.providers.adapter import tool_result_text
from core.runs import ChatRunManager
from core.sessions import SESSION_GOAL_META_KEY, SessionAddress
from core.tools.tools import ToolContext, ToolRegistry
from core.tools.update_goal import register_update_goal_tool
from tests.core.tools.tools_test_support import dispatch_as_executor

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("current_format_data_directory")]

_ADDRESS = SessionAddress(project_id=None, agent_id="coder", session_id="session-one")


class _Tool:
    def __init__(self, tmp_path: Path, *, goal: dict[str, Any] | None) -> None:
        self.sessions = ChatSessionManager(tmp_path)
        self.sessions.create("coder", session_id="session-one")
        if goal is not None:
            self.sessions.mutate_metadata(
                _ADDRESS, lambda metadata: metadata.update({SESSION_GOAL_META_KEY: goal})
            )
        self.registry = ToolRegistry()
        goals = GoalService(
            sessions=self.sessions, run_manager=ChatRunManager(), chat_loop=cast(Any, Mock())
        )
        register_update_goal_tool(self.registry, goals)
        self.tmp_path = tmp_path

    async def call(self, arguments: Any) -> tuple[bool, str]:
        context = ToolContext(
            agent_id="coder",
            session_id="session-one",
            run_id="run-1",
            tool_call_id="call",
            tool_name="update_goal",
            tool_call_index=0,
            workspace=self.tmp_path,
            vbot_root=self.tmp_path,
            data_root=self.tmp_path,
            session_tool_grants=("update_goal",),
            permissions=PermissionBoundary.UNRESTRICTED,
        )
        result = await dispatch_as_executor(self.registry, context, arguments)
        return bool(result["ok"]), str(tool_result_text(json.dumps(result)))

    def status(self) -> str:
        goal = self.sessions.metadata_value(_ADDRESS, SESSION_GOAL_META_KEY)
        return cast(str, goal["status"])


def _goal(status: str = "active") -> dict[str, Any]:
    return {"objective": "Ship the release", "status": status, "turns": 3, "max_turns": 30}


@pytest.mark.parametrize(
    ("arguments", "status", "text"),
    [
        (
            {"status": "complete"},
            "complete",
            "Goal marked complete; no further automatic turns. Finish your reply to the user.",
        ),
        (
            {"status": "Done"},
            "complete",
            "Goal marked complete; no further automatic turns. Finish your reply to the user.",
        ),
        (
            {"status": "stuck"},
            "blocked",
            "Goal marked blocked; automatic turns continue after the user replies.",
        ),
        (
            {"status": "stop", "summary": "Tests pass"},
            "paused",
            "Goal paused. The user can resume it with /goal resume or delete it with "
            "/goal clear. summary has no effect; tell the user in your reply.",
        ),
    ],
)
async def test_update_goal_sets_the_status_the_agent_means(
    tmp_path: Path, arguments: dict[str, Any], status: str, text: str
) -> None:
    tool = _Tool(tmp_path, goal=_goal())

    assert await tool.call(arguments) == (True, text)
    assert tool.status() == status


async def test_the_same_status_changes_nothing(tmp_path: Path) -> None:
    tool = _Tool(tmp_path, goal=_goal("blocked"))

    assert await tool.call({"status": "blocked"}) == (
        True,
        "The goal is already blocked; nothing changed.",
    )


@pytest.mark.parametrize(
    ("goal", "arguments", "text"),
    [
        (
            _goal(),
            {"status": "finished-ish"},
            'Nothing was changed. Unknown status "finished-ish". '
            "Use one of: complete, blocked, paused.",
        ),
        (
            None,
            {"status": "complete"},
            "Nothing was changed. This session has no goal; only the user can set one with /goal.",
        ),
    ],
)
async def test_update_goal_refuses_with_what_to_do(
    tmp_path: Path, goal: dict[str, Any] | None, arguments: dict[str, Any], text: str
) -> None:
    tool = _Tool(tmp_path, goal=goal)

    ok, result = await tool.call(arguments)

    assert not ok
    assert text in result
    if goal is not None:
        assert tool.status() == "active"
