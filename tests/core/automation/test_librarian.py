"""Contracts of the Librarian: aging, consolidation, its state, status and schedule."""

from __future__ import annotations

import asyncio
import json
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

import core.automation.librarian as librarian_module
from core.agents import AgentNotFoundError
from core.automation.librarian import (
    CHECK_INTERVAL_SECONDS,
    FIRST_CHECK_DELAY_SECONDS,
    LIBRARIAN_TOOL_ITERATION_LIMIT,
    LIBRARIAN_TOOL_RESTRICTION,
    LibrarianBusyError,
    LibrarianService,
    LibrarianStateError,
    LibrarianUnavailableError,
    librarian_candidates,
    librarian_tool_denial_resolver,
    validate_librarian_state_file,
)
from core.prompts.briefs import librarian_brief
from core.runs import RunKind
from core.skills import SkillAuthoringService, SkillWriter
from core.statistics.skills import SkillUse
from core.storage import StorageManager
from core.tools.availability import ToolAccess
from core.utils.timestamps import format_canonical_timestamp

_TOOLS = (
    SimpleNamespace(name="read"),
    SimpleNamespace(name="skill"),
    SimpleNamespace(name="skill_manage", constraints=("identity_agent",)),
)
_AGENT = SkillWriter(actor="agent", session_id="s-1", run_id="r-1", run_kind="user")
_REFLECTION = SkillWriter(actor="reflection", session_id="s-2", run_id="r-2", run_kind="reflection")
_HUMAN = SkillWriter(actor="human")
# Skills are written now; the Librarian's clock runs this far ahead.
_LATER = timedelta(days=200)


def _document(name: str, description: str = "Do a demo task.", body: str = "# Demo\n") -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n\n{body}"


class _Harness:
    """A Librarian over real Skill homes and fakes for Agents, Sessions and Runs."""

    def __init__(self, data_dir: Path) -> None:
        self.storage = StorageManager(data_dir=data_dir)
        self.authoring = SkillAuthoringService()
        self.settings: dict[str, Any] = {
            "enabled": True,
            "interval_days": 7,
            "archive_after_days": 90,
            "consolidate": True,
        }
        self.agents: dict[str, Any] = {"main": self.agent()}
        self.busy: set[str] = set()
        self.usage: dict[tuple[str, str], SkillUse] = {}
        self.scheduled: dict[str, frozenset[str]] = {}
        self.changed: list[str] = []
        self.announced = 0
        self.started: list[dict[str, Any]] = []
        self.created_sessions: list[dict[str, Any]] = []
        self.deleted_sessions: list[Any] = []
        # Called with the start kwargs while the consolidation Run "runs".
        self.on_run: Any = None
        self.run_gate: asyncio.Event | None = None
        self.now = datetime.now(UTC) + _LATER

        async def resolve_agent_async(_project_id: str | None, agent_id: str) -> Any:
            return self.agents[agent_id]

        runtime = SimpleNamespace(
            storage=SimpleNamespace(
                data_dir=data_dir,
                load_librarian_settings=lambda: dict(self.settings),
                read_prompt_fragment=self.storage.read_prompt_fragment,
            ),
            agents=SimpleNamespace(
                list=lambda: [SimpleNamespace(id=agent_id) for agent_id in self.agents],
                exists=lambda agent_id: agent_id in self.agents,
                lifecycle_guard=nullcontext,
            ),
            agent_resolver=SimpleNamespace(resolve_agent_async=resolve_agent_async),
            tools=SimpleNamespace(list_tools=lambda: list(_TOOLS)),
            chat_sessions=SimpleNamespace(create_async=self._create_session, delete=self._delete),
            streaming_chat_loop=SimpleNamespace(start_run=self._start_run),
            chat_run_manager=SimpleNamespace(
                has_activity_for_agent=lambda agent_id, *, project_id: agent_id in self.busy
            ),
        )

        async def skill_usage() -> dict[tuple[str, str], SkillUse]:
            return dict(self.usage)

        self.service = LibrarianService(
            cast("Any", runtime),
            authoring=self.authoring,
            skills_dir=self.home,
            skill_usage=skill_usage,
            triggered_skill_names=lambda agent_id: self.scheduled.get(agent_id, frozenset()),
            skills_changed=self.changed.append,
            status_changed=self.announce,
            clock=lambda: self.now,
        )

    @staticmethod
    def agent(**tool_access: Any) -> Any:
        return SimpleNamespace(
            workspace="/data/workspace-main",
            memory_prompt_mode="agent_user",
            tool_access=ToolAccess(**tool_access),
        )

    def announce(self) -> None:
        self.announced += 1

    def home(self, agent_id: str) -> Path:
        return self.storage.data_dir / "agents" / agent_id / "skills"

    def state(self, agent_id: str = "main") -> dict[str, Any]:
        path = self.storage.data_dir / "agents" / agent_id / "librarian.json"
        return cast("dict[str, Any]", json.loads(path.read_text(encoding="utf-8")))

    async def run_pass(self, agent_id: str = "main") -> dict[str, Any]:
        """Start a manual pass, wait for it, and return the status after it."""
        await self.service.run(agent_id)
        await asyncio.gather(
            *(active.task for active in self.service._active.values() if active.task)
        )
        return await self.service.status(agent_id)

    async def _create_session(self, agent_id: str, **kwargs: Any) -> Any:
        self.created_sessions.append({"agent_id": agent_id, **kwargs})
        return SimpleNamespace(
            id=f"lib-{len(self.created_sessions)}", address=("address", agent_id)
        )

    def _delete(self, address: Any) -> None:
        self.deleted_sessions.append(address)

    async def _start_run(self, agent_id: str, content: str, **kwargs: Any) -> Any:
        started = {"agent_id": agent_id, "message": content, **kwargs}
        self.started.append(started)
        run = SimpleNamespace(id=f"run-lib-{len(self.started)}")
        if self.on_run is not None:
            self.on_run(started, run.id)
        gate = self.run_gate

        async def wait() -> Any:
            if gate is not None:
                await gate.wait()
            return SimpleNamespace(content="Nothing to change.")

        run.wait = wait
        return run


