"""Real dispatch reads ``cron`` schedules and time zones written in other shapes.

Schedule spellings of other harnesses, clock and epoch times, named time zones,
and event times written as reminder offsets reach exactly the fires the call
meant. Each repair is paired with a
nearby call that means something else and is refused, before any job changes,
with the corrected call.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from tests.core.tools.scheduling_tool_support import JOB_PROMPT, CronTool, clock_at, cron_tool


@pytest.fixture
def tool(tmp_path: Path) -> CronTool:
    return cron_tool(tmp_path)


# The days around the 2030 clock changes: North America on March 10 and November 3, Europe
# on March 31 and October 27. The autumn window spans a whole week, so every weekday is in it.
_CLOCK_CHANGE_WINDOWS = (
    (datetime(2030, 3, 9, tzinfo=UTC), datetime(2030, 3, 12, tzinfo=UTC)),
    (datetime(2030, 3, 30, tzinfo=UTC), datetime(2030, 4, 2, tzinfo=UTC)),
    (datetime(2030, 10, 26, tzinfo=UTC), datetime(2030, 11, 4, tzinfo=UTC)),
)


def fires(tool: CronTool) -> list[datetime]:
    """The instants the scheduler fires the Tool's jobs around the clock changes."""
    return [
        occurrence.fire_at_utc
        for start, end in _CLOCK_CHANGE_WINDOWS
        for occurrence in tool.service.project_occurrences(start, end, max_per_job=10_000)
    ]


def fires_in(tmp_path: Path, zone: str, schedule: str) -> list[datetime]:
    """The instants ``schedule`` means in ``zone``: the scheduler running in that zone."""
    reference = cron_tool(tmp_path, tz=zone)
    reference.created({"action": "create", "prompt": JOB_PROMPT, "schedule": schedule})
    return fires(reference)


# -- schedule spellings ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fields", "schedule_type", "stored"),
    [
        ({"schedule": "every 2 hours"}, "interval", 7200),
        ({"schedule": "every 90 minutes"}, "interval", 5400),
        ({"schedule": "every 1 week"}, "interval", 604800),
        ({"interval": "PT30M"}, "interval", 1800),
        ({"interval_seconds": 7200}, "interval", 7200),
        ({"everyMs": 3600000}, "interval", 3600),
        ({"every": "2h"}, "interval", 7200),
        ({"schedule": "30m", "schedule_type": "interval"}, "interval", 1800),
        ({"schedule": "@daily"}, "cron", "0 0 * * *"),
        ({"cron": "0 9 * * 1-5"}, "cron", "0 9 * * 1-5"),
        ({"cronExpression": "0 8 * * *"}, "cron", "0 8 * * *"),
        ({"schedule": {"kind": "cron", "expr": "0 8 * * *"}}, "cron", "0 8 * * *"),
        ({"schedule": {"kind": "every", "everyMs": 900000}}, "interval", 900),
    ],
)
def test_schedule_spellings_reach_the_schedule(
    tool: CronTool, fields: dict[str, Any], schedule_type: str, stored: object
) -> None:
    job, _text = tool.created({"action": "create", "prompt": JOB_PROMPT, **fields})

    assert job.schedule_type == schedule_type
    assert (job.interval_seconds if schedule_type == "interval" else job.cron_expression) == stored


@pytest.mark.parametrize(
    "fields",
    [
        {"schedule": "in 45 minutes"},
        {"schedule": "30m", "schedule_type": "once"},
        {"delay": "PT2H"},
        {"run_at": "2030-01-01T09:00:00Z", "schedule_type": "once"},
        {"fireAt": "2030-01-01T10:00:00+01:00"},
        {"at": 1893484800000},
        {"schedule": {"kind": "at", "at": "2030-01-01T09:00:00Z"}},
    ],
)
def test_one_time_spellings_create_one_fire(tool: CronTool, fields: dict[str, Any]) -> None:
    job, _text = tool.created({"action": "create", "prompt": JOB_PROMPT, **fields})

    assert (job.schedule_type, job.remaining_runs) == ("once", 1)


