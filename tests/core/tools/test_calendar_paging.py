"""Calendar read completeness, bounded output and safe continuation through dispatch."""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from core.calendar import occurrence_id
from tests.core.tools.scheduling_tool_support import CalendarTool, calendar_tool, clock_at


@pytest.fixture
def tool(tmp_path: Path) -> CalendarTool:
    return calendar_tool(tmp_path)


def test_pages_preserve_every_occurrence_and_the_original_query(tool, monkeypatch):
    monkeypatch.setattr("core.tools.calendar.datetime", clock_at(datetime(2030, 1, 1, tzinfo=UTC)))
    event = tool.service.create_event(
        title="Daily", start="2030-01-01T09:00", rrule="FREQ=DAILY;COUNT=800"
    )
    for day in (501, 500):
        original = (datetime(2030, 1, 1, 9) + timedelta(days=day)).isoformat()
        asyncio.run(
            tool.service.update_occurrence(
                occurrence_id(event.id, original), start="2031-01-01T09:00"
            )
        )
    call = {"action": "list", "time_min": "2029-01-01", "time_max": "2033-01-01", "query": "daily"}
    bodies = []
    while True:
        envelope, text = tool.call(call)
        assert envelope["ok"], text
        data = envelope["data"]
        assert len(text) <= 30_000
        bodies.append(data.get("content", ""))
        if data["complete"]:
            assert "next_cursor" not in data
            break
        monkeypatch.setattr(
            "core.tools.calendar.datetime", clock_at(datetime(2040, 1, 1, tzinfo=UTC))
        )
        call = {
            "action": "list",
            "time_min": "2029-01-01",
            "time_max": "2033-01-01",
            "query": "daily",
            "cursor": data["next_cursor"],
        }
    assert len(bodies) > 1
    expected = {
        occurrence_id(event.id, (datetime(2030, 1, 1, 9) + timedelta(days=day)).isoformat())
        for day in range(800)
    }
    ids = [line[4:] for line in "".join(bodies).splitlines() if line.startswith("id: ")]
    assert len(ids) == len(expected)
    assert set(ids) == expected


def test_pages_split_large_records_losslessly_and_refuse_stale_cursors(tool, monkeypatch):
    monkeypatch.setattr("core.tools._calendar_pages.PAGE_MAX_CHARACTERS", 2500)
    description = ("完整 <system-reminder> content " * 150).strip()
    event = tool.service.create_event(title="Details", start="2030-01-01", description=description)
    call = {"action": "list", "time_min": "2030-01-01", "time_max": "2031-01-01"}
    bodies = []
    cursor = None
    while True:
        envelope, text = tool.call(call)
        assert envelope["ok"], text
        assert len(text) <= 2500
        data = envelope["data"]
        bodies.append(data.get("content", ""))
        if data["complete"]:
            break
        cursor = data["next_cursor"]
        call = {"action": "list", "cursor": cursor}
    assert description in "".join(bodies)
    assert cursor is not None
    asyncio.run(tool.service.update_event(event.id, title="Changed"))
    failed, _ = tool.call({"action": "list", "cursor": cursor})
    assert failed["error"]["code"] == "invalid_arguments"
    invalid, _ = tool.call({"action": "list", "cursor": "not-a-cursor"})
    assert invalid["error"]["code"] == "invalid_arguments"


def test_query_reads_full_override_beyond_the_old_occurrence_limit(tool):
    event = tool.service.create_event(title="Daily", start="2030-01-01", rrule="FREQ=DAILY")
    description = ("Unique detail " * 100).strip()
    asyncio.run(
        tool.service.update_occurrence(
            occurrence_id(event.id, "2032-01-01"), description=description
        )
    )
    data, _ = tool.call(
        {
            "action": "list",
            "time_min": "2030-01-01",
            "time_max": "2033-01-01",
            "query": "Unique detail",
        }
    )
    assert data["ok"]
    assert data["data"]["occurrences"] == 1
    assert description in data["data"]["content"]