@pytest.fixture
def harness(tmp_path: Path) -> _Harness:
    return _Harness(tmp_path / "data")


@pytest.mark.asyncio
async def test_a_pass_archives_inactive_background_skills_and_reports_them(
    harness: _Harness,
) -> None:
    root = harness.home("main")
    for name, writer in (
        ("old-review", _REFLECTION),
        ("old-pass", SkillWriter(actor="librarian", run_kind="librarian")),
        ("old-pinned", _REFLECTION),
        ("old-agent", _AGENT),
        ("old-human", _HUMAN),
        ("used-recently", _REFLECTION),
        ("scheduled", _REFLECTION),
    ):
        harness.authoring.create(root, name, _document(name), writer=writer)
    harness.authoring.set_pinned(root, "old-pinned", True, writer=_HUMAN)
    recent = format_canonical_timestamp(harness.now - timedelta(days=10))
    harness.usage = {
        ("main", "used-recently"): SkillUse(last_activated=recent, count=3),
        # Another Agent's use of the same name does not keep this Agent's Skill.
        ("other", "old-review"): SkillUse(last_activated=recent, count=1),
    }
    harness.scheduled = {"main": frozenset({"scheduled"})}
    harness.settings["consolidate"] = False

    status = await harness.run_pass()

    assert sorted(harness.authoring.records(root)) == [
        "old-agent",
        "old-human",
        "old-pinned",
        "scheduled",
        "used-recently",
    ]
    assert [
        (change["skill"], change["actor"], change["reason"], change["run_kind"])
        for change in status["changes"]
    ] == [
        ("old-review", "librarian", "inactive", "librarian"),
        ("old-pass", "librarian", "inactive", "librarian"),
    ]
    assert harness.changed == ["main"]
    # Observers learn that the pass started and that it ended.
    assert harness.announced == 2
    last_pass = status["last_pass"]
    assert (last_pass["trigger"], last_pass["archived"], last_pass["consolidation"]) == (
        "manual",
        2,
        "disabled",
    )
    finished = datetime.fromisoformat(last_pass["finished_at"])
    assert datetime.fromisoformat(status["next_due_at"]) == finished + timedelta(days=7)
    assert (status["running"], status["available"]) == (False, True)
    # The persisted state: the pass and the archive revisions its report reads.
    document = harness.state()
    assert document["format_version"] == 1
    assert document["last_pass"]["archived_revisions"] == [
        change["id"] for change in reversed(status["changes"])
    ]
    path = harness.storage.data_dir / "agents" / "main" / "librarian.json"
    assert validate_librarian_state_file(path).diagnostics == ()