@pytest.mark.parametrize(
    ("fields", "run_at", "where"),
    [
        ({"run_at": "0900"}, "2030-01-10T08:00:00+00:00", "the server time zone Europe/Berlin"),
        ({"at": "9:00"}, "2030-01-10T08:00:00+00:00", "the server time zone Europe/Berlin"),
        # 07:00 has passed in Berlin today, so the next 07:00 is tomorrow's.
        ({"fireAt": "0700"}, "2030-01-11T06:00:00+00:00", "the server time zone Europe/Berlin"),
        (
            {"run_at": "08:30", "timezone": "America/New_York"},
            "2030-01-10T13:30:00+00:00",
            "America/New_York",
        ),
    ],
)
def test_clock_time_at_a_time_key_is_its_next_occurrence(
    tool: CronTool,
    monkeypatch: pytest.MonkeyPatch,
    fields: dict[str, Any],
    run_at: str,
    where: str,
) -> None:
    # 08:00 in Berlin, 02:00 in New York.
    monkeypatch.setattr("core.tools.cron.datetime", clock_at(datetime(2030, 1, 10, 7, tzinfo=UTC)))

    job, text = tool.created({"action": "create", "prompt": JOB_PROMPT, **fields})

    assert job.run_at == run_at
    zone = ZoneInfo(tool.service.system_timezone_name())
    shown = datetime.fromisoformat(run_at).astimezone(zone).isoformat()
    assert f"as its next occurrence in {where}: {shown}." in text


