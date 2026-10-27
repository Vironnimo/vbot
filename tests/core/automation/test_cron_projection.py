"""Tests for the read-only cron schedule projection."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest

from core.automation.cron import CronJobValidationError, CronService


@pytest.fixture()
def service(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> CronService:
    monkeypatch.setattr(
        "core.automation._cron_timing._utc_now", lambda: datetime(2026, 9, 1, tzinfo=UTC)
    )
    return CronService(Mock(), tmp_path, tz="UTC")


class TestProjectOccurrences:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("zone", "expression", "reference", "expected"),
        [
            (
                "Europe/Berlin",
                "30 2 * * *",
                "2027-03-28T03:10:00+02:00",
                "2027-03-28T03:30:00+02:00",
            ),
            (
                "Europe/Berlin",
                "30 2 * * *",
                "2027-03-28T03:30:00+02:00",
                "2027-03-29T02:30:00+02:00",
            ),
            (
                "Europe/Berlin",
                "15,30 2,3 * * *",
                "2027-03-28T03:10:00+02:00",
                "2027-03-28T03:15:00+02:00",
            ),
            (
                "Australia/Lord_Howe",
                "15 2 * * *",
                "2027-10-03T02:40:00+11:00",
                "2027-10-03T02:45:00+11:00",
            ),
            (
                "Pacific/Apia",
                "0 12 * * *",
                "2011-12-31T00:10:00+14:00",
                "2011-12-31T12:00:00+14:00",
            ),
        ],
    )
    async def test_a_shifted_gap_fire_is_still_upcoming_after_the_clock_jumped(
        self, service, zone, expression, reference, expected
    ):
        service.set_timezone(zone)
        job = await service.create_job(
            agent_id="joel", prompt="test", schedule_type="cron", cron_expression=expression
        )
        start = datetime.fromisoformat(reference).astimezone(UTC)
        expected_utc = datetime.fromisoformat(expected).astimezone(UTC)
        assert (
            datetime.fromisoformat(service.next_fire_at(job, reference_time=start)) == expected_utc
        )
        # Projection includes its lower bound; exclude it here to match next_fire_at.
        rows = service.project_occurrences(
            start + timedelta(microseconds=1), expected_utc + timedelta(seconds=1)
        )
        assert rows[0].fire_at_utc == expected_utc

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("zone", "expression", "start", "end", "expected"),
        [
            (
                "Europe/Berlin",
                "*/30 * * * *",
                "2026-03-29T00:00:00+00:00",
                "2026-03-29T03:00:00+00:00",
                [
                    f"2026-03-29T{hour:02}:{minute:02}:00+00:00"
                    for hour in range(3)
                    for minute in (0, 30)
                ],
            ),
            (
                "Australia/Lord_Howe",
                "15,30 2 * * *",
                "2026-10-03T15:20:00+00:00",
                "2026-10-03T16:00:00+00:00",
                ["2026-10-03T15:30:00+00:00", "2026-10-03T15:45:00+00:00"],
            ),
            (
                "Pacific/Apia",
                "0 0,12 * * *",
                "2011-12-30T09:55:00+00:00",
                "2011-12-30T23:00:00+00:00",
                ["2011-12-30T10:00:00+00:00", "2011-12-30T22:00:00+00:00"],
            ),
        ],
    )
    async def test_spring_gap_projection_matches_successive_live_fires(
        self, service, zone, expression, start, end, expected
    ):
        service.set_timezone(zone)
        job = await service.create_job(
            agent_id="joel",
            prompt="half-hour",
            schedule_type="cron",
            cron_expression=expression,
        )
        start = datetime.fromisoformat(start)
        end = datetime.fromisoformat(end)
        projected = [row.fire_at_utc for row in service.project_occurrences(start, end)]
        assert projected == [datetime.fromisoformat(item) for item in expected]
        cursor = start - timedelta(microseconds=1)
        live = []
        for _ in expected:
            cursor = datetime.fromisoformat(service.next_fire_at(job, reference_time=cursor))
            live.append(cursor)
        assert live == projected

    @pytest.mark.asyncio
    @pytest.mark.parametrize("day", ["2026-09-10", "2026-03-29", "2026-10-25"])
    async def test_cron_midnight_includes_start_and_excludes_end(self, service, day):
        service.set_timezone("Europe/Berlin")
        await service.create_job(
            agent_id="joel",
            prompt="midnight",
            schedule_type="cron",
            cron_expression="0 0 * * *",
        )
        start = datetime.fromisoformat(day).replace(tzinfo=ZoneInfo("Europe/Berlin"))
        end = start + timedelta(days=1)
        rows = service.project_occurrences(start.astimezone(UTC), end.astimezone(UTC))
        assert [row.fire_at_utc for row in rows] == [start.astimezone(UTC)]
        assert (
            service.project_occurrences(
                start.astimezone(UTC) + timedelta(microseconds=1), end.astimezone(UTC)
            )
            == []
        )

    @pytest.mark.asyncio
    async def test_once_job_projects_within_window(self, service: CronService) -> None:
        await service.create_job(
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

    @pytest.mark.asyncio
    async def test_once_job_outside_window_is_absent(self, service: CronService) -> None:
        await service.create_job(
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

    @pytest.mark.asyncio
    async def test_cron_expression_projects_local_schedule(self, service: CronService) -> None:
        await service.create_job(
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
        system_timezone = ZoneInfo(service.system_timezone_name())
        assert occurrences[0].fire_at_utc.astimezone(system_timezone).replace(
            tzinfo=None
        ) == datetime(2026, 9, 2, 9, 0)  # 09:00 server-local

    @pytest.mark.asyncio
    async def test_interval_projects_from_anchor(self, service: CronService) -> None:
        await service.create_job(
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

    @pytest.mark.asyncio
    async def test_paused_and_terminal_jobs_do_not_project(self, service: CronService) -> None:
        await service.create_job(
            agent_id="joel",
            prompt="paused",
            schedule_type="cron",
            cron_expression="0 9 * * *",
            status="paused",
        )
        await service.create_job(
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

    @pytest.mark.asyncio
    async def test_future_ticks_respect_remaining_runs(self, service: CronService) -> None:
        await service.create_job(
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

    @pytest.mark.asyncio
    @pytest.mark.parametrize("elapsed_hours", [0, 12, 24])
    async def test_historical_ticks_do_not_consume_remaining_runs(
        self, service: CronService, monkeypatch: pytest.MonkeyPatch, elapsed_hours: int
    ) -> None:
        window_start = datetime(2026, 9, 10, tzinfo=UTC)
        now = window_start + timedelta(hours=elapsed_hours)
        monkeypatch.setattr("core.automation._cron_timing._utc_now", lambda: now)
        await service.create_job(
            agent_id="joel",
            prompt="limited",
            schedule_type="interval",
            interval_seconds=3600,
            interval_anchor_at="2026-09-01T00:00:00+00:00",
            remaining_runs=2,
        )

        occurrences = service.project_occurrences(window_start, window_start + timedelta(days=1))

        assert [occurrence.fire_at_utc for occurrence in occurrences] == [
            window_start + timedelta(hours=hour) for hour in range(min(elapsed_hours + 2, 24))
        ]

    @pytest.mark.asyncio
    async def test_projection_is_sorted_and_sorted_by_fire_time(self, service: CronService) -> None:
        await service.create_job(
            agent_id="joel",
            prompt="late",
            schedule_type="once",
            run_at="2026-09-02T18:00:00+00:00",
            remaining_runs=1,
        )
        await service.create_job(
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

    @pytest.mark.asyncio
    async def test_caps_occurrences_per_job(self, service: CronService) -> None:
        await service.create_job(
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
