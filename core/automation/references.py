"""Which automations start Runs of an Agent, Project or Session, and the lock that keeps it true.

A Bootstrap job, a Cron job and a Calendar action each target one Agent, either
an Identity Agent or an Agent of a Project, and can select one existing Session
of it: every Run it starts goes there. Deleting an Agent, removing a Project or
deleting or moving such a Session would make each later start fail, so whoever
does that asks this owner first, and every such check and every edit that
chooses a target holds :attr:`AutomationReferences.lock`.

The texts an automation's Runs receive can trigger the Identity Agent's Skills
by name; when a Skill is merged into another one, this owner also moves those
triggers to the other Skill.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any, Literal

from core.automation.bootstrap import TERMINAL_BOOTSTRAP_STATUSES
from core.automation.cron import TERMINAL_CRON_JOB_STATUSES
from core.projects.address import parse_agent_address
from core.skills import rename_skill_triggers, triggered_skill_names

if TYPE_CHECKING:
    from core.automation.bootstrap import BootstrapService
    from core.automation.cron import CronService
    from core.calendar import CalendarService
    from core.sessions import SessionAddress

AutomationKind = Literal["bootstrap", "cron", "calendar"]

# Whether a target (Agent id, Project id or None, selected Session id or None) counts.
_Selector = Callable[[str, str | None, str | None], bool]
# Who changes an automation's texts when a Skill merge moves its triggers.
_MERGE_ACTOR = "tool"


@dataclass(frozen=True, slots=True)
class AutomationReference:
    """One automation that starts Runs of a given Agent, Project or Session.

    ``name`` is how the user knows it: the job name, or the title of the event a
    Calendar action belongs to.
    """

    kind: AutomationKind
    id: str
    name: str

    @property
    def label(self) -> str:
        """The ``kind:id`` form that names the reference in logs and error messages."""
        return f"{self.kind}:{self.id}"

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "id": self.id, "name": self.name}


@dataclass(frozen=True, slots=True)
class _LiveAutomation:
    """One live automation; ``texts`` returns the texts its Runs receive.

    The texts are read only when asked for: a reference check needs the target alone.
    """

    reference: AutomationReference
    texts: Callable[[], tuple[str, ...]]


def _job_texts(job: Any) -> tuple[str, ...]:
    return (job.prompt,)


def _action_texts(action: dict[str, Any], event: Any | None) -> tuple[str, ...]:
    texts = [action["prompt"]]
    if event is not None:
        texts.extend(text for text in (event.title, event.notes) if text)
    return tuple(texts)


class AutomationReferences:
    """Answer which live automations start Runs of an Agent, Project or Session.

    Terminal history never starts another Run and does not count: completed
    Bootstrap jobs, completed or missed Cron jobs, and Calendar actions that can
    no longer fire (``CalendarActions.can_fire``, for example after a one-time
    event has passed). A paused or failed Cron job can be enabled again, so it
    counts.

    ``lock`` serializes reference checks with the edits that create, move or
    remove references, across RPC handlers, Tools and commands. Hold it from the
    check through the change it guards; never while waiting for a Run.
    """

    def __init__(
        self,
        *,
        bootstrap: BootstrapService,
        cron: CronService,
        calendar: CalendarService,
    ) -> None:
        self._bootstrap = bootstrap
        self._cron = cron
        self._calendar = calendar
        self.lock = asyncio.Lock()

    def agent_references(self, agent_id: str) -> tuple[AutomationReference, ...]:
        """Return the live automations that start Runs of the Identity Agent ``agent_id``.

        Only a target without a Project names the Identity Agent; a target such as
        ``builder@vbot`` names that Project's Agent, even when the ids match.
        """
        return self._live(lambda agent, project, _session: (agent, project) == (agent_id, None))

    def project_references(self, project_id: str) -> tuple[AutomationReference, ...]:
        """Return the live automations that start Runs of any Agent of ``project_id``."""
        return self._live(lambda _agent, project, _session: project == project_id)

    def session_references(self, address: SessionAddress) -> tuple[AutomationReference, ...]:
        """Return the live automations that start their Runs in ``address``."""
        owner = (address.agent_id, address.project_id, address.session_id)
        return self._live(lambda agent, project, session: (agent, project, session) == owner)

    def agent_triggered_skill_names(self, agent_id: str) -> frozenset[str]:
        """Return the Skill names the live automations of the Identity Agent ``agent_id`` trigger.

        Each text an automation's Runs receive counts as one message, in which a
        leading ``/name`` or any ``$name`` triggers the Skill ``name``: a
        Bootstrap or Cron job's prompt, and a Calendar action's prompt plus the
        title and notes of its event.
        """
        automations = self._live_automations(_identity_agent(agent_id))
        return frozenset(
            name
            for automation in automations
            for text in automation.texts()
            for name in triggered_skill_names(text)
        )

    def agent_skill_triggers(self, agent_id: str, name: str) -> tuple[AutomationReference, ...]:
        """Return the live automations of the Identity Agent ``agent_id`` that trigger ``name``.

        They read the texts :meth:`agent_triggered_skill_names` reads; sorted by label.
        """
        references = [
            automation.reference
            for automation in self._live_automations(_identity_agent(agent_id))
            if any(name in triggered_skill_names(text) for text in automation.texts())
        ]
        return tuple(sorted(references, key=lambda reference: reference.label))

    async def rename_skill_triggers(
        self, agent_id: str, reference: AutomationReference, name: str, new_name: str
    ) -> None:
        """Make one automation of the Identity Agent ``agent_id`` trigger ``new_name`` for ``name``.

        For a Skill merged into another one; the caller holds :attr:`lock`. A
        Bootstrap job keeps its status and arming. A Calendar action's prompt
        changes, and its event's title and notes change while every action of the
        event starts Runs of this Agent: they reach the Runs of each action.
        Raises the error of the automation's owner when it cannot change.
        """
        if reference.kind == "bootstrap":
            job = self._bootstrap.get_job(reference.id)
            self._bootstrap.rename_prompt_skill(
                job.id, rename_skill_triggers(job.prompt, name, new_name), actor=_MERGE_ACTOR
            )
            return
        if reference.kind == "cron":
            cron_job = self._cron.get_job(reference.id)
            prompt = rename_skill_triggers(cron_job.prompt, name, new_name)
            if prompt != cron_job.prompt:
                await self._cron.update_job(cron_job.id, actor=_MERGE_ACTOR, prompt=prompt)
            return
        actions = self._calendar.actions
        action = next((item for item in actions.list_actions() if item["id"] == reference.id), None)
        if action is None:
            raise LookupError(f"Calendar action not found: {reference.id}")
        prompt = rename_skill_triggers(action["prompt"], name, new_name)
        if prompt != action["prompt"]:
            await actions.update(action["id"], actor=_MERGE_ACTOR, prompt=prompt)
        if any(
            parse_agent_address(item["target"]) != (agent_id, None)
            for item in actions.list_actions(action["event_id"])
        ):
            return
        event = self._calendar.get_event(action["event_id"])
        fields = {
            field: rename_skill_triggers(text, name, new_name)
            for field, text in (("title", event.title), ("notes", event.notes))
            if text and rename_skill_triggers(text, name, new_name) != text
        }
        if fields:
            await self._calendar.update_event(event.id, actor=_MERGE_ACTOR, **fields)

    def _live(self, selects: _Selector) -> tuple[AutomationReference, ...]:
        """Return the live automations whose target ``selects`` accepts, sorted by label."""
        references = [automation.reference for automation in self._live_automations(selects)]
        return tuple(sorted(references, key=lambda reference: reference.label))

    def _live_automations(self, selects: _Selector) -> list[_LiveAutomation]:
        """Return the live automations whose target ``selects`` accepts."""
        automations = [
            _LiveAutomation(
                AutomationReference("bootstrap", job.id, job.name), partial(_job_texts, job)
            )
            for job in self._bootstrap.list_jobs()
            if selects(job.agent_id, job.project_id, job.session_id)
            and job.status not in TERMINAL_BOOTSTRAP_STATUSES
        ]
        automations.extend(
            _LiveAutomation(AutomationReference("cron", job.id, job.name), partial(_job_texts, job))
            for job in self._cron.list_jobs()
            if selects(job.agent_id, job.project_id, job.session_id)
            and job.status not in TERMINAL_CRON_JOB_STATUSES
        )
        actions = [
            action
            for action in self._calendar.actions.list_actions()
            if selects(*parse_agent_address(action["target"]), action.get("session"))
            and self._calendar.actions.can_fire(action["id"])
        ]
        if actions:
            events = {event.id: event for event in self._calendar.list_events()}
            for action in actions:
                event = events.get(action["event_id"])
                name = event.title if event is not None else action["event_id"]
                automations.append(
                    _LiveAutomation(
                        AutomationReference("calendar", action["id"], name),
                        partial(_action_texts, action, event),
                    )
                )
        return automations


def _identity_agent(agent_id: str) -> _Selector:
    """Select the targets that name the Identity Agent ``agent_id`` (no Project)."""
    return lambda agent, project, _session: (agent, project) == (agent_id, None)


__all__ = ["AutomationKind", "AutomationReference", "AutomationReferences"]
