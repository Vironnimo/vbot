"""AutomationReferences: which live automations start Runs of an Agent, Project or Session."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any, cast

import pytest

from core.automation import AutomationReference, AutomationReferences
from core.sessions import SessionAddress

_SESSION = SessionAddress(project_id=None, agent_id="builder", session_id="s1")


class _Automations:
    """Bootstrap, Cron and Calendar fakes listing what a test arranges.

    ``edits`` records each change of an automation's texts: the owner, the job,
    action or event id, and the changed fields.
    """

    def __init__(self) -> None:
        self.bootstrap_jobs: list[Any] = []
        self.cron_jobs: list[Any] = []
        self.actions: list[dict[str, Any]] = []
        self.event_notes: str | None = None
        self.edits: list[tuple[str, str, dict[str, Any]]] = []
        self.bootstrap = SimpleNamespace(
            list_jobs=lambda: list(self.bootstrap_jobs),
            get_job=lambda job_id: _find(self.bootstrap_jobs, job_id),
            rename_prompt_skill=lambda job_id, prompt, actor: self.edits.append(
                ("bootstrap", job_id, {"prompt": prompt})
            ),
        )
        self.cron = SimpleNamespace(
            list_jobs=lambda: list(self.cron_jobs),
            get_job=lambda job_id: _find(self.cron_jobs, job_id),
            update_job=self._edit("cron"),
        )
        self.calendar = SimpleNamespace(
            actions=SimpleNamespace(
                list_actions=lambda event_id=None: [
                    action
                    for action in self.actions
                    if event_id is None or action["event_id"] == event_id
                ],
                # A test marks an action whose occurrences are used up "spent".
                can_fire=lambda action_id: (
                    not any(
                        action["id"] == action_id and action.get("spent") for action in self.actions
                    )
                ),
                update=self._edit("calendar-action"),
            ),
            list_events=lambda: [self._event()],
            get_event=lambda _event_id: self._event(),
            update_event=self._edit("calendar-event"),
        )

    def _event(self) -> Any:
        return SimpleNamespace(id="evt-1", title="Weekly review", notes=self.event_notes)

    def _edit(self, owner: str) -> Callable[..., Any]:
        async def edit(item_id: str, *, actor: str, **fields: Any) -> None:
            assert actor == "tool"
            self.edits.append((owner, item_id, fields))

        return edit

    def references(self) -> AutomationReferences:
        return AutomationReferences(
            bootstrap=cast(Any, self.bootstrap),
            cron=cast(Any, self.cron),
            calendar=cast(Any, self.calendar),
        )


def _find(jobs: list[Any], job_id: str) -> Any:
    return next(job for job in jobs if job.id == job_id)


def _job(**fields: Any) -> Any:
    """One job selecting builder/s1 unless ``fields`` say otherwise."""
    return SimpleNamespace(
        **{
            "id": "job-1",
            "name": "Daily report",
            "agent_id": "builder",
            "project_id": None,
            "session_id": "s1",
            "status": "active",
            "prompt": "Report.",
            **fields,
        }
    )


def _bootstrap(**fields: Any) -> Callable[[_Automations], None]:
    return lambda automations: automations.bootstrap_jobs.append(_job(**fields))


def _cron(**fields: Any) -> Callable[[_Automations], None]:
    return lambda automations: automations.cron_jobs.append(_job(**fields))


def _calendar(**fields: Any) -> Callable[[_Automations], None]:
    action = {
        "id": "act-1",
        "event_id": "evt-1",
        "target": "builder",
        "session": "s1",
        "prompt": "Prepare.",
        **fields,
    }
    return lambda automations: automations.actions.append(action)


@pytest.mark.parametrize(
    ("arrange", "reference"),
    [
        pytest.param(
            _bootstrap(id="boot-1", name="Warm up"),
            AutomationReference("bootstrap", "boot-1", "Warm up"),
            id="bootstrap",
        ),
        # A paused or failed job can be enabled again, so its Session still counts.
        pytest.param(
            _cron(id="cron-1", status="failed"),
            AutomationReference("cron", "cron-1", "Daily report"),
            id="cron-failed",
        ),
        pytest.param(
            _cron(id="cron-1", status="paused"),
            AutomationReference("cron", "cron-1", "Daily report"),
            id="cron-paused",
        ),
        # An action is named by its event's title.
        pytest.param(
            _calendar(),
            AutomationReference("calendar", "act-1", "Weekly review"),
            id="calendar",
        ),
    ],
)
def test_session_references_name_each_live_automation_in_the_session(
    arrange: Callable[[_Automations], None], reference: AutomationReference
) -> None:
    automations = _Automations()
    arrange(automations)

    assert automations.references().session_references(_SESSION) == (reference,)


@pytest.mark.parametrize(
    "arrange",
    [
        # Terminal history never starts another Run.
        pytest.param(_cron(status="completed"), id="cron-completed"),
        pytest.param(_cron(status="missed"), id="cron-missed"),
        pytest.param(_bootstrap(status="completed"), id="bootstrap-completed"),
        # The same Session id under another Agent address is another Session.
        pytest.param(_cron(project_id="vbot"), id="cron-other-scope"),
        pytest.param(_calendar(target="builder@vbot"), id="calendar-other-scope"),
        # A fresh Session per start selects none.
        pytest.param(_calendar(session=None), id="calendar-fresh-session"),
        # An action that can no longer fire, for example of a past one-time event.
        pytest.param(_calendar(spent=True), id="calendar-used-up"),
    ],
)
def test_session_references_ignore_automations_that_cannot_start_in_the_session(
    arrange: Callable[[_Automations], None],
) -> None:
    automations = _Automations()
    arrange(automations)

    assert automations.references().session_references(_SESSION) == ()


def _agent(references: AutomationReferences) -> tuple[AutomationReference, ...]:
    return references.agent_references("builder")


def _project(references: AutomationReferences) -> tuple[AutomationReference, ...]:
    return references.project_references("vbot")


@pytest.mark.parametrize(
    ("query", "arrange", "labels"),
    [
        # Any Session, or a fresh one per start, of the Identity Agent counts.
        pytest.param(_agent, _cron(session_id=None), ["cron:job-1"], id="agent-cron"),
        pytest.param(_agent, _calendar(), ["calendar:act-1"], id="agent-calendar"),
        # A Project target names that Project's Agent, even with the same id.
        pytest.param(_agent, _bootstrap(project_id="vbot"), [], id="agent-not-project-agent"),
        pytest.param(_agent, _calendar(spent=True), [], id="agent-not-used-up-action"),
        pytest.param(_project, _bootstrap(project_id="vbot"), ["bootstrap:job-1"], id="project"),
        pytest.param(
            _project,
            _calendar(target="builder@vbot", session=None),
            ["calendar:act-1"],
            id="project-calendar",
        ),
        pytest.param(_project, _cron(), [], id="project-not-identity-agent"),
        pytest.param(_project, _cron(project_id="other"), [], id="project-not-other-project"),
        pytest.param(
            _project, _cron(project_id="vbot", status="missed"), [], id="project-not-history"
        ),
    ],
)
def test_agent_and_project_references_name_the_live_automations_of_their_target(
    query: Callable[[AutomationReferences], tuple[AutomationReference, ...]],
    arrange: Callable[[_Automations], None],
    labels: list[str],
) -> None:
    automations = _Automations()
    arrange(automations)

    assert [reference.label for reference in query(automations.references())] == labels


def test_agent_triggered_skill_names_read_every_live_text_of_the_identity_agent() -> None:
    automations = _Automations()
    automations.event_notes = "Bring $agenda."
    for arrange in (
        _cron(prompt="/deploy the release"),
        _bootstrap(id="boot-1", prompt="Warm up with $warmup, then $deploy."),
        _calendar(prompt="Use /triage only when asked."),
        # Terminal history triggers nothing here, and neither does a job of the
        # Project Agent with the same id: that Config Agent never loads the
        # Identity Agent's own Skills.
        _cron(id="cron-2", prompt="$retired", status="completed"),
        _cron(id="cron-3", prompt="$project-only", project_id="vbot"),
    ):
        arrange(automations)

    # ``/name`` counts only at the start of a text; ``$name`` anywhere.
    assert automations.references().agent_triggered_skill_names("builder") == {
        "deploy",
        "warmup",
        "agenda",
    }


# A Calendar event's title and notes reach the Runs of every action of the event,
# so they follow only while all of them start Runs of the merging Agent.
@pytest.mark.parametrize("shared_event", [False, True], ids=["own-event", "shared-event"])
def test_a_skill_merge_renames_the_triggers_in_the_identity_agents_automations(
    shared_event: bool,
) -> None:
    automations = _Automations()
    automations.event_notes = "Bring $deploy-web and $agenda."
    for arrange in (
        # Only a leading /name triggers, and $name only with the whole name.
        _cron(prompt="/deploy-web the release, not /deploy-web or $deploy-webhook."),
        _bootstrap(id="boot-1", prompt="Warm up with $deploy-web."),
        _calendar(prompt="Use $deploy-web."),
        _cron(id="cron-2", prompt="$deploy-web", status="completed"),
        _cron(id="cron-3", prompt="$deploy-web", project_id="vbot"),
        _cron(id="cron-4", prompt="/deploy the release."),
    ):
        arrange(automations)
    if shared_event:
        _calendar(id="act-2", target="writer", prompt="Prepare.")(automations)
    references = automations.references()

    found = references.agent_skill_triggers("builder", "deploy-web")
    for reference in found:
        asyncio.run(references.rename_skill_triggers("builder", reference, "deploy-web", "deploy"))

    assert [reference.label for reference in found] == [
        "bootstrap:boot-1",
        "calendar:act-1",
        "cron:job-1",
    ]
    event_edit = ("calendar-event", "evt-1", {"notes": "Bring $deploy and $agenda."})
    assert automations.edits == [
        ("bootstrap", "boot-1", {"prompt": "Warm up with $deploy."}),
        ("calendar-action", "act-1", {"prompt": "Use $deploy."}),
        *([] if shared_event else [event_edit]),
        ("cron", "job-1", {"prompt": "/deploy the release, not /deploy-web or $deploy-webhook."}),
    ]
