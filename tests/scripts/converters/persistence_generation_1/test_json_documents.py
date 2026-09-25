"""Tests for the Generation 1 conversion of durable JSON documents."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from core.projects.projects import PROJECT_DEFAULT_ALLOWED_TOOLS
from core.settings import validate_data_dir_config
from scripts.converters.persistence_generation_1._context import ConversionContext
from scripts.converters.persistence_generation_1.json_documents import AREA, convert

_TIMESTAMP = "2026-06-18T10:00:00Z"
_MCP_CONNECTIONS = "extension-data/mcp/connections.json"
# Documents whose Generation 1 location differs from their old one.
_MOVED = {"mcp/connections.json": _MCP_CONNECTIONS}


def _write(root: Path, relative: str, value: Any) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    text = value if isinstance(value, str) else json.dumps(value)
    path.write_text(text, encoding="utf-8")


def _staged(context: ConversionContext, relative: str) -> Any:
    return json.loads((context.staging / relative).read_text(encoding="utf-8"))


def _context(tmp_path: Path) -> ConversionContext:
    source = tmp_path / "data"
    source.mkdir(exist_ok=True)
    return ConversionContext(source=source, staging=tmp_path / "staging")


def _agent(agent_id: str = "main", **fields: Any) -> dict[str, Any]:
    return {
        "id": agent_id,
        "name": "Main",
        "model": "",
        "fallback_models": [],
        "temperature": None,
        "thinking_effort": None,
        "allowed_skills": ["*"],
        "custom_system_prompt_enabled": False,
        "created_at": _TIMESTAMP,
        "updated_at": _TIMESTAMP,
        **fields,
    }


def _cron_job(job_id: str = "job-1", **fields: Any) -> dict[str, Any]:
    return {
        "id": job_id,
        "agent_id": "main",
        "name": "Nightly",
        "prompt": "do it",
        "schedule_type": "cron",
        "cron_expression": "0 3 * * *",
        "status": "active",
        "created_at": _TIMESTAMP,
        **fields,
    }


def _attachment_sidecar(**fields: Any) -> dict[str, Any]:
    return {
        "id": "att_000000000001",
        "filename": "notes.txt",
        "media_type": "text/plain",
        "size_bytes": 5,
        "stored_at": _TIMESTAMP,
        "file_path": "C:/Users/someone/.vbot/artifacts/attachments/att_000000000001.txt",
        "transcription": None,
        **fields,
    }


def _legacy_data_dir(root: Path) -> dict[str, Any]:
    documents: dict[str, Any] = {
        "settings.json": {"server_port": 8500},
        "agents/main/agent.json": _agent(tool_access={"mode": "all"}),
        "agents/order.json": {"revision": 1, "agent_ids": ["main"]},
        "agents/main/prompts/layout.json": [{"id": "core:soul", "enabled": True}],
        "prompts/layout.json": [{"id": "core:tools", "enabled": False, "source": "core"}],
        "projects/vbot/project.json": {
            "project_id": "vbot",
            "display_name": "vBot",
            "cwd": "/srv/repos/vbot",
            "created_at": _TIMESTAMP,
            "updated_at": _TIMESTAMP,
        },
        "channels/tg/channel.json": {
            "id": "tg",
            "platform": "telegram",
            "agent_id": "main",
            "token_env_var": "TELEGRAM_BOT_TOKEN",
            "enabled": True,
            "owner_user_ids": [50],
        },
        "cron/jobs.json": [_cron_job(timezone="Europe/Berlin")],
        "bootstrap/jobs.json": [],
        "calendar/events.json": [],
        "calendar/actions.json": {"actions": [], "executions": {}},
        "skills/policy.json": {"version": 2, "disabled": ["deploy"], "shared": {}},
        "terminals/launch-history.json": {"version": 1, "entries": []},
        "terminals/groups.json": {"version": 1, "groups": []},
        "oauth/github-copilot-oauth.json": {"access_token": "token"},
        "mcp/connections.json": [{"id": "example", "transport": "stdio", "command": "unused"}],
        "artifacts/attachments/att_000000000001.json": _attachment_sidecar(),
        "artifacts/speech/aud_000000000001.json": {
            "id": "aud_000000000001",
            "filename": "aud_000000000001.mp3",
            "media_type": "audio/mpeg",
            "size_bytes": 3,
        },
    }
    for relative, value in documents.items():
        _write(root, relative, value)
    return documents


def test_converted_data_directory_passes_the_doctor_and_source_is_untouched(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    documents = _legacy_data_dir(context.source)
    before = {path: path.read_bytes() for path in context.source.rglob("*") if path.is_file()}

    convert(context)

    assert {path: path.read_bytes() for path in before} == before
    staged = {
        path.relative_to(context.staging).as_posix()
        for path in context.staging.rglob("*")
        if path.is_file()
    }
    assert staged == {_MOVED.get(relative, relative) for relative in documents}
    assert [path.as_posix() for path in context.retired] == list(_MOVED)
    reports = validate_data_dir_config(context.staging)
    assert len(reports) == len(documents)
    assert [
        (report.file_path.name, diagnostic.path, diagnostic.message)
        for report in reports
        for diagnostic in report.diagnostics
    ] == []
    assert all(_staged(context, relative)["format_version"] == 1 for relative in staged)


def test_arrays_become_named_arrays_and_versions_move_to_format_version(tmp_path: Path) -> None:
    context = _context(tmp_path)
    _legacy_data_dir(context.source)

    convert(context)

    assert _staged(context, "prompts/layout.json") == {
        "format_version": 1,
        "entries": [{"id": "core:tools", "enabled": False, "source": "core"}],
    }
    assert _staged(context, "bootstrap/jobs.json") == {"format_version": 1, "jobs": []}
    assert _staged(context, "calendar/events.json") == {"format_version": 1, "events": []}
    assert _staged(context, _MCP_CONNECTIONS)["connections"][0]["id"] == "example"
    assert _staged(context, "skills/policy.json") == {
        "format_version": 1,
        "disabled": ["deploy"],
        "shared": {},
    }
    assert _staged(context, "terminals/groups.json") == {"format_version": 1, "groups": []}
    counts = context.report.counts[AREA]
    assert counts["prompt_layouts"] == 2
    assert counts["terminal_documents"] == 2


def test_cron_jobs_lose_timezone_and_get_their_derived_name(tmp_path: Path) -> None:
    context = _context(tmp_path)
    unnamed = _cron_job("job-2", name="", prompt="\n## Morning   briefing\nDetails")
    nameless = {key: value for key, value in _cron_job("job-3").items() if key != "name"}
    invalid = "not a job"
    _write(
        context.source,
        "cron/jobs.json",
        [_cron_job(timezone="UTC", future="kept"), unnamed, nameless, invalid],
    )

    convert(context)

    jobs = _staged(context, "cron/jobs.json")["jobs"]
    assert "timezone" not in jobs[0]
    assert jobs[0]["future"] == "kept"
    assert jobs[0]["name"] == "Nightly"
    assert jobs[1]["name"] == "Morning briefing"
    assert jobs[2]["name"] == "do it"
    assert jobs[3] == invalid
    assert context.report.counts[AREA]["cron_timezones_dropped"] == 1
    assert context.report.counts[AREA]["cron_names_derived"] == 2


@pytest.mark.parametrize(
    ("allowed_tools", "tool_access"),
    [
        (["*"], {"mode": "all"}),
        (["read", "memory", "bash"], {"mode": "selected", "allowed": ["read", "bash"]}),
    ],
)
def test_agent_allowed_tools_become_tool_access(
    tmp_path: Path, allowed_tools: list[str], tool_access: dict[str, Any]
) -> None:
    context = _context(tmp_path)
    _write(context.source, "agents/main/agent.json", _agent(allowed_tools=allowed_tools))

    convert(context)

    agent = _staged(context, "agents/main/agent.json")
    assert "allowed_tools" not in agent
    assert {key: value for key, value in agent["tool_access"].items() if value} == tool_access
    assert context.report.counts[AREA]["agent_allowed_tools_converted"] == 1


def test_agent_allowed_tools_next_to_tool_access_are_dropped(tmp_path: Path) -> None:
    context = _context(tmp_path)
    policy = {"mode": "selected", "allowed": ["read"]}
    _write(
        context.source,
        "agents/main/agent.json",
        _agent(allowed_tools=["*"], tool_access=policy),
    )

    convert(context)

    agent = _staged(context, "agents/main/agent.json")
    assert "allowed_tools" not in agent
    assert agent["tool_access"] == policy
    assert [item.item for item in context.report.skipped] == ["agents/main/agent.json"]


def test_agent_with_unconvertible_allowed_tools_is_left_unconverted(tmp_path: Path) -> None:
    context = _context(tmp_path)
    _write(context.source, "agents/main/agent.json", _agent(allowed_tools=["*", "read"]))

    convert(context)

    assert not (context.staging / "agents/main/agent.json").exists()
    [skipped] = context.report.skipped
    assert skipped.item == "agents/main/agent.json"
    assert skipped.reason.startswith("left unconverted: allowed_tools mixes '*'")


@pytest.mark.parametrize(
    ("policy", "converted", "narrowed"),
    [
        (
            {"mode": "selected", "allowed": ["read", "grep", "glob"]},
            {"mode": "selected", "allowed": ["read", "search_files"]},
            False,
        ),
        (
            {"mode": "all", "denied": ["grep", "glob"]},
            {"mode": "all", "denied": ["search_files"]},
            False,
        ),
        (
            {"mode": "selected", "allowed": ["read", "grep"]},
            {"mode": "selected", "allowed": ["read"]},
            True,
        ),
        ({"mode": "all", "denied": ["glob"]}, {"mode": "all", "denied": ["search_files"]}, True),
        (
            {"mode": "selected", "allowed": ["grep", "search_files"]},
            {"mode": "selected", "allowed": ["search_files"]},
            False,
        ),
    ],
)
def test_agent_search_tools_are_consolidated_without_widening(
    tmp_path: Path, policy: dict[str, Any], converted: dict[str, Any], narrowed: bool
) -> None:
    context = _context(tmp_path)
    _write(context.source, "agents/main/agent.json", _agent(tool_access=policy))

    convert(context)

    assert _staged(context, "agents/main/agent.json")["tool_access"] == converted
    assert context.report.counts[AREA]["search_tools_consolidated"] == 1
    assert bool(context.report.skipped) == narrowed


def test_legacy_allowed_tools_with_both_search_tools_grant_search_files(tmp_path: Path) -> None:
    context = _context(tmp_path)
    _write(context.source, "agents/main/agent.json", _agent(allowed_tools=["glob", "grep"]))

    convert(context)

    assert _staged(context, "agents/main/agent.json")["tool_access"] == {
        "mode": "selected",
        "allowed": ["search_files"],
    }


def test_project_search_tools_are_consolidated_in_whitelist_and_overrides(tmp_path: Path) -> None:
    context = _context(tmp_path)
    project = {
        "project_id": "a",
        "display_name": "A",
        "cwd": "/srv/a",
        "allowed_tools": ["read", "grep", "glob"],
        "overrides": {
            "worker": {
                "model": "m",
                "tool_access": {"mode": "selected", "allowed": ["glob", "grep"]},
            },
            "reader": {"tool_access": {"mode": "selected", "allowed": ["grep"]}},
        },
    }
    _write(context.source, "projects/a/project.json", project)
    _write(
        context.source, "projects/b/project.json", {**project, "allowed_tools": ["read", "glob"]}
    )

    convert(context)

    converted = _staged(context, "projects/a/project.json")
    assert converted["allowed_tools"] == ["read", "search_files"]
    assert converted["overrides"] == {
        "worker": {"model": "m", "tool_access": {"mode": "selected", "allowed": ["search_files"]}},
        "reader": {"tool_access": {"mode": "selected", "allowed": []}},
    }
    assert _staged(context, "projects/b/project.json")["allowed_tools"] == ["read"]
    assert sorted(item.reason.split(":")[0] for item in context.report.skipped) == [
        "allowed_tools",
        "overrides.reader.tool_access",
        "overrides.reader.tool_access",
    ]


def test_invalid_tool_access_is_carried_over_for_the_application_to_report(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    policy = {"mode": "selected", "allowed": ["grep", "grep"]}
    _write(context.source, "agents/main/agent.json", _agent(tool_access=policy))

    convert(context)

    assert _staged(context, "agents/main/agent.json")["tool_access"] == policy
    assert context.report.skipped == []


def test_project_without_tool_whitelist_gets_the_default_one(tmp_path: Path) -> None:
    context = _context(tmp_path)
    project = {"project_id": "a", "display_name": "A", "cwd": "/srv/a"}
    _write(context.source, "projects/a/project.json", project)
    _write(context.source, "projects/b/project.json", {**project, "allowed_tools": []})

    convert(context)

    assert _staged(context, "projects/a/project.json")["allowed_tools"] == list(
        PROJECT_DEFAULT_ALLOWED_TOOLS
    )
    assert _staged(context, "projects/b/project.json")["allowed_tools"] == []
    assert context.report.counts[AREA]["project_allowed_tools_filled"] == 1


def test_channel_owner_user_ids_are_dropped_and_reported(tmp_path: Path) -> None:
    context = _context(tmp_path)
    _write(context.source, "channels/tg/channel.json", {"id": "tg", "owner_user_ids": [50]})

    convert(context)

    assert _staged(context, "channels/tg/channel.json") == {"format_version": 1, "id": "tg"}
    assert "group admins" in context.report.skipped[0].reason


def test_retired_mcp_agents_field_is_kept_and_reported(tmp_path: Path) -> None:
    context = _context(tmp_path)
    connection = {"id": "example", "transport": "stdio", "command": "x", "agents": ["main"]}
    _write(context.source, "mcp/connections.json", [connection])

    convert(context)

    assert _staged(context, _MCP_CONNECTIONS)["connections"] == [connection]
    assert "agents field" in context.report.skipped[0].reason


def test_attachment_sidecars_lose_the_stored_path_and_the_retired_text_cache(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    _write(
        context.source,
        "artifacts/attachments/att_000000000001.json",
        _attachment_sidecar(text_content="hello", future={"kept": True}),
    )
    _write(
        context.source,
        "artifacts/attachments/att_000000000002.json",
        {**_attachment_sidecar(id="att_000000000002"), "transcription": "hi"},
    )
    # An interrupted store leaves an empty reservation; vBot never reads it.
    _write(context.source, "artifacts/attachments/att_000000000003.json", "")

    convert(context)

    first = _staged(context, "artifacts/attachments/att_000000000001.json")
    assert first == {
        "format_version": 1,
        "id": "att_000000000001",
        "filename": "notes.txt",
        "media_type": "text/plain",
        "size_bytes": 5,
        "stored_at": _TIMESTAMP,
        "transcription": None,
        "future": {"kept": True},
    }
    assert _staged(context, "artifacts/attachments/att_000000000002.json")["transcription"] == "hi"
    counts = context.report.counts[AREA]
    assert counts["attachment_metadata"] == 2
    assert counts["attachment_file_path_dropped"] == 2
    assert counts["attachment_text_content_dropped"] == 1
    [skipped] = context.report.skipped
    assert skipped.item == "artifacts/attachments/att_000000000003.json"
    assert "unreadable JSON" in skipped.reason


@pytest.mark.parametrize(
    ("text", "count"),
    [
        ('{"format_version": 1, "connections": []}', "already_current"),
        ('{"connections": []}', None),
    ],
)
def test_mcp_connections_move_even_when_not_rewritten(
    tmp_path: Path, text: str, count: str | None
) -> None:
    context = _context(tmp_path)
    _write(context.source, "mcp/connections.json", text)

    convert(context)

    # vBot reads only the new location, so the document moves unchanged and
    # the application reports a document it refuses there.
    assert (context.staging / _MCP_CONNECTIONS).read_text(encoding="utf-8") == text
    assert [path.as_posix() for path in context.retired] == ["mcp/connections.json"]
    assert context.report.counts[AREA]["relocated"] == 1
    if count is None:
        assert "expected a JSON array" in context.report.skipped[0].reason
    else:
        assert context.report.counts[AREA][count] == 1


def test_mcp_connections_stay_when_the_new_location_exists(tmp_path: Path) -> None:
    context = _context(tmp_path)
    _write(context.source, "mcp/connections.json", [])
    _write(context.source, _MCP_CONNECTIONS, {"format_version": 1, "connections": []})

    convert(context)

    assert not (context.staging / _MCP_CONNECTIONS).exists()
    assert context.retired == []
    [skipped] = context.report.skipped
    assert (skipped.item, skipped.reason) == (
        "mcp/connections.json",
        f"left in place: {_MCP_CONNECTIONS} already exists",
    )


@pytest.mark.parametrize(
    ("relative", "text", "reason"),
    [
        ("settings.json", "{broken", "unreadable JSON"),
        ("cron/jobs.json", '{"jobs": []}', "expected a JSON array"),
        ("skills/policy.json", '{"version": 1}', "expected version 2"),
        ("terminals/groups.json", '{"version": true, "groups": []}', "expected version 1"),
        ("oauth/x.json", "[]", "expected a JSON object"),
        ("calendar/events.json", '{"format_version": 2, "events": []}', "format_version 2"),
    ],
)
def test_documents_that_cannot_be_converted_are_reported_and_not_staged(
    tmp_path: Path, relative: str, text: str, reason: str
) -> None:
    context = _context(tmp_path)
    _write(context.source, relative, text)

    convert(context)

    assert not (context.staging / relative).exists()
    [skipped] = context.report.skipped
    assert (skipped.area, skipped.item) == (AREA, relative)
    assert reason in skipped.reason


def test_current_documents_are_not_rewritten(tmp_path: Path) -> None:
    context = _context(tmp_path)
    _write(context.source, "cron/jobs.json", {"format_version": 1, "jobs": []})

    convert(context)

    assert not (context.staging / "cron/jobs.json").exists()
    assert context.report.counts[AREA] == {"already_current": 1}
    assert context.report.skipped == []
