"""``/handoff``, ``/learn`` and ``/reflect``: commands that work through internal Runs.

The dispatcher runs against recorded collaborators: the Agent resolver, the Session
and Agent stores, Run admission and the Trigger service. ``/reflect`` goes through
a real ``ReflectionService`` over recorded Session and Run I/O, so the command's
fork, review Run and counter reset follow the Reflection owner's contract.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from core.automation.reflection import (
    COUNTER_GENERATION_KEY,
    REFLECTION_COUNTERS_META_KEY,
    ReflectionService,
)
from core.chat import (
    ChatMessage,
    CommandDispatcher,
    CommandNavigation,
    CommandResourceChange,
    ReplySurface,
)
from core.projects import ResolutionAgentNotFoundError, format_agent_address
from core.prompts.briefs import learn_brief, reflection_brief
from core.runs import RunKind
from core.sessions import (
    AGENT_DEFAULT_PROJECT,
    SESSION_WORKING_PROJECT_META_KEY,
    SessionAddress,
    WorkingProjectChoice,
)
from core.tools.availability import ToolAccess
from tests.core.chat.commands_test_support import _execute

pytestmark = pytest.mark.asyncio

WEBUI = ReplySurface.webui()

# The trailing newline must not leak into the handoff-writing prompt.
_HANDOFF_FRAGMENT = "Write a handoff for the next agent.\n"

# Registered Tools, with the activation the Tools domain declares for them.
_TOOLS = (
    SimpleNamespace(name="memory", activation="memory_mode", constraints=("identity_agent",)),
    SimpleNamespace(name="skill"),
    SimpleNamespace(name="skill_manage", constraints=("identity_agent",)),
)


def _fragment_storage() -> SimpleNamespace:
    """Prompt fragments the command briefs are read from; brief fragments read as their names."""
    return SimpleNamespace(
        read_prompt_fragment=lambda name: _HANDOFF_FRAGMENT if name == "handoff.md" else f"[{name}]"
    )


class _AnsweredRun:
    def __init__(self, answer: str) -> None:
        self._answer = answer

    async def wait(self) -> ChatMessage:
        return ChatMessage.assistant(content=self._answer, model="openai/gpt-5.2")


class _Trigger:
    """Trigger service double: records each Run request; every Run answers ``answer``."""

    def __init__(self, answer: str = "Handoff: finish the parser refactor.") -> None:
        self.answer = answer
        self.runs: list[dict[str, Any]] = []

    async def trigger_run(self, agent_id: str, message: Any, **kwargs: Any) -> _AnsweredRun:
        self.runs.append({"agent_id": agent_id, "message": message, **kwargs})
        return _AnsweredRun(self.answer)


class _Resolver:
    """Resolves every Agent except ``ghost``, recording each resolve target.

    ``librarian`` resolves as the built-in Librarian.
    """

    def __init__(
        self,
        *,
        workspace: str = "/home/agent",
        memory_prompt_mode: str = "agent_user",
        tool_access: ToolAccess | None = None,
    ):
        self.agent = SimpleNamespace(
            name="Builder",
            workspace=workspace,
            memory_prompt_mode=memory_prompt_mode,
            tool_access=tool_access or ToolAccess(),
        )
        self.resolved: list[tuple[str | None, str]] = []

    def resolve_agent(self, project_id: str | None, agent_id: str) -> SimpleNamespace:
        self.resolved.append((project_id, agent_id))
        if agent_id == "ghost":
            raise ResolutionAgentNotFoundError(f"unknown agent: {agent_id}")
        if agent_id == "librarian":
            return SimpleNamespace(**{**vars(self.agent), "builtin": "librarian"})
        return self.agent

    async def resolve_agent_async(self, project_id: str | None, agent_id: str) -> SimpleNamespace:
        return self.resolve_agent(project_id, agent_id)


class _NewSessions:
    """Records each created Session with its working Project choice.

    ``working_projects`` names the Project each existing Identity Session works in.
    """

    def __init__(self) -> None:
        self.created: list[tuple[str, str | None, WorkingProjectChoice]] = []
        self.working_projects: dict[str, str | None] = {}

    def metadata_value(self, address: SessionAddress, key: str) -> str | None:
        assert key == SESSION_WORKING_PROJECT_META_KEY
        return address.project_id or self.working_projects.get(address.session_id)

    def create(
        self,
        agent_id: str,
        *,
        project_id: str | None = None,
        actor: str | None = None,
        working_project_id: WorkingProjectChoice = AGENT_DEFAULT_PROJECT,
    ) -> SimpleNamespace:
        self.created.append((agent_id, project_id, working_project_id))
        return SimpleNamespace(id="new-session")


class _Agents:
    def __init__(self) -> None:
        self.updates: list[tuple[str, dict[str, Any]]] = []

    def update(self, agent_id: str, **changes: Any) -> None:
        self.updates.append((agent_id, changes))


def _runs(*, active: bool) -> Any:
    active_run = object() if active else None
    return SimpleNamespace(active_run=lambda **_address: active_run)


# ---------------------------------------------------------------------------
# /handoff
# ---------------------------------------------------------------------------


class _Handoff:
    def __init__(self, *, active: bool = False, answer: str | None = None) -> None:
        self.resolver = _Resolver()
        self.sessions = _NewSessions()
        self.agents = _Agents()
        self.trigger = _Trigger() if answer is None else _Trigger(answer)
        self.changes: list[CommandResourceChange] = []
        self.dispatcher = CommandDispatcher(
            _runs(active=active),
            agent_resolver=cast(Any, self.resolver),
            sessions=cast(Any, self.sessions),
            agents=cast(Any, self.agents),
            trigger_service=self.trigger,
            storage=_fragment_storage(),
        )

    async def run(self, message: str, *, project_id: str | None = None) -> Any:
        return await _execute(
            self.dispatcher,
            message,
            agent_id="builder",
            session_id="s1",
            project_id=project_id,
            on_change=self.changes.append,
        )


@pytest.mark.parametrize(
    ("message", "source_project", "source_working", "target", "target_project", "instruction"),
    [
        pytest.param("/handoff", None, None, "builder", None, None, id="same-agent"),
        pytest.param(
            "/handoff agent:reviewer don't forget the plates!",
            None,
            "alpha",
            "reviewer",
            None,
            "don't forget the plates!",
            id="identity-target-with-instruction",
        ),
        pytest.param(
            "/handoff agent:reviewer",
            "vbot",
            None,
            "reviewer",
            None,
            None,
            id="identity-target-from-a-project-session",
        ),
        pytest.param(
            "/handoff agent:orchestrator@vbot",
            None,
            "alpha",
            "orchestrator",
            "vbot",
            None,
            id="project",
        ),
        pytest.param(
            "/handoff keep the deployment notes",
            "vbot",
            None,
            "builder",
            "vbot",
            "keep the deployment notes",
            id="bare-stays-in-source-scope",
        ),
    ],
)
async def test_handoff_starts_the_target_on_the_written_handoff_in_a_new_session(
    message: str,
    source_project: str | None,
    source_working: str | None,
    target: str,
    target_project: str | None,
    instruction: str | None,
) -> None:
    handoff = _Handoff()
    handoff.sessions.working_projects["s1"] = source_working

    result = await handoff.run(message, project_id=source_project)

    writer, receiver = handoff.trigger.runs
    # The source Agent writes the handoff in an internal Run on the source Session.
    assert (writer["agent_id"], writer["session_id"], writer["project_id"]) == (
        "builder",
        "s1",
        source_project,
    )
    assert writer["internal"] is True
    if instruction is None:
        assert writer["message"] == "Write a handoff for the next agent."
    else:
        assert writer["message"].startswith("Write a handoff for the next agent.\n\n")
        assert writer["message"].endswith(instruction)
    # Another Agent is resolved before any Run starts; the source Agent is not.
    if (target, target_project) != ("builder", source_project):
        assert handoff.resolver.resolved == [(target_project, target)]
    else:
        assert handoff.resolver.resolved == []
    # The receiver starts in its fresh Session with the written handoff as its message.
    # An Identity target continues in the Project the source Session works in; a
    # Team target works in its Team's Project.
    working_project = (
        (source_project or source_working) if target_project is None else AGENT_DEFAULT_PROJECT
    )
    assert handoff.sessions.created == [(target, target_project, working_project)]
    assert receiver == {
        "agent_id": target,
        "message": handoff.trigger.answer,
        "session_id": "new-session",
        "project_id": target_project,
        "internal": False,
        "reply_surface": WEBUI,
    }
    assert writer["reply_surface"] == WEBUI
    # Only an identity target's current Session pointer moves to the new Session.
    expected_updates = (
        [(target, {"current_session_id": "new-session"})] if target_project is None else []
    )
    assert handoff.agents.updates == expected_updates
    target_address = format_agent_address(target, target_project)
    assert result.facts == {"session_id": "new-session", "agent_id": target_address}
    assert result.navigation == CommandNavigation(
        kind="offer_session", agent_id=target, session_id="new-session", project_id=target_project
    )
    [follow_up] = result.runs
    assert follow_up.role == "follow_up"
    scope = {"agent_id": target, "session_id": "new-session"}
    if target_project is not None:
        scope["project_id"] = target_project
    change = CommandResourceChange(kind="sessions", scope=scope)
    assert handoff.changes == [change]
    assert result.resource_changes == (change,)


@pytest.mark.parametrize(
    ("message", "active", "answer", "started_runs", "reason"),
    [
        pytest.param("/handoff agent:ghost", False, None, 0, "ghost", id="unknown-target"),
        pytest.param(
            "/handoff agent:librarian", False, None, 0, "built-in Librarian", id="librarian"
        ),
        pytest.param("/handoff agent:a@b@c", False, None, 0, "a@b@c", id="invalid-address"),
        pytest.param("/handoff", True, None, 0, "current run", id="run-active"),
        # The writer ran but produced no handoff text.
        pytest.param("/handoff", False, "", 1, "could not be generated", id="empty-handoff"),
    ],
)
async def test_handoff_is_refused_without_creating_a_session(
    message: str, active: bool, answer: str | None, started_runs: int, reason: str
) -> None:
    handoff = _Handoff(active=active, answer=answer)

    result = await handoff.run(message)

    assert result.feedback is not None
    assert result.feedback.kind == "notice"
    assert reason in result.feedback.text
    assert len(handoff.trigger.runs) == started_runs
    assert handoff.sessions.created == []
    assert handoff.agents.updates == []
    assert (result.runs, result.navigation, handoff.changes) == ((), None, [])


# ---------------------------------------------------------------------------
# /learn
# ---------------------------------------------------------------------------


def _learn_dispatcher(trigger: _Trigger, *, workspace: str, active: bool) -> CommandDispatcher:
    return CommandDispatcher(
        _runs(active=active),
        agent_resolver=cast(Any, _Resolver(workspace=workspace)),
        trigger_service=trigger,
        storage=_fragment_storage(),
    )


@pytest.mark.parametrize("argument", ["the deploy steps", None])
async def test_learn_starts_an_internal_skill_authoring_run(argument: str | None) -> None:
    trigger = _Trigger(answer="Created the deploy skill.")
    dispatcher = _learn_dispatcher(trigger, workspace="/home/agent", active=False)
    message = "/learn" if argument is None else f"/learn {argument}"

    result = await _execute(dispatcher, message, agent_id="builder", session_id="s1")

    [run] = trigger.runs
    assert (run["agent_id"], run["session_id"], run["project_id"]) == ("builder", "s1", None)
    assert run["internal"] is True
    assert run["reply_surface"] == WEBUI
    assert run["message"] == learn_brief(_fragment_storage(), argument)
    # The feedback is the authoring Run's final answer.
    assert result.feedback is not None
    assert (result.feedback.kind, result.feedback.text) == ("notice", "Created the deploy skill.")


@pytest.mark.parametrize(
    ("workspace", "active"),
    [("/home/agent", True), ("", False)],
    ids=["run-active", "config-agent"],
)
async def test_learn_is_refused_without_starting_a_run(workspace: str, active: bool) -> None:
    trigger = _Trigger()
    dispatcher = _learn_dispatcher(trigger, workspace=workspace, active=active)

    result = await _execute(dispatcher, "/learn deploy", agent_id="builder", session_id="s1")

    assert result.feedback is not None
    assert result.feedback.text.strip()
    assert trigger.runs == []


# ---------------------------------------------------------------------------
# /reflect
# ---------------------------------------------------------------------------


class _ReflectSessions:
    """Session store double for ``/reflect``: records forks, titles and metadata."""

    def __init__(self) -> None:
        self.forks: list[dict[str, Any]] = []
        self.titles: list[tuple[str, str]] = []
        self.metadata_writes: list[tuple[str, dict[str, Any]]] = []
        self.stored: dict[str, Any] = {}

    async def fork(
        self,
        source: SessionAddress,
        *,
        target_agent_id: str | None = None,
        target_project_id: str | None = None,
        title: str | None = None,
        run_kind: RunKind | None = None,
    ) -> Any:
        self.forks.append(
            {
                "source_agent_id": source.agent_id,
                "session_id": source.session_id,
                "target_agent_id": target_agent_id,
                "target_project_id": target_project_id,
                "run_kind": run_kind,
            }
        )
        if title is not None:
            self.titles.append(("fork-1", title))
        return SimpleNamespace(id="fork-1")

    async def metadata_value_async(self, address: SessionAddress, key: str) -> Any:
        return None

    def get_metadata(self, address: SessionAddress) -> dict[str, Any]:
        return {}

    def set_metadata(self, address: SessionAddress, data: dict[str, Any]) -> None:
        self.metadata_writes.append((address.session_id, data))

    def mutate_metadata(self, address: SessionAddress, mutation: Any) -> dict[str, Any]:
        metadata: dict[str, Any] = dict(self.stored)
        mutation(metadata)
        self.metadata_writes.append((address.session_id, metadata))
        return metadata


class _Reflect:
    """A dispatcher whose ``ReflectionService`` is real over recorded Session and Run I/O."""

    def __init__(
        self,
        *,
        workspace: str = "/home/agent",
        memory_prompt_mode: str = "agent_user",
        tool_access: ToolAccess | None = None,
        active: bool = False,
    ) -> None:
        self.sessions = _ReflectSessions()
        self.review_runs: list[dict[str, Any]] = []
        self.changes: list[CommandResourceChange] = []
        resolver = _Resolver(
            workspace=workspace, memory_prompt_mode=memory_prompt_mode, tool_access=tool_access
        )

        async def start_run(agent_id: str, content: Any, **kwargs: Any) -> _AnsweredRun:
            self.review_runs.append({"agent_id": agent_id, "message": content, **kwargs})
            return _AnsweredRun("Updated the deploy memory.")

        runtime = SimpleNamespace(
            agent_resolver=resolver,
            chat_sessions=self.sessions,
            storage=_fragment_storage(),
            streaming_chat_loop=SimpleNamespace(start_run=start_run),
            tools=SimpleNamespace(list_tools=lambda: list(_TOOLS)),
        )
        self.dispatcher = CommandDispatcher(
            _runs(active=active),
            agent_resolver=cast(Any, resolver),
            reflection_service=ReflectionService(cast(Any, runtime)),
        )

    async def run(self, message: str) -> Any:
        return await _execute(
            self.dispatcher,
            message,
            agent_id="builder",
            session_id="s1",
            on_change=self.changes.append,
        )


@pytest.mark.parametrize("focus", ["focus on the memory side", None])
async def test_reflect_reviews_a_fork_with_a_restricted_run(focus: str | None) -> None:
    reflect = _Reflect()

    result = await reflect.run("/reflect" if focus is None else f"/reflect {focus}")

    # The fork stays in the source's scope and is classified as a reflection in
    # the same write that creates it; it is titled with the agent's name.
    assert reflect.sessions.forks == [
        {
            "source_agent_id": "builder",
            "session_id": "s1",
            "target_agent_id": None,
            "target_project_id": None,
            "run_kind": RunKind.REFLECTION,
        }
    ]
    assert reflect.sessions.titles == [("fork-1", "Builder")]
    [run] = reflect.review_runs
    assert run["session_id"] == "fork-1"
    assert run["internal"] is True
    assert run["run_kind"] is RunKind.REFLECTION
    assert run["tool_restriction"] == ("memory", "skill", "skill_manage")
    assert "tool_grants" not in run
    assert run["reply_surface"] == WEBUI
    assert run["message"] == reflection_brief(_fragment_storage(), "combined", focus=focus)
    # A manual review covers both dimensions: the SOURCE Session's counters reset.
    assert reflect.sessions.metadata_writes == [
        (
            "s1",
            {
                REFLECTION_COUNTERS_META_KEY: {
                    "turns_since_memory_review": 0,
                    "iterations_since_skill_review": 0,
                    COUNTER_GENERATION_KEY: 1,
                }
            },
        )
    ]
    # The fork is reported while the review runs and again in the outcome.
    fork = CommandResourceChange(
        kind="sessions", scope={"agent_id": "builder", "session_id": "fork-1"}
    )
    assert reflect.changes == [fork]
    assert result.resource_changes == (fork,)
    assert result.feedback is not None
    assert result.feedback.text == "Updated the deploy memory."
    assert result.facts == {"session_id": "fork-1", "agent_id": "builder"}


async def test_reflect_narrows_to_the_tools_the_agent_can_call() -> None:
    reflect = _Reflect(memory_prompt_mode="off")
    reflect.sessions.stored = {
        REFLECTION_COUNTERS_META_KEY: {
            "turns_since_memory_review": 7,
            "iterations_since_skill_review": 12,
        }
    }

    await reflect.run("/reflect")

    [run] = reflect.review_runs
    assert run["run_kind"] is RunKind.SKILL_REFLECTION
    assert run["tool_restriction"] == ("skill", "skill_manage")
    assert run["message"] == reflection_brief(_fragment_storage(), "skill")
    # Only the reviewed dimension's counter resets.
    assert reflect.sessions.metadata_writes == [
        (
            "s1",
            {
                REFLECTION_COUNTERS_META_KEY: {
                    "turns_since_memory_review": 7,
                    "iterations_since_skill_review": 0,
                    COUNTER_GENERATION_KEY: 1,
                }
            },
        )
    ]


@pytest.mark.parametrize(
    "refusal",
    [
        {"active": True},
        {"workspace": ""},
        {"memory_prompt_mode": "off", "tool_access": ToolAccess(denied=("skill_manage",))},
    ],
    ids=["run-active", "config-agent", "no-learning-tools"],
)
async def test_reflect_is_refused_before_forking(refusal: dict[str, Any]) -> None:
    reflect = _Reflect(**refusal)

    result = await reflect.run("/reflect")

    assert result.feedback is not None
    assert result.feedback.text.strip()
    assert reflect.sessions.forks == []
    assert reflect.review_runs == []