def test_clock_time_the_clocks_skip_is_refused_with_each_instant(
    tool: CronTool, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 01:00 in Berlin on the night the clocks jump from 02:00 to 03:00.
    monkeypatch.setattr("core.tools.cron.datetime", clock_at(datetime(2030, 3, 31, 0, tzinfo=UTC)))

    message = tool.refused({"action": "create", "prompt": JOB_PROMPT, "run_at": "02:30"})

    assert message.startswith(
        'cron was not run: 02:30 next comes on 2030-03-31, and "2030-03-31T02:30" does not '
        "exist in the server time zone Europe/Berlin: the clocks jump over it that day."
    )
    assert '"schedule":"2030-03-31T01:30:00+01:00"}' in message
    assert '"schedule":"2030-03-31T03:30:00+02:00"}' in message


@pytest.mark.parametrize(
    "fields",
    [
        {"at": 1893484800000, "timezone": "Asia/Tokyo"},
        {"at": "1893484800", "timezone": "Asia/Tokyo"},
        {"atMs": "1893484800000"},
        {"timestamp": 1893484800},
    ],
)
def test_epoch_names_an_instant_in_every_zone(tool: CronTool, fields: dict[str, Any]) -> None:
    job, _text = tool.created({"action": "create", "prompt": JOB_PROMPT, **fields})

    assert job.run_at == "2030-01-01T08:00:00+00:00"


def test_compact_local_timestamp_is_read_as_server_time(tool: CronTool) -> None:
    job, _text = tool.created({"action": "create", "prompt": JOB_PROMPT, "run_at": "203001010900"})

    assert job.run_at == "2030-01-01T08:00:00+00:00"


@pytest.mark.parametrize(
    ("fields", "text", "stand_in"),
    [
        ({"at": 900}, '"at" 900 is not an epoch time', "<local time such as 2030-01-01T09:00>"),
        (
            {"atMs": 1893484800},
            '"atMs" 1893484800 is not an epoch time in milliseconds',
            "<local time such as 2030-01-01T09:00>",
        ),
        ({"run_at": "2400"}, '"run_at" "2400" does not read as one time.', None),
        (
            {"run_at": "20300101"},
            '"run_at" "20300101" is a date without a time of day',
            "<2030-01-01 with a time of day, as 2030-01-01THH:MM>",
        ),
    ],
)
def test_digits_that_name_no_clear_time_are_refused_with_a_stand_in(
    tool: CronTool, fields: dict[str, Any], text: str, stand_in: str | None
) -> None:
    message = tool.refused({"action": "create", "prompt": JOB_PROMPT, **fields})

    assert text in message
    expected = stand_in or "<local time such as 2030-01-01T09:00>"
    assert message.endswith(
        f'Send: {{"action":"create","prompt":"{JOB_PROMPT}","schedule":"{expected}"}}'
    )


def test_bare_duration_is_refused_with_both_readings(tool: CronTool) -> None:
    message = tool.refused({"action": "create", "prompt": JOB_PROMPT, "schedule": "30m"})

    assert f'{{"action":"create","prompt":"{JOB_PROMPT}","schedule":"in 30m"}}' in message
    assert f'{{"action":"create","prompt":"{JOB_PROMPT}","schedule":"every 30m"}}' in message


def test_two_schedules_without_a_type_are_refused(tool: CronTool) -> None:
    message = tool.refused(
        {
            "action": "create",
            "prompt": JOB_PROMPT,
            "cron_expression": "0 3 * * *",
            "run_at": "2030-01-01T03:00",
        },
    )

    assert "it names more than one schedule" in message
    assert '"schedule":"0 3 * * *"' in message and '"schedule":"2030-01-01T03:00"' in message


def test_repeating_schedule_marked_one_time_is_refused(tool: CronTool) -> None:
    message = tool.refused(
        {
            "action": "create",
            "prompt": JOB_PROMPT,
            "cron_expression": "0 3 * * *",
            "schedule_type": "once",
        },
    )

    assert '"schedule":"0 3 * * *"}' in message
    assert '"schedule":"0 3 * * *","repeat":1}' in message


@pytest.mark.parametrize(
    ("schedule", "text", "stand_in"),
    [
        (
            "2030-01-01",
            "has no time of day",
            "<2030-01-01 with a time of day, as 2030-01-01THH:MM>",
        ),
        (
            "0 0 9 * * *",
            "cron here takes exactly five",
            "<five cron fields: minute hour day-of-month month day-of-week>",
        ),
    ],
)
def test_incomplete_schedules_are_refused_with_a_stand_in(
    tool: CronTool, schedule: str, text: str, stand_in: str
) -> None:
    message = tool.refused({"action": "create", "prompt": JOB_PROMPT, "schedule": schedule})

    assert text in message
    assert message.endswith(
        f'Send: {{"action":"create","prompt":"{JOB_PROMPT}","schedule":"{stand_in}"}}'
    )
    # Sent back unchanged, the stand-in schedules nothing.
    assert "create needs" in tool.refused(
        {"action": "create", "prompt": JOB_PROMPT, "schedule": stand_in}
    )


@pytest.mark.parametrize(
    ("fields", "text"),
    [
        ({"interval": 30}, "has no unit"),
        ({"interval_seconds": 90}, "is not a whole number of minutes"),
    ],
)
def test_unclear_interval_numbers_are_refused(
    tool: CronTool, fields: dict[str, Any], text: str
) -> None:
    assert text in tool.refused({"action": "create", "prompt": JOB_PROMPT, **fields})


# -- time zones --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("server", "zone"), [("Europe/Berlin", "europe/berlin"), ("UTC", "Z"), ("UTC", "+00:00")]
)
def test_timezone_that_is_the_server_zone_is_dropped(
    tmp_path: Path, server: str, zone: str
) -> None:
    tool = cron_tool(tmp_path, tz=server)

    job, text = tool.created(
        {"action": "create", "prompt": JOB_PROMPT, "schedule": "0 9 * * *", "timezone": zone}
    )

    assert job.cron_expression == "0 9 * * *"
    assert "note" not in text


@pytest.mark.parametrize("schedule", ["in 30m", "every 2h"])
def test_timezone_of_a_relative_schedule_is_irrelevant(tool: CronTool, schedule: str) -> None:
    tool.created(
        {"action": "create", "prompt": JOB_PROMPT, "schedule": schedule, "tz": "Mars/Olympus"}
    )


