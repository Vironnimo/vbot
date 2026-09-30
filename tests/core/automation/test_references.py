"""AutomationReferences: which live automations start their Runs in a Session."""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any, cast

import pytest

from core.automation import AutomationReference, AutomationReferences
from core.sessions import SessionAddress

_SESSION = SessionAddress(project_id=None, agent_id="builder", session_id="s1")


class _Automations:
    """Bootstrap, Cron and Calendar fakes listing what a test arranges."""

    def __init__(self) -> None:
        self.bootstrap_jobs: list[Any] = []
        self.cron_jobs: list[Any] = []
        self.actions: list[dict[str, Any]] = []
        self.bootstrap = SimpleNamespace(list_jobs=lambda: list(self.bootstrap_jobs))
        self.cron = SimpleNamespace(list_jobs=lambda: list(self.cron_jobs))
        self.calendar = SimpleNamespace(
            actions=SimpleNamespace(list_actions=lambda: list(self.actions)),
            list_events=lambda: [SimpleNamespace(id="evt-1", title="Weekly review")],
        )

    def references(self) -> AutomationReferences:
        return AutomationReferences(
            bootstrap=cast(Any, self.bootstrap),
            cron=cast(Any, self.cron),
            calendar=cast(Any, self.calendar),
        )


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
            **fields,
        }
    )


def _bootstrap(**fields: Any) -> Callable[[_Automations], None]:
    return lambda automations: automations.bootstrap_jobs.append(_job(**fields))


def _cron(**fields: Any) -> Callable[[_Automations], None]:
    return lambda automations: automations.cron_jobs.append(_job(**fields))


def _calendar(**fields: Any) -> Callable[[_Automations], None]:
    action = {"id": "act-1", "event_id": "evt-1", "target": "builder", "session": "s1", **fields}
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
    ],
)
def test_session_references_ignore_automations_that_cannot_start_in_the_session(
    arrange: Callable[[_Automations], None],
) -> None:
    automations = _Automations()
    arrange(automations)

    assert automations.references().session_references(_SESSION) == ()
