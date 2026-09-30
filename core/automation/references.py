"""Which automations start their Runs in a Session, and the lock that keeps that answer true.

A Bootstrap job, a Cron job and a Calendar action can each select one existing
Session: every Run it starts goes into exactly that Session, addressed by its
Agent and Session id. Deleting or moving such a Session would make each later
start fail, so whoever removes or moves a Session asks this owner first, and
every such check and every edit that selects a Session holds
:attr:`AutomationReferences.lock`.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from core.automation.bootstrap import TERMINAL_BOOTSTRAP_STATUSES
from core.automation.cron import TERMINAL_CRON_JOB_STATUSES
from core.projects.address import parse_agent_address

if TYPE_CHECKING:
    from core.automation.bootstrap import BootstrapService
    from core.automation.cron import CronService
    from core.calendar import CalendarService
    from core.sessions import SessionAddress

AutomationKind = Literal["bootstrap", "cron", "calendar"]


@dataclass(frozen=True, slots=True)
class AutomationReference:
    """One automation that starts its Runs in a given Session.

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


class AutomationReferences:
    """Answer which live automations select a Session, for its removers and movers.

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

    def session_references(self, address: SessionAddress) -> tuple[AutomationReference, ...]:
        """Return the live automations that start their Runs in ``address``, sorted by label."""
        owner = (address.agent_id, address.project_id)
        references = [
            AutomationReference("bootstrap", job.id, job.name)
            for job in self._bootstrap.list_jobs()
            if (job.agent_id, job.project_id) == owner
            and job.session_id == address.session_id
            and job.status not in TERMINAL_BOOTSTRAP_STATUSES
        ]
        references.extend(
            AutomationReference("cron", job.id, job.name)
            for job in self._cron.list_jobs()
            if (job.agent_id, job.project_id) == owner
            and job.session_id == address.session_id
            and job.status not in TERMINAL_CRON_JOB_STATUSES
        )
        actions = [
            action
            for action in self._calendar.actions.list_actions()
            if action.get("session") == address.session_id
            and parse_agent_address(action["target"]) == owner
            and self._calendar.actions.can_fire(action["id"])
        ]
        if actions:
            titles = {event.id: event.title for event in self._calendar.list_events()}
            references.extend(
                AutomationReference(
                    "calendar", action["id"], titles.get(action["event_id"], action["event_id"])
                )
                for action in actions
            )
        return tuple(sorted(references, key=lambda reference: reference.label))


__all__ = ["AutomationKind", "AutomationReference", "AutomationReferences"]
