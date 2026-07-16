"""Tests for Automation-owned ``cron/jobs.json`` validation."""

from __future__ import annotations

from core.automation import validate_cron_jobs_data

_AGENT_ID_SLUG_ERROR = "must be 1-64 characters using only letters, numbers, hyphen, or underscore"


def test_validate_cron_jobs_data_rejects_path_traversal_agent_id() -> None:
    diagnostics = validate_cron_jobs_data(
        [{"id": "job-1", "agent_id": "../escape", "prompt": "do it"}]
    )

    assert ("error", "$[0].agent_id", _AGENT_ID_SLUG_ERROR) in [
        (diagnostic.severity, diagnostic.path, diagnostic.message) for diagnostic in diagnostics
    ]
