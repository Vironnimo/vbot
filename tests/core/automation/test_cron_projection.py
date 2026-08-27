"""Tests for the read-only cron schedule projection."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import Mock

import pytest

from core.automation.cron import CronJobValidationError, CronService


@pytest.fixture()
def service(tmp_path: Any) -> CronService:
    return CronService(Mock(), tmp_path)


class TestProjectOccurrences:
    def test_once_job_projects_within_window(self, service: CronService) -> None:
        service.create_job(
            agent_id="joel",
            prompt="one-shot",
            schedule_type="once",
            run_at="2026-09-10T12:00:00+00:00",
            remaining_runs=1,
        )
        occurrences = service.project_occurrences(
            datetime(2026, 9, 10, tzinfo=UTC), datetime(2026, 9, 11, tzinfo=UTC)
        )
        assert [occurrence.fire_at_utc for occurrence in occurrences] == [
            datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
        ]

    def test_once_job_outside_window_is_absent(self, service: CronService) -> None:
        service.create_job(
            agent_id="joel",
            prompt="one-shot",
            schedule_type="once",
            run_at="2026-09-10T12:00:00+00:00",
            remaining_runs=1,
        )
        occurrences = service.project_occurrences(
            datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 9, 2, tzinfo=UTC)
        )
        assert occurrences == []

    def test_cron_expression_projects_local_schedule(self, service: CronService) -> None:
        service.create_job(
            agent_id="joel",
            prompt="check mail",
            schedule_type="cron",
            cron_expression="0 9 * * *",
        )
        occurrences = service.project_occurrences(
            datetime(2026, 9, 2, tzinfo=UTC), datetime(2026, 9, 3, tzinfo=UTC)
        )
        assert len(occurrences) == 1
        assert occurrences[0].schedule_type == "cron"
        assert occurrences[0].fire_at_utc.hour in (7, 8)  # 09:00 server-local

    def test_interval_projects_from_anchor(self, service: CronService) -> None:
        service.create_job(
            agent_id="joel",
            prompt="interval job",
            schedule_type="interval",
            interval_seconds=3600,
            interval_anchor_at="2026-09-01T00:00:00+00:00",
        )
        occurrences = service.project_occurrences(
            datetime(2026, 9, 2, 0, 0, tzinfo=UTC), datetime(2026, 9, 2, 3, 0, tzinfo=UTC)
        )
        assert [occurrence.fire_at_utc for occurrence in occurrences] == [
            datetime(2026, 9, 2, 0, 0, tzinfo=UTC),
            datetime(2026, 9, 2, 1, 0, tzinfo=UTC),
            datetime(2026, 9, 2, 2, 0, tzinfo=UTC),
        ]

    def test_paused_and_terminal_jobs_do_not_project(self, service: CronService) -> None:
        service.create_job(
            agent_id="joel",
            prompt="paused",
            schedule_type="cron",
            cron_expression="0 9 * * *",
            status="paused",
        )
        service.create_job(
            agent_id="joel",
            prompt="exhausted",
            schedule_type="cron",
            cron_expression="0 10 * * *",
            remaining_runs=0,
        )
        assert (
            service.project_occurrences(
                datetime(2026, 9, 2, tzinfo=UTC), datetime(2026, 9, 3, tzinfo=UTC)
            )
            == []
        )

    def test_future_ticks_respect_remaining_runs(self, service: CronService) -> None:
        service.create_job(
            agent_id="joel",
            prompt="limited",
            schedule_type="interval",
            interval_seconds=3600,
            interval_anchor_at="2026-09-01T00:00:00+00:00",
            remaining_runs=2,
        )
        occurrences = service.project_occurrences(
            datetime(2026, 9, 10, tzinfo=UTC), datetime(2026, 9, 11, tzinfo=UTC)
        )
        assert len(occurrences) == 2

    def test_projection_is_sorted_and_sorted_by_fire_time(self, service: CronService) -> None:
        service.create_job(
            agent_id="joel",
            prompt="late",
            schedule_type="once",
            run_at="2026-09-02T18:00:00+00:00",
            remaining_runs=1,
        )
        service.create_job(
            agent_id="joel",
            prompt="early",
            schedule_type="once",
            run_at="2026-09-02T06:00:00+00:00",
            remaining_runs=1,
        )
        occurrences = service.project_occurrences(
            datetime(2026, 9, 2, tzinfo=UTC), datetime(2026, 9, 3, tzinfo=UTC)
        )
        fire_times = [occurrence.fire_at_utc for occurrence in occurrences]
        assert fire_times == sorted(fire_times)
        assert [occurrence.name for occurrence in occurrences] == ["early", "late"]

    def test_caps_occurrences_per_job(self, service: CronService) -> None:
        service.create_job(
            agent_id="joel",
            prompt="every minute",
            schedule_type="interval",
            interval_seconds=60,
            interval_anchor_at="2026-09-01T00:00:00+00:00",
        )
        occurrences = service.project_occurrences(
            datetime(2026, 9, 2, tzinfo=UTC), datetime(2026, 9, 3, tzinfo=UTC)
        )
        assert len(occurrences) == 500

    def test_rejects_inverted_window(self, service: CronService) -> None:
        with pytest.raises(CronJobValidationError):
            service.project_occurrences(
                datetime(2026, 9, 2, tzinfo=UTC), datetime(2026, 9, 1, tzinfo=UTC)
            )