@pytest.mark.asyncio
async def test_consolidation_runs_the_brief_only_over_changed_candidates(
    harness: _Harness,
) -> None:
    root = harness.home("main")
    harness.authoring.create(root, "deploy-web", _document("deploy-web"), writer=_AGENT)
    harness.authoring.create(root, "deploy-api", _document("deploy-api"), writer=_REFLECTION)
    harness.authoring.create(root, "deploy-docs", _document("deploy-docs"), writer=_HUMAN)
    harness.authoring.create(root, "deploy-db", _document("deploy-db"), writer=_AGENT)
    harness.authoring.set_pinned(root, "deploy-db", True, writer=_HUMAN)
    # Nothing is old enough to age.
    harness.now = datetime.now(UTC)

    def merge(started: dict[str, Any], run_id: str) -> None:
        writer = SkillWriter(
            actor="librarian", session_id=started["session_id"], run_id=run_id, run_kind="librarian"
        )
        harness.authoring.edit(
            root, "deploy-web", _document("deploy-web", body="# Web and API\n"), writer=writer
        )
        harness.authoring.delete(
            root, "deploy-api", writer=writer, reason="absorbed", absorbed_into="deploy-web"
        )

    harness.on_run = merge
    candidates = librarian_candidates(harness.authoring, root, usage={}, scheduled=frozenset())

    status = await harness.run_pass()

    [started] = harness.started
    assert started["message"] == librarian_brief(
        harness.storage, candidates, limit=LIBRARIAN_TOOL_ITERATION_LIMIT
    )
    # Only unpinned Skills that the Agent or a background Run created are listed.
    assert "- deploy-web\n" in started["message"] and "- deploy-api\n" in started["message"]
    assert "- deploy-docs" not in started["message"] and "- deploy-db" not in started["message"]
    assert {
        key: started[key]
        for key in (
            "agent_id",
            "session_id",
            "internal",
            "tool_restriction",
            "max_tool_iterations",
            "run_kind",
            "contributes_to_agent_activity",
        )
    } == {
        "agent_id": "main",
        "session_id": "lib-1",
        "internal": True,
        "tool_restriction": LIBRARIAN_TOOL_RESTRICTION,
        "max_tool_iterations": 60,
        "run_kind": RunKind.LIBRARIAN,
        "contributes_to_agent_activity": False,
    }
    assert harness.created_sessions == [{"agent_id": "main", "run_kind": RunKind.LIBRARIAN}]
    assert started["tool_denial_resolver"]("read") == librarian_tool_denial_resolver()("read")
    last_pass = status["last_pass"]
    assert {
        key: last_pass[key]
        for key in ("candidates", "consolidation", "session_id", "run_id", "created", "changed")
    } == {
        "candidates": 2,
        "consolidation": "ran",
        "session_id": "lib-1",
        "run_id": "run-lib-1",
        "created": 0,
        "changed": 1,
    }
    assert last_pass["merged"] == 1
    assert [(change["skill"], change["kind"]) for change in status["changes"]] == [
        ("deploy-api", "archive"),
        ("deploy-web", "change"),
    ]

    # Unchanged candidates are not consolidated again.
    harness.authoring.create(root, "deploy-cli", _document("deploy-cli"), writer=_AGENT)
    harness.on_run = None
    await harness.run_pass()
    assert len(harness.started) == 2
    status = await harness.run_pass()
    assert len(harness.started) == 2
    assert status["last_pass"]["consolidation"] == "unchanged"
    assert status["changes"] == []

    # A single candidate is never consolidated.
    harness.authoring.delete(root, "deploy-cli", writer=_HUMAN)
    status = await harness.run_pass()
    assert (status["last_pass"]["consolidation"], len(harness.started)) == ("too_few", 2)