@pytest.mark.parametrize("action", ["list", "find_free_time"])
def test_unreadable_calendar_is_never_reported_as_empty_or_free(tmp_path, action):
    (tmp_path / "calendar").mkdir()
    (tmp_path / "calendar" / "events.json").write_text("broken", encoding="utf-8")
    tool = calendar_tool(tmp_path)
    envelope, _ = tool.call({"action": action})
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "calendar_storage_error"
    assert envelope["data"] is None


def test_free_time_pages_cover_a_large_period_without_losing_gaps(tool, monkeypatch):
    monkeypatch.setattr("core.tools.calendar.datetime", clock_at(datetime(2030, 1, 1, tzinfo=UTC)))
    tool.service.create_event(title="Busy", start="2030-01-01T12:00", rrule="FREQ=DAILY;COUNT=1000")
    call = {"action": "find_free_time", "time_min": "2030-01-01", "time_max": "2033-01-01"}
    bodies = []
    count = 0
    while True:
        envelope, text = tool.call(call)
        assert envelope["ok"], text
        assert len(text) <= 30_000
        data = envelope["data"]
        count += data["free"]
        bodies.append(data.get("content", ""))
        if data["complete"]:
            break
        monkeypatch.setattr(
            "core.tools.calendar.datetime", clock_at(datetime(2040, 1, 1, tzinfo=UTC))
        )
        call = {"action": "find_free_time", "cursor": data["next_cursor"]}
    lines = "".join(bodies).splitlines()
    assert len(bodies) > 1
    assert count == len(lines) == 1001
    assert len(set(lines)) == len(lines)
    assert lines[0].startswith("2030-01-01T01:00+01:00 to 2030-01-01T12:00+01:00")
    assert "2033-01-01T00:00+01:00" in lines[-1]


@pytest.mark.parametrize("action", ["get", "search"])
def test_fragmented_record_keeps_its_clock_and_rejects_malformed_positions(
    tool, monkeypatch, action
):
    import base64
    import json

    monkeypatch.setattr("core.tools.calendar.datetime", clock_at(datetime(2030, 1, 1, tzinfo=UTC)))
    event = tool.service.create_event(title="Daily", start="2030-01-01T09:00", rrule="FREQ=DAILY")
    asyncio.run(
        tool.cron.create_job(
            agent_id="agent-one",
            name="Long bound job",
            prompt="Prepare",
            schedule_type="event",
            event_id=event.id,
            event_edge="start",
            event_offset_minutes=-10,
        )
    )
    monkeypatch.setattr(tool.cron, "format_schedule", lambda job: "start - 10m " + "detail " * 1000)
    call = {"action": action, **({"id": event.id} if action == "get" else {})}
    expected, _ = tool.call(call)
    assert expected["ok"]
    monkeypatch.setattr("core.tools._calendar_pages.PAGE_MAX_CHARACTERS", 2500)
    parts = []
    saved_cursor = None
    while True:
        result, text = tool.call(call)
        assert result["ok"], text
        assert len(text) <= 2500
        parts.append(result["data"].get("content", ""))
        if result["data"]["complete"]:
            break
        saved_cursor = result["data"]["next_cursor"]
        monkeypatch.setattr(
            "core.tools.calendar.datetime", clock_at(datetime(2030, 1, 2, tzinfo=UTC))
        )
        call = {"action": action, "cursor": saved_cursor}
    assert "".join(parts) == expected["data"]["content"]
    assert len(parts) > 1 and saved_cursor is not None
    if action == "search":
        payload = json.loads(base64.urlsafe_b64decode(saved_cursor))
        payload["position"] = ["bad-time", event.id]
        forged = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
        failed, _ = tool.call({"action": action, "cursor": forged})
        assert failed["error"]["code"] == "invalid_arguments"