def test_local_time_in_another_zone_converts_exactly(tool: CronTool) -> None:
    job, text = tool.created(
        {
            "action": "create",
            "prompt": JOB_PROMPT,
            "schedule": "2030-01-01T09:00",
            "timezone": "Pacific/Kiritimati",
        },
    )

    assert job.run_at == "2029-12-31T19:00:00+00:00"
    assert "schedule: 2029-12-31T20:00:00+01:00" in text
    assert (
        'note: Read "2030-01-01T09:00" as Pacific/Kiritimati time: 2029-12-31T20:00:00+01:00 in '
        "the server time zone Europe/Berlin."
    ) in text


def test_offset_timestamp_in_its_own_zone_passes(tool: CronTool) -> None:
    job, _text = tool.created(
        {
            "action": "create",
            "prompt": JOB_PROMPT,
            "schedule": "2030-01-01T09:00:00-05:00",
            "timezone": "America/New_York",
        },
    )

    assert job.run_at == "2030-01-01T14:00:00+00:00"


def test_offset_timestamp_in_another_zone_is_refused_with_both_readings(tool: CronTool) -> None:
    message = tool.refused(
        {
            "action": "create",
            "prompt": JOB_PROMPT,
            "schedule": "2030-01-01T09:00:00Z",
            "timezone": "America/New_York",
        },
    )

    assert '"schedule":"2030-01-01T09:00:00Z"' in message
    assert '"schedule":"2030-01-01T15:00:00+01:00"' in message


def test_cron_in_a_zone_with_a_constant_offset_converts(tool: CronTool) -> None:
    job, text = tool.created(
        {
            "action": "create",
            "prompt": JOB_PROMPT,
            "schedule": "0 9 * * 1-5",
            "timezone": "Europe/London",
        },
    )

    assert job.cron_expression == "0 10 * * 1-5"
    assert 'note: Read "0 9 * * 1-5" as Europe/London time: "0 10 * * 1-5"' in text


@pytest.mark.parametrize(
    ("server", "zone", "schedule", "converted"),
    [
        ("UTC", "Asia/Tokyo", "30 3,9-17 * * *", "30 18,0-8 * * *"),
        ("UTC", "Asia/Tokyo", "0 3 * * 1", "0 18 * * 0"),
        ("UTC", "Asia/Tokyo", "0 1-2 * * mon-fri", "0 16-17 * * 0-4"),
        ("UTC", "Pacific/Kiritimati", "0 9 * * 0,3", "0 19 * * 2,6"),
        ("UTC", "Asia/Kolkata", "0 9 * * *", "30 3 * * *"),
        ("UTC", "Asia/Kolkata", "0 * * * *", "30 * * * *"),
        ("Europe/Berlin", "Europe/London", "*/15 1-3 * * *", "*/15 2-4 * * *"),
        ("Europe/Berlin", "Europe/London", "*/15 * * * *", "*/15 * * * *"),
    ],
)
def test_cron_under_a_constant_offset_fires_at_the_moments_the_zone_means(
    tmp_path: Path, server: str, zone: str, schedule: str, converted: str
) -> None:
    tool = cron_tool(tmp_path / "server", tz=server)

    job, text = tool.created(
        {"action": "create", "prompt": JOB_PROMPT, "schedule": schedule, "timezone": zone}
    )

    assert job.cron_expression == converted
    assert ("note:" in text) is (converted != schedule)
    assert fires(tool) == fires_in(tmp_path / "zone", zone, schedule)


@pytest.mark.parametrize(
    "schedule",
    [
        "0 1,12 * * 1",  # 01:00 moves to Sunday, 12:00 stays on Monday
        "0 3 1 * *",  # the day before the 1st is no fixed day of the month
        "0 3 * 1 *",  # 03:00 on January 1st is December 31st in UTC
        "0 * * * 1",  # Monday's hours start on Sunday in UTC
        # Digits int() cannot read name no hour, weekday or step to convert.
        "0 \u00b2 * * *",
        "0 3 * * \u00b2",
        "0 3 * * */\u00b2",
    ],
)
def test_cron_whose_fires_cannot_follow_the_zone_is_refused(tmp_path: Path, schedule: str) -> None:
    tool = cron_tool(tmp_path, tz="UTC")

    message = tool.refused(
        {"action": "create", "prompt": JOB_PROMPT, "schedule": schedule, "timezone": "Asia/Tokyo"},
    )

    assert "Convert the fields to server time and omit timezone." in message
    assert message.endswith(
        f'Send: {{"action":"create","prompt":"{JOB_PROMPT}",'
        '"schedule":"<five cron fields in server time>"}'
    )


