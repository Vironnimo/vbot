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
    """Bootstrap and Cron fakes listing what a test arranges.

    ``edits`` records each change of an automation's texts: the owner, the job
    id, and the changed fields.
    """

    def __init__(self) -> None:
        self.bootstrap_jobs: list[Any] = []
        self.cron_jobs: list[Any] = []
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

    def _edit(self, owner: str) -> Callable[..., Any]:
        async def edit(item_id: str, *, actor: str, **fields: Any) -> None:
            assert actor == "tool"
            self.edits.append((owner, item_id, fields))

        return edit

    def references(self) -> AutomationReferences:
        return AutomationReferences(bootstrap=cast(Any, self.bootstrap), cron=cast(Any, self.cron))


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
        # A fresh Session per start selects none.
        pytest.param(_cron(session_id=None), id="cron-fresh-session"),
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
        # A Project target names that Project's Agent, even with the same id.
        pytest.param(_agent, _bootstrap(project_id="vbot"), [], id="agent-not-project-agent"),
        pytest.param(_project, _bootstrap(project_id="vbot"), ["bootstrap:job-1"], id="project"),
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
    for arrange in (
        _cron(prompt="/deploy the release"),
        _bootstrap(id="boot-1", prompt="Warm up with $warmup, then $deploy."),
        _cron(id="cron-4", prompt="Use /triage only when asked."),
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
    }


def test_a_skill_merge_renames_the_triggers_in_the_identity_agents_automations() -> None:
    automations = _Automations()
    for arrange in (
        # Only a leading /name triggers, and $name only with the whole name.
        _cron(prompt="/deploy-web the release, not /deploy-web or $deploy-webhook."),
        _bootstrap(id="boot-1", prompt="Warm up with $deploy-web."),
        _cron(id="cron-2", prompt="$deploy-web", status="completed"),
        _cron(id="cron-3", prompt="$deploy-web", project_id="vbot"),
        _cron(id="cron-4", prompt="/deploy the release."),
    ):
        arrange(automations)
    references = automations.references()

    found = references.agent_skill_triggers("builder", "deploy-web")
    for reference in found:
        asyncio.run(references.rename_skill_triggers("builder", reference, "deploy-web", "deploy"))

    assert [reference.label for reference in found] == ["bootstrap:boot-1", "cron:job-1"]
    assert automations.edits == [
        ("bootstrap", "boot-1", {"prompt": "Warm up with $deploy."}),
        ("cron", "job-1", {"prompt": "/deploy the release, not /deploy-web or $deploy-webhook."}),
    ]
