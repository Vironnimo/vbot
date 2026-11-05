"""The update_goal Tool through production dispatch."""

import json
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from unittest.mock import Mock

import pytest

from core.automation.goals import GoalService
from core.chat import ChatSessionManager
from core.permissions import PermissionBoundary
from core.providers.adapter import tool_result_text
from core.runs import ChatRunManager, Run, RunAdmission, RunKind
from core.sessions import SESSION_GOAL_META_KEY, SessionAddress
from core.tools.create_goal import register_create_goal_tool
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
        self.runs = ChatRunManager()
        self.goals = GoalService(
            sessions=self.sessions, run_manager=self.runs, chat_loop=cast(Any, Mock())
        )
        register_update_goal_tool(self.registry, self.goals)
        register_create_goal_tool(self.registry, self.goals)
        self.tmp_path = tmp_path

    async def call(self, arguments: Any) -> tuple[bool, str]:
        result = await self.call_result(arguments)
        return bool(result["ok"]), str(tool_result_text(json.dumps(result)))

    async def call_result(
        self,
        arguments: Any,
        *,
        name: str = "update_goal",
        kind: RunKind = RunKind.USER,
        subagent: bool = False,
    ) -> dict[str, Any]:
        context = ToolContext(
            agent_id="coder",
            session_id="session-one",
            run_id="run-1",
            tool_call_id="call",
            tool_name=name,
            tool_call_index=0,
            workspace=self.tmp_path,
            vbot_root=self.tmp_path,
            data_root=self.tmp_path,
            session_tool_grants=("update_goal",),
            permissions=PermissionBoundary.UNRESTRICTED,
            run_kind=kind,
            subagent_session=subagent,
        )
        if name == "create_goal":

            async def executor(run: Run) -> dict[str, Any]:
                return await dispatch_as_executor(
                    self.registry, replace(context, run_id=run.id), arguments
                )

            run = await self.runs.start(_ADDRESS, executor, admission=RunAdmission(run_kind=kind))
            return cast(dict[str, Any], await run.wait())
        return await dispatch_as_executor(self.registry, context, arguments)

    def status(self) -> str:
        goal = self.sessions.metadata_value(_ADDRESS, SESSION_GOAL_META_KEY)
        return cast(str, goal["status"])


def _goal(status: str = "active") -> dict[str, Any]:
    return {"objective": "Ship the release", "status": status, "turns": 3, "max_turns": 30}


@pytest.mark.parametrize(
    ("arguments", "status"),
    [
        (
            {"status": "complete"},
            "complete",
        ),
        (
            {"status": "Done"},
            "complete",
        ),
        (
            {"status": "stuck", "blocked_reason": "Needs deployment credentials"},
            "blocked",
        ),
        (
            {"status": "stop", "summary": "Tests pass"},
            "paused",
        ),
    ],
)
async def test_update_goal_sets_the_status_the_agent_means(
    tmp_path: Path, arguments: dict[str, Any], status: str
) -> None:
    tool = _Tool(tmp_path, goal=_goal())

    ok, text = await tool.call(arguments)
    assert ok
    assert tool.status() == status
    if status == "blocked":
        assert tool.sessions.metadata_value(_ADDRESS, SESSION_GOAL_META_KEY)["blocked_reason"] == (
            "Needs deployment credentials"
        )
    if "summary" in arguments:
        assert "summary" in text


async def test_the_same_status_changes_nothing(tmp_path: Path) -> None:
    tool = _Tool(tmp_path, goal=_goal("blocked"))

    assert await tool.call({"status": "blocked"}) == (
        True,
        "The goal is already blocked; nothing changed.",
    )


@pytest.mark.parametrize(
    ("goal", "arguments", "code"),
    [
        (
            _goal(),
            {"status": "finished-ish"},
            "invalid_arguments",
        ),
        (
            None,
            {"status": "complete"},
            "no_goal",
        ),
        (
            {**_goal(), "turns": 2},
            {"status": "blocked", "blocked_reason": "Key missing"},
            "goal_blocked_too_early",
        ),
        (_goal(), {"status": "blocked"}, "invalid_arguments"),
        (_goal(), {"status": "blocked", "blocked_reason": " "}, "invalid_arguments"),
    ],
)
async def test_update_goal_refuses_with_what_to_do(
    tmp_path: Path, goal: dict[str, Any] | None, arguments: dict[str, Any], code: str
) -> None:
    tool = _Tool(tmp_path, goal=goal)

    result = await tool.call_result(arguments)

    assert not result["ok"]
    assert result["error"]["code"] == code
    if goal is not None:
        assert tool.status() == "active"


@pytest.mark.parametrize(
    ("kind", "subagent", "goal", "objective", "code"),
    [
        (RunKind.USER, False, None, "Finish the report", None),
        (RunKind.USER, False, _goal("complete"), "Next report", None),
        (RunKind.USER, False, _goal(), "Replace it", "goal_exists"),
        (RunKind.USER, False, None, " ", "invalid_arguments"),
        (RunKind.CRON, False, None, "Keep running", "goal_creation_not_allowed"),
        (RunKind.SYSTEM, False, None, "Keep running", "goal_creation_not_allowed"),
        (RunKind.SESSION_MESSAGE, False, None, "Keep running", "goal_creation_not_allowed"),
        (RunKind.USER, True, None, "Keep running", "goal_creation_not_allowed"),
    ],
)
async def test_create_goal_starts_only_an_authorized_new_objective(
    tmp_path: Path,
    kind: RunKind,
    subagent: bool,
    goal: dict[str, Any] | None,
    objective: str,
    code: str | None,
) -> None:
    tool = _Tool(tmp_path, goal=goal)
    result = await tool.call_result(
        {"objective": objective}, name="create_goal", kind=kind, subagent=subagent
    )
    stored = tool.sessions.metadata_value(_ADDRESS, SESSION_GOAL_META_KEY)
    if code is not None:
        assert not result["ok"] and result["error"]["code"] == code
        assert stored == goal
    else:
        assert result["ok"]
        assert stored["objective"] == objective
        assert stored["status"] == "active" and stored["turns"] == 0
        assert stored["max_turns"] == 30
        assert [m.goal_event.event for m in tool.sessions.get(_ADDRESS).load() if m.goal_event] == [
            "set"
        ]