def test_cron_in_a_zone_with_a_changing_offset_is_refused(tool: CronTool) -> None:
    message = tool.refused(
        {
            "action": "create",
            "prompt": JOB_PROMPT,
            "schedule": "0 9 * * *",
            "timezone": "America/New_York",
        },
    )

    assert "jobs use the server time zone Europe/Berlin" in message
    assert "changes during the year" in message
    assert re.search(
        r'Send: \{"action":"create","prompt":"[^"]+","schedule":"0 1[45] \* \* \*"\}$', message
    )


def test_offsets_that_differ_for_less_than_a_week_are_refused(
    tool: CronTool, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Jerusalem moves its clocks two days before Berlin in March and two hours before it in
    # October; from this Monday a weekly look at the offsets sees neither.
    monkeypatch.setattr("core.tools.cron.datetime", clock_at(datetime(2026, 9, 28, 12, tzinfo=UTC)))

    message = tool.refused(
        {
            "action": "create",
            "prompt": JOB_PROMPT,
            "schedule": "0 9 * * *",
            "timezone": "Asia/Jerusalem",
        },
    )

    assert "changes during the year" in message
    assert message.endswith(
        f'Send: {{"action":"create","prompt":"{JOB_PROMPT}","schedule":"0 8 * * *"}}'
    )


def test_hourly_cron_across_zones_changing_clocks_on_other_dates_is_refused(
    tmp_path: Path,
) -> None:
    tool = cron_tool(tmp_path / "server")
    schedule = "*/15 * * * *"

    message = tool.refused(
        {
            "action": "create",
            "prompt": JOB_PROMPT,
            "schedule": schedule,
            "timezone": "America/New_York",
        },
    )

    assert "change their clocks on different dates" in message
    assert message.endswith(
        f'Send: {{"action":"create","prompt":"{JOB_PROMPT}","schedule":"{schedule}"}}'
    )
    # Sent as offered, the job fires in server time, which differs around the changes.
    tool.created({"action": "create", "prompt": JOB_PROMPT, "schedule": schedule})
    assert fires(tool) != fires_in(tmp_path / "zone", "America/New_York", schedule)


@pytest.mark.parametrize(
    ("schedule", "reason", "meant"),
    [
        (
            "2030-03-31T01:30",
            "does not exist in Europe/London: the clocks jump over it that day.",
            [
                ("2030-03-31T01:30:00+01:00", "2030-03-31T00:30+00:00 in Europe/London"),
                ("2030-03-31T03:30:00+02:00", "2030-03-31T02:30+01:00 in Europe/London"),
            ],
        ),
        (
            "2030-10-27T01:30",
            "happens twice in Europe/London: the clocks go back over it that day.",
            [
                ("2030-10-27T02:30:00+02:00", "2030-10-27T01:30+01:00 in Europe/London"),
                ("2030-10-27T02:30:00+01:00", "2030-10-27T01:30+00:00 in Europe/London"),
            ],
        ),
    ],
)
def test_local_time_the_zone_skips_or_repeats_is_refused_with_each_instant(
    tool: CronTool, schedule: str, reason: str, meant: list[tuple[str, str]]
) -> None:
    message = tool.refused(
        {
            "action": "create",
            "prompt": JOB_PROMPT,
            "schedule": schedule,
            "timezone": "Europe/London",
        },
    )

    calls = " or ".join(
        f'{{"action":"create","prompt":"{JOB_PROMPT}","schedule":"{server}"}} ({label})'
        for server, label in meant
    )
    assert message == (
        f'cron was not run: "{schedule}" {reason} Send the one that is meant: {calls}.'
    )
    # Each offered call is valid as sent and fires at its own instant.
    job, _text = tool.created({"action": "create", "prompt": JOB_PROMPT, "schedule": meant[1][0]})
    assert datetime.fromisoformat(job.run_at or "") == datetime.fromisoformat(meant[1][0])


def test_offset_of_the_repeated_hour_names_its_instant(tool: CronTool) -> None:
    job, _text = tool.created(
        {
            "action": "create",
            "prompt": JOB_PROMPT,
            "schedule": "2030-10-27T01:30:00+00:00",
            "timezone": "Europe/London",
        },
    )

    assert job.run_at == "2030-10-27T01:30:00+00:00"


def test_unknown_zone_for_a_wall_clock_schedule_is_refused(tool: CronTool) -> None:
    message = tool.refused(
        {
            "action": "create",
            "prompt": JOB_PROMPT,
            "schedule": "0 9 * * *",
            "timezone": "Mars/Olympus",
        },
    )

    assert '"timezone" "Mars/Olympus" is not a known time zone' in message


def test_timezone_alone_on_update_is_refused(tool: CronTool) -> None:
    job_id = tool.add_job()

    message = tool.refused({"action": "update", "id": job_id, "timezone": "America/New_York"})

    assert "cannot keep America/New_York" in message


# -- event times -----------------------------------------------------------------------------


def _standup(tool: CronTool) -> str:
    return tool.calendar.create_event(
        title="Standup", start="2030-01-07T09:00", rrule="FREQ=WEEKLY"
    ).id


@pytest.mark.parametrize(
    "fields",
    [
        {"minutes_before": 30},
        {"before_start": 30},
        {"before_start": "30m"},
        {"schedule": "-PT30M"},
        {"schedule": "30 minutes before"},
        {"schedule": "30 min before the start"},
        {"schedule": "start-30m"},
        {"schedule": "start", "minutes_before": 30},
        {"edge": "start", "offset": -30},
    ],
)
def test_reminder_offsets_reach_the_event_time(tool: CronTool, fields: dict[str, Any]) -> None:
    standup = _standup(tool)

    job, _text = tool.created(
        {"action": "create", "event_id": standup, "prompt": JOB_PROMPT, **fields}
    )

    assert tool.service.format_schedule(job) == "start - 30m"


def test_offset_alone_moves_an_event_job(tool: CronTool) -> None:
    standup = _standup(tool)
    job = tool.created(
        {"action": "create", "event_id": standup, "prompt": JOB_PROMPT, "schedule": "start"}
    )[0]

    tool.succeeded({"action": "update", "id": job.id, "minutes_before": 10})

    assert tool.service.format_schedule(tool.only_job()) == "start - 10m"


@pytest.mark.parametrize(
    ("schedule", "question", "readings"),
    [
        (
            "30m",
            "whether the job runs before or after the event's start",
            ["start - 30m", "start + 30m"],
        ),
        ("+30m", "whether it counts from the event's start or end", ["start + 30m", "end + 30m"]),
    ],
)
def test_unsigned_or_edgeless_event_time_is_refused_with_each_reading(
    tool: CronTool, schedule: str, question: str, readings: list[str]
) -> None:
    standup = _standup(tool)
    call = {"action": "create", "event_id": standup, "prompt": JOB_PROMPT}

    message = tool.refused({**call, "schedule": schedule})

    choices = " or ".join(
        f'{{"action":"create","event_id":"{standup}","prompt":"{JOB_PROMPT}",'
        f'"schedule":"{reading}"}}'
        for reading in readings
    )
    assert message == f'cron was not run: schedule "{schedule}" does not say {question}: {choices}'


def test_unclear_event_time_on_an_event_job_update_is_refused_with_each_reading(
    tool: CronTool,
) -> None:
    standup = _standup(tool)
    job = tool.created(
        {"action": "create", "event_id": standup, "prompt": JOB_PROMPT, "schedule": "start"}
    )[0]

    message = tool.refused({"action": "update", "id": job.id, "schedule": "1h after"})

    assert message == (
        'cron was not run: schedule "1h after" does not say whether it counts from the '
        "event's start or end: "
        f'{{"action":"update","id":"{job.id}","schedule":"start + 1h"}} '
        f'or {{"action":"update","id":"{job.id}","schedule":"end + 1h"}}'
    )
