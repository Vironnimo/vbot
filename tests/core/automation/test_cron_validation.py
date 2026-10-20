"""Tests for Automation-owned ``cron/jobs.json`` validation."""

from __future__ import annotations

from core.automation import validate_cron_jobs_data

_AGENT_ID_SLUG_ERROR = "must be 1-64 characters using only letters, numbers, hyphen, or underscore"


def _diagnostics(data: object) -> list[tuple[str, str, str]]:
    return [
        (diagnostic.severity, diagnostic.path, diagnostic.message)
        for diagnostic in validate_cron_jobs_data(data)
    ]


def test_validate_cron_jobs_data_rejects_path_traversal_agent_id() -> None:
    diagnostics = _diagnostics(
        {
            "format_version": 1,
            "jobs": [{"id": "job-1", "agent_id": "../escape", "name": "x", "prompt": "do it"}],
        }
    )

    assert ("error", "$.jobs[0].agent_id", _AGENT_ID_SLUG_ERROR) in diagnostics


def test_validate_cron_jobs_data_accepts_missing_defaulted_metadata() -> None:
    diagnostics = _diagnostics(
        {
            "format_version": 1,
            "jobs": [
                {
                    "id": "job-1",
                    "agent_id": "main",
                    "name": "Daily",
                    "prompt": "do it",
                    "schedule_type": "cron",
                    "cron_expression": "0 9 * * *",
                }
            ],
        }
    )

    assert diagnostics == []


def test_validate_cron_jobs_data_checks_delivery_and_last_answer() -> None:
    base = {
        "id": "job-1",
        "agent_id": "main",
        "name": "Daily",
        "prompt": "do it",
        "schedule_type": "cron",
        "cron_expression": "0 9 * * *",
    }
    diagnostics = _diagnostics(
        {
            "format_version": 1,
            "jobs": [
                {**base, "delivery": {"kind": "chat", "channel_id": "tg-main"}},
                {**base, "id": "job-2", "delivery": {"kind": "app", "sound": True}},
                {**base, "id": "job-3", "last_result": {"text": "x" * 2001, "at": "now"}},
                {**base, "id": "job-4", "last_run_silent": "yes"},
            ],
        }
    )

    assert diagnostics == [
        ("error", "$.jobs[0].delivery", "delivery.platform_target must be a non-empty string"),
        ("warning", "$.jobs[1].delivery.sound", "unknown delivery field: sound"),
        (
            "error",
            "$.jobs[2].last_result",
            "last_result.text must be a non-empty string of at most 2000 characters",
        ),
        ("error", "$.jobs[3].last_run_silent", "must be a boolean"),
    ]


def test_validate_cron_jobs_data_requires_the_versioned_document() -> None:
    assert [path for _, path, _ in _diagnostics([])] == ["$"]
    assert _diagnostics({"jobs": []})[0][1] == "$.format_version"
    assert _diagnostics({"format_version": 1}) == [("error", "$.jobs", "is required")]


def test_validate_cron_jobs_data_requires_a_name_and_warns_on_unknown_fields() -> None:
    diagnostics = _diagnostics(
        {
            "format_version": 1,
            "future": True,
            "jobs": [
                {
                    "id": "job-1",
                    "agent_id": "main",
                    "prompt": "do it",
                    "schedule_type": "cron",
                    "timezone": "Europe/Paris",
                }
            ],
        }
    )

    assert diagnostics == [
        ("warning", "$.future", "unknown cron jobs field: future"),
        ("warning", "$.jobs[0].timezone", "unknown cron job field: timezone"),
        ("error", "$.jobs[0].name", "is required"),
    ]
