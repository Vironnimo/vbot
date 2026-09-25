"""Tests for the Generation 1 conversion of durable JSON documents."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

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


def _legacy_agent(agent_id: str = "main", **fields: Any) -> dict[str, Any]:
    """An Agent as vBot wrote it before the fallback chain replaced ``fallback_model``."""
    agent = {key: value for key, value in _agent(agent_id).items() if key != "fallback_models"}
    return {**agent, "fallback_model": "", **fields}


def _diagnostics(context: ConversionContext) -> list[tuple[str, str, str]]:
    """``(document, path, message)`` of every diagnostic ``doctor config`` reports."""
    return [
        (
            report.file_path.relative_to(context.staging).as_posix(),
            diagnostic.path,
            diagnostic.message,
        )
        for report in validate_data_dir_config(context.staging)
        for diagnostic in report.diagnostics
    ]


def _legacy_data_dir(root: Path) -> dict[str, Any]:
    documents: dict[str, Any] = {
        "settings.json": {
            "server_port": 8500,
            "defaults": {"agent": {"fallback_model": "", "thinking_effort": "high"}},
            "recall": {"backend": "jsonl_scan"},
            "live_voice": {"enabled": True},
            "reflection": {"enabled": True, "skill_tool_call_interval": 20},
        },
        "agents/main/agent.json": _legacy_agent(tool_access={"mode": "all"}),
        "agents/order.json": {"revision": 1, "agent_ids": ["main"]},
        "agents/main/prompts/layout.json": [
            {"id": "core:soul", "enabled": True},
            {"id": "core:runtime", "enabled": False, "source": "core"},
            {"id": "core:project_files", "enabled": True, "source": "core"},
        ],
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
    assert len(validate_data_dir_config(context.staging)) == len(documents)
    # Retired fields and values are normalized, so none is left as an unknown field.
    assert _diagnostics(context) == []
    assert all(_staged(context, relative)["format_version"] == 1 for relative in staged)


@pytest.mark.parametrize("mode", [0o600, 0o640, 0o400])
@pytest.mark.parametrize(
    ("relative", "content"),
    [
        ("oauth/github-copilot-oauth.json", {"access_token": "secret-token"}),
        ("settings.json", {}),
        ("mcp/connections.json", []),
        ("mcp/connections.json", {"format_version": 1, "connections": []}),
        ("mcp/connections.json", "invalid JSON kept when relocated"),
    ],
)
def test_staged_json_keeps_source_permissions(
    tmp_path: Path, mode: int, relative: str, content: Any
) -> None:
    context = _context(tmp_path)
    _write(context.source, relative, content)
    source = context.source / relative
    source.chmod(mode)
    source_mode = stat.S_IMODE(source.stat().st_mode)
    original = source.read_bytes()

    convert(context)

    staged = context.staging / _MOVED.get(relative, relative)
    assert stat.S_IMODE(staged.stat().st_mode) == source_mode
    assert source.read_bytes() == original
    assert stat.S_IMODE(source.stat().st_mode) == source_mode


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits and umask")
def test_oauth_staging_is_private_before_secret_bytes_are_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = _context(tmp_path)
    _write(context.source, "oauth/token.json", {"access_token": "secret-token"})
    (context.source / "oauth/token.json").chmod(0o600)
    actual_fdopen = os.fdopen
    observed_modes = []

    def observe_fdopen(descriptor: int, *args: Any, **kwargs: Any) -> Any:
        observed_modes.append(stat.S_IMODE(os.fstat(descriptor).st_mode))
        return actual_fdopen(descriptor, *args, **kwargs)

    monkeypatch.setattr(os, "fdopen", observe_fdopen)
    original_umask = os.umask(0o022)
    try:
        convert(context)
    finally:
        os.umask(original_umask)

    assert observed_modes == [0o600]
    assert _staged(context, "oauth/token.json")["access_token"] == "secret-token"


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


def test_retired_tool_names_in_agent_tool_access_are_replaced_and_reported(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    policy = {
        "mode": "selected",
        "allowed": ["bash", "edit", "grep", "write", "glob", "subagent", "subagent_result"],
    }
    _write(context.source, "agents/main/agent.json", _agent(tool_access=policy))
    _write(
        context.source,
        "agents/other/agent.json",
        _agent("other", tool_access={"mode": "all", "denied": ["edit", "browser"]}),
    )
    _write(
        context.source,
        "agents/current/agent.json",
        _agent("current", tool_access={"mode": "selected", "allowed": ["search_files"]}),
    )

    convert(context)

    assert _staged(context, "agents/main/agent.json")["tool_access"] == {
        "mode": "selected",
        "allowed": ["bash", "apply_patch", "search_files", "subagent"],
    }
    assert _staged(context, "agents/other/agent.json")["tool_access"] == {"mode": "all"}
    assert context.report.counts[AREA]["retired_tool_names_converted"] == 2
    assert [(item.item, item.reason) for item in context.report.skipped] == [
        (
            "agents/main/agent.json",
            "tool_access: edit and write replaced by apply_patch; "
            "grep and glob replaced by search_files; "
            "subagent_result dropped: subagent includes its status lookup",
        ),
        (
            "agents/other/agent.json",
            "tool_access: edit dropped (apply_patch stays allowed); "
            "browser dropped: browser automation moved to the playwright-cli Skill",
        ),
    ]


def test_retired_tool_names_that_do_not_cover_the_successor_report_the_narrowing(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    policy = {"mode": "selected", "allowed": ["read", "edit"]}
    _write(context.source, "agents/main/agent.json", _agent(tool_access=policy))

    convert(context)

    assert _staged(context, "agents/main/agent.json")["tool_access"] == {
        "mode": "selected",
        "allowed": ["read"],
    }
    [skipped] = context.report.skipped
    assert skipped.reason == (
        "tool_access: edit dropped (apply_patch not granted): apply_patch needs write, "
        "so the access of edit is not carried over; enable apply_patch explicitly if wanted"
    )


def test_legacy_allowed_tools_with_retired_names_grant_their_successors(tmp_path: Path) -> None:
    context = _context(tmp_path)
    _write(
        context.source,
        "agents/main/agent.json",
        _agent(allowed_tools=["glob", "grep", "write", "terminal_beta"]),
    )

    convert(context)

    assert _staged(context, "agents/main/agent.json")["tool_access"] == {
        "mode": "selected",
        "allowed": ["search_files", "apply_patch", "terminal"],
    }
    assert context.report.counts[AREA]["agent_allowed_tools_converted"] == 1
    assert context.report.counts[AREA]["retired_tool_names_converted"] == 1


def test_retired_tool_names_are_replaced_in_project_whitelists_and_overrides(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    project = {
        "project_id": "a",
        "display_name": "A",
        "cwd": "/srv/a",
        "allowed_tools": ["read", "write", "edit", "grep", "glob", "terminal_beta", "bash"],
        "overrides": {
            "worker": {
                "model": "m",
                "tool_access": {"mode": "selected", "allowed": ["glob", "grep", "write"]},
            },
            "reviewer": {"tool_access": {"mode": "all", "denied": ["bash", "edit", "write"]}},
            "reader": {"tool_access": {"mode": "selected", "allowed": ["grep"]}},
        },
    }
    _write(context.source, "projects/a/project.json", project)
    _write(
        context.source,
        "projects/b/project.json",
        {**project, "allowed_tools": ["read", "apply_patch", "write", "glob"], "overrides": {}},
    )

    convert(context)

    converted = _staged(context, "projects/a/project.json")
    assert converted["allowed_tools"] == ["read", "apply_patch", "search_files", "terminal", "bash"]
    assert converted["overrides"] == {
        "worker": {
            "model": "m",
            "tool_access": {"mode": "selected", "allowed": ["search_files", "apply_patch"]},
        },
        "reviewer": {"tool_access": {"mode": "all", "denied": ["bash", "apply_patch"]}},
        "reader": {"tool_access": {"mode": "selected", "allowed": []}},
    }
    assert _staged(context, "projects/b/project.json")["allowed_tools"] == ["read", "apply_patch"]
    assert context.report.counts[AREA]["retired_tool_names_converted"] == 5
    assert [(item.item, item.reason.split(":")[0]) for item in context.report.skipped] == [
        ("projects/a/project.json", "allowed_tools"),
        ("projects/a/project.json", "overrides.worker.tool_access"),
        ("projects/a/project.json", "overrides.reviewer.tool_access"),
        ("projects/a/project.json", "overrides.reader.tool_access"),
        ("projects/b/project.json", "allowed_tools"),
    ]
    assert context.report.skipped[2].reason == (
        "overrides.reviewer.tool_access: edit and write replaced by a denial of apply_patch"
    )
    assert context.report.skipped[4].reason == (
        "allowed_tools: write dropped (apply_patch already allowed); glob dropped "
        "(search_files not granted): search_files needs both grep and glob, so the access "
        "of glob is not carried over; enable search_files explicitly if wanted"
    )


def test_invalid_tool_access_is_carried_over_for_the_application_to_report(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    policy = {"mode": "selected", "allowed": ["grep", "grep"]}
    _write(context.source, "agents/main/agent.json", _agent(tool_access=policy))

    convert(context)

    assert _staged(context, "agents/main/agent.json")["tool_access"] == policy
    assert context.report.skipped == []


@pytest.mark.parametrize("empty", ["", "  ", None])
def test_empty_retired_fallback_model_is_dropped(tmp_path: Path, empty: str | None) -> None:
    context = _context(tmp_path)
    _write(context.source, "agents/main/agent.json", _legacy_agent(fallback_model=empty))

    convert(context)

    agent = _staged(context, "agents/main/agent.json")
    assert "fallback_model" not in agent
    assert "fallback_models" not in agent
    assert context.report.counts[AREA]["fallback_model_dropped"] == 1
    assert context.report.skipped == []
    assert _diagnostics(context) == []


def test_retired_fallback_model_becomes_the_first_fallback_chain_entry(tmp_path: Path) -> None:
    context = _context(tmp_path)
    legacy = _legacy_agent(fallback_model=" openrouter/vendor/model::api-key ")
    _write(context.source, "agents/main/agent.json", legacy)

    convert(context)

    agent = _staged(context, "agents/main/agent.json")
    assert agent["fallback_models"] == ["openrouter/vendor/model::api-key"]
    # The chain takes the retired field's place.
    assert list(agent) == [
        "format_version",
        *("fallback_models" if key == "fallback_model" else key for key in legacy),
    ]
    assert context.report.counts[AREA]["fallback_model_converted"] == 1
    assert context.report.skipped == []
    assert _diagnostics(context) == []


def test_retired_fallback_model_next_to_a_fallback_chain_is_dropped_and_reported(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    _write(
        context.source,
        "agents/main/agent.json",
        _legacy_agent(fallback_model="provider/old", fallback_models=["provider/new"]),
    )

    convert(context)

    agent = _staged(context, "agents/main/agent.json")
    assert agent["fallback_models"] == ["provider/new"]
    assert "fallback_model" not in agent
    assert context.report.counts[AREA]["fallback_model_dropped"] == 1
    assert [(item.item, item.reason) for item in context.report.skipped] == [
        (
            "agents/main/agent.json",
            "retired fallback_model 'provider/old' dropped; the existing fallback_models applies",
        )
    ]


def test_invalid_retired_fallback_model_stays_invalid_for_the_application_to_report(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    _write(context.source, "agents/main/agent.json", _legacy_agent(fallback_model=5))

    convert(context)

    assert _staged(context, "agents/main/agent.json")["fallback_models"] == [5]
    assert [(item.item, item.reason) for item in context.report.skipped] == [
        (
            "agents/main/agent.json",
            "invalid retired fallback_model 5 carried over into fallback_models "
            "for the application to report",
        )
    ]
    assert [(path, message) for _, path, message in _diagnostics(context)] == [
        ("$.fallback_models", "must be a list of strings")
    ]


def test_retired_settings_are_normalized_and_reported(tmp_path: Path) -> None:
    context = _context(tmp_path)
    _write(
        context.source,
        "settings.json",
        {
            "defaults": {"agent": {"model": "provider/main", "fallback_model": "provider/backup"}},
            "recall": {"backend": "canonical_scan"},
            "live_voice": {"enabled": False},
            "reflection": {
                "enabled": True,
                "skill_tool_call_interval": 20,
                "skill_model_step_interval": 8,
            },
            "future": {"kept": True},
        },
    )

    convert(context)

    assert _staged(context, "settings.json") == {
        "format_version": 1,
        "defaults": {"agent": {"model": "provider/main", "fallback_models": ["provider/backup"]}},
        "recall": {"backend": "sqlite_fts"},
        "reflection": {"enabled": True, "skill_model_step_interval": 8},
        "future": {"kept": True},
    }
    counts = context.report.counts[AREA]
    assert counts["fallback_model_converted"] == 1
    assert counts["recall_backend_replaced"] == 1
    assert counts["live_voice_dropped"] == 1
    assert counts["reflection_skill_tool_call_interval_dropped"] == 1
    assert [(item.item, item.reason) for item in context.report.skipped] == [
        (
            "settings.json",
            "recall.backend: retired canonical_scan replaced by sqlite_fts; "
            "a scan backend is no longer selectable",
        ),
        (
            "settings.json",
            "retired live_voice dropped; the Live voice control is available "
            "whenever the model_tasks.live_voice binding is set",
        ),
        (
            "settings.json",
            "retired reflection.skill_tool_call_interval dropped: Skill reviews now count "
            "Model steps instead of Tool calls; the existing skill_model_step_interval applies",
        ),
    ]
    assert [path for _, path, _ in _diagnostics(context)] == ["$.future"]


def test_retired_settings_without_successors_report_the_default(tmp_path: Path) -> None:
    context = _context(tmp_path)
    _write(
        context.source,
        "settings.json",
        {
            "defaults": {"agent": {"fallback_model": ""}},
            "recall": {"backend": "jsonl_scan"},
            "reflection": {"skill_tool_call_interval": 20},
        },
    )

    convert(context)

    assert _staged(context, "settings.json") == {
        "format_version": 1,
        "defaults": {"agent": {}},
        "recall": {"backend": "sqlite_fts"},
        "reflection": {},
    }
    assert context.report.counts[AREA]["fallback_model_dropped"] == 1
    assert [item.reason for item in context.report.skipped] == [
        "recall.backend: retired jsonl_scan replaced by sqlite_fts; "
        "a scan backend is no longer selectable",
        "retired reflection.skill_tool_call_interval dropped: Skill reviews now count Model "
        "steps instead of Tool calls; set reflection.skill_model_step_interval if its default "
        "does not fit",
    ]


def test_current_settings_values_are_kept(tmp_path: Path) -> None:
    context = _context(tmp_path)
    settings = {
        "defaults": {"agent": {"fallback_models": ["provider/backup"]}},
        "recall": {"backend": "hybrid"},
        "reflection": {"skill_model_step_interval": 8},
    }
    _write(context.source, "settings.json", settings)

    convert(context)

    assert _staged(context, "settings.json") == {"format_version": 1, **settings}
    assert context.report.skipped == []


def test_retired_project_files_block_becomes_working_project_after_identity_runtime(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    _write(
        context.source,
        "agents/tester/prompts/layout.json",
        [
            {"id": "core:soul", "enabled": False, "source": "core"},
            {"id": "core:runtime", "enabled": False, "source": "core"},
            {"id": "core:tools", "enabled": True, "source": "core"},
            {"id": "core:project_files", "enabled": False, "source": "core", "future": 1},
        ],
    )
    _write(
        context.source,
        "prompts/layout.json",
        [
            {"id": "core:runtime"},
            {"id": "core:project_files", "enabled": True},
            {"id": "core:working_project", "enabled": False},
        ],
    )

    convert(context)

    assert _staged(context, "agents/tester/prompts/layout.json")["entries"] == [
        {"id": "core:soul", "enabled": False, "source": "core"},
        {"id": "core:runtime", "enabled": False, "source": "core"},
        {"id": "core:identity_runtime", "enabled": False, "source": "core"},
        {"id": "core:tools", "enabled": True, "source": "core"},
        {"id": "core:working_project", "enabled": False, "source": "core", "future": 1},
    ]
    # A layout already naming the successor keeps it; the retired entry goes.
    assert _staged(context, "prompts/layout.json")["entries"] == [
        {"id": "core:runtime"},
        {"id": "core:identity_runtime", "enabled": True, "source": "core"},
        {"id": "core:working_project", "enabled": False},
    ]
    counts = context.report.counts[AREA]
    assert counts["prompt_project_files_converted"] == 2
    assert counts["prompt_identity_runtime_added"] == 2
    assert context.report.skipped == []


def test_layout_without_the_retired_block_is_only_wrapped(tmp_path: Path) -> None:
    context = _context(tmp_path)
    # Without core:project_files nothing shows the layout predates the
    # core:runtime split, so the missing core:identity_runtime is not guessed.
    layout = [{"id": "core:runtime", "enabled": False}]
    _write(context.source, "prompts/layout.json", layout)

    convert(context)

    assert _staged(context, "prompts/layout.json")["entries"] == layout
    assert "prompt_identity_runtime_added" not in context.report.counts[AREA]


def test_project_without_tool_whitelist_gets_the_pre_generation_1_default(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    project = {"project_id": "a", "display_name": "A", "cwd": "/srv/a"}
    _write(context.source, "projects/a/project.json", project)
    _write(context.source, "projects/b/project.json", {**project, "allowed_tools": []})

    convert(context)

    # The list the pre-Generation-1 application applied, pinned independently of
    # the application's current default.
    assert _staged(context, "projects/a/project.json")["allowed_tools"] == [
        "read",
        "apply_patch",
        "search_files",
        "bash",
        "process",
        "terminal",
        "web_fetch",
        "web_search",
        "status",
        "subagent",
        "skill",
    ]
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
