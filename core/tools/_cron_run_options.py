"""Flat cron Tool arguments for an automation's execution settings."""

from __future__ import annotations

from typing import Any

from core.automation.cron import CronJob, cron_run_overrides
from core.sessions import AGENT_DEFAULT_PROJECT

EXECUTION_FIELDS = ("working_project", "model", "thinking_effort")


def execution_changes(arguments: dict[str, Any], *, creating: bool) -> dict[str, Any]:
    """Translate explicit Tool choices; the Cron owner validates the resulting target."""
    changes: dict[str, Any] = {}
    if "working_project" in arguments:
        project = arguments["working_project"].strip()
        changes["working_project_id"] = None if project.casefold() == "none" else project
    overrides: dict[str, Any] = {}
    if "model" in arguments:
        model = arguments["model"].strip()
        overrides["model"] = None if model.casefold() == "inherit" else model
    if "thinking_effort" in arguments:
        effort = arguments["thinking_effort"].strip().casefold()
        if effort == "default":
            overrides["thinking_effort_default"] = True
        else:
            overrides["thinking_effort"] = None if effort == "inherit" else effort
    if overrides:
        changes["run_overrides"] = cron_run_overrides(overrides) if creating else overrides
    return changes


def execution_fields(job: CronJob) -> dict[str, Any]:
    """Expose saved choices so editing one setting does not need another read."""
    fields: dict[str, Any] = {}
    if job.working_project_id is not AGENT_DEFAULT_PROJECT:
        fields["working_project"] = job.working_project_id or "none"
    overrides = job.run_overrides
    if overrides is None:
        return fields
    if overrides.model is not None:
        fields["model"] = overrides.model
    if overrides.thinking_effort_default:
        fields["thinking_effort"] = "default"
    elif overrides.thinking_effort is not None:
        fields["thinking_effort"] = overrides.thinking_effort
    return fields