@pytest.mark.asyncio
async def test_run_refuses_clearly_and_changes_nothing(harness: _Harness) -> None:
    root = harness.home("main")
    harness.authoring.create(root, "old-review", _document("old-review"), writer=_REFLECTION)
    harness.authoring.create(root, "other-review", _document("other-review"), writer=_REFLECTION)
    harness.now = datetime.now(UTC)
    harness.run_gate = asyncio.Event()
    harness.agents["limited"] = harness.agent(denied=("skill_manage",))

    await harness.service.run("main")
    status = await harness.service.status("main")
    assert status["running"] is True and status["running_since"] is not None
    with pytest.raises(LibrarianBusyError, match="already running"):
        await harness.service.run("main")
    harness.run_gate.set()
    await asyncio.gather(*(a.task for a in harness.service._active.values() if a.task))

    harness.busy.add("main")
    with pytest.raises(LibrarianBusyError, match="active or queued Run"):
        await harness.service.run("main")
    with pytest.raises(LibrarianUnavailableError, match="cannot call skill and skill_manage"):
        await harness.service.run("limited")
    with pytest.raises(AgentNotFoundError):
        await harness.service.run("ghost")
    harness.busy.clear()
    (harness.storage.data_dir / "agents" / "main" / "librarian.json").write_text(
        "{", encoding="utf-8"
    )
    revisions = len(harness.authoring.history(root))
    harness.now += _LATER
    with pytest.raises(LibrarianStateError):
        await harness.service.run("main")
    assert len(harness.authoring.history(root)) == revisions


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_schedule_passes_due_idle_and_eligible_agents_once_an_hour(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    harness.settings["consolidate"] = False
    harness.settings["enabled"] = enabled
    for agent_id in ("due", "busy", "recent", "limited"):
        harness.agents[agent_id] = harness.agent()
        harness.authoring.create(
            harness.home(agent_id), "old-review", _document("old-review"), writer=_REFLECTION
        )
    harness.agents["limited"] = harness.agent(denied=("skill",))
    del harness.agents["main"]
    harness.busy.add("busy")
    recent = harness.storage.data_dir / "agents" / "recent" / "librarian.json"
    passed = format_canonical_timestamp(harness.now - timedelta(days=1))
    recent.write_text(
        json.dumps(
            {
                "format_version": 1,
                "last_pass": {
                    "started_at": passed,
                    "finished_at": passed,
                    "trigger": "schedule",
                    "consolidation": "disabled",
                },
            }
        ),
        encoding="utf-8",
    )
    delays: list[float] = []
    checked = asyncio.Event()

    async def wait(seconds: float) -> None:
        delays.append(seconds)
        if len(delays) > 1:
            checked.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(librarian_module, "_wait", wait)

    harness.service.start()
    try:
        await asyncio.wait_for(checked.wait(), timeout=10)
    finally:
        await harness.service.aclose()

    assert delays == [FIRST_CHECK_DELAY_SECONDS, CHECK_INTERVAL_SECONDS]
    if enabled:
        assert harness.state("due")["last_pass"]["trigger"] == "schedule"
    assert harness.changed == (["due"] if enabled else [])
    for agent_id in ("busy", "recent", "limited", *(() if enabled else ("due",))):
        assert "old-review" in harness.authoring.records(harness.home(agent_id))
    assert not (harness.storage.data_dir / "agents" / "busy" / "librarian.json").exists()
