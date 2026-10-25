"""Project RPCs: add, detect, show, list and set, plus the per-Project skill cache.

Team payloads and per-Agent Overrides live in ``test_project_methods_team.py``;
removal lives in ``test_project_methods_delete.py``.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.projects.projects import PROJECT_DEFAULT_ALLOWED_TOOLS
from core.runtime.runtime import Runtime
from core.utils.config import Config
from server.events import ServerEventBus
from tests.server.rpc.project_methods_test_support import _make_repo, _make_state, _write_agent
from tests.server.rpc_test_support import JsonObject, resource_changes, rpc_error, rpc_result


def _write_claude_agent(repo: Path, filename: str, name: str) -> None:
    agents_dir = repo / ".claude" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / filename).write_text(
        f"---\nname: {name}\ndescription: A Claude agent.\n---\nBody.\n", encoding="utf-8"
    )


def _write_project_skill(repo: Path, name: str, description: str) -> None:
    """Write a project-owned skill under ``<repo>/.opencode/skills/<name>/``."""
    _write_skill(repo / ".opencode" / "skills", name, description)


def _write_skill(folder: Path, name: str, description: str) -> None:
    skill_dir = folder / name
    skill_dir.mkdir(parents=True)
    skill_dir.joinpath("SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\nUse this skill.\n",
        encoding="utf-8",
    )


def _team(result: JsonObject) -> list[str]:
    return [member["agent_id"] for member in result["scan"]["team"]]


@pytest.mark.asyncio
async def _vbot_state(tmp_path: Path, *agents: str, **fields: Any) -> tuple[SimpleNamespace, Path]:
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "vbot", *agents)
    await rpc_result(state, "project.add", cwd=str(repo), display_name="vBot", **fields)
    return state, repo


# ---------------------------------------------------------------------------
# project.add / project.detect
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_creates_the_project_with_seeded_defaults_and_a_scan_preview(
    tmp_path: Path,
) -> None:
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "vbot", "builder.md")
    (repo / "CLAUDE.md").write_text("Instructions", encoding="utf-8")

    result = await rpc_result(
        state,
        "project.add",
        cwd=str(repo),
        display_name="vBot",
        default_temperature=0.4,
        default_thinking_effort="high",
    )

    project = result["project"]
    assert project["project_id"] == "vbot"
    assert project["cwd_exists"] is True
    assert project["memory_enabled"] is True
    # The repository's instruction file loads with no configuration.
    assert (project["auto_instructions"], project["folder_instructions"]) == (True, True)
    assert project["auto_load"] == []
    assert project["instruction_status"] == {"main_file": "CLAUDE.md", "missing": []}
    assert project["allowed_tools"] == list(PROJECT_DEFAULT_ALLOWED_TOOLS)
    assert project["skills_bundled_enabled"] == []
    assert project["skills_global_enabled"] == []
    assert project["skills_project_disabled"] == []
    assert project["default_temperature"] == 0.4
    assert project["default_thinking_effort"] == "high"
    assert _team(result) == ["builder"]
    assert result["scan"]["report"]["clean"] is True
    assert state.runtime.projects.exists("vbot")

    changed = await rpc_result(
        state,
        "project.set",
        project_id="vbot",
        auto_instructions=False,
        folder_instructions=False,
        memory_enabled=False,
        auto_load=["NOTES.md"],
        context_commands=[" git status --short ", "git status --short"],
    )
    project = changed["project"]
    assert (project["auto_instructions"], project["folder_instructions"]) == (False, False)
    assert project["memory_enabled"] is False
    assert project["context_commands"] == ["git status --short"]
    # The main file is still reported while off; an additional file that does
    # not exist is listed as missing.
    assert project["instruction_status"] == {"main_file": "CLAUDE.md", "missing": ["NOTES.md"]}


@pytest.mark.asyncio
async def test_add_derives_the_project_id_from_the_cwd_of_a_bare_repo(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "my-repo")

    result = await rpc_result(state, "project.add", cwd=str(repo))

    assert result["project"]["project_id"] == "my-repo"
    # Neither Agent format is present: the non-interactive default is opencode.
    assert result["project"]["sources"] == []
    assert result["scan"]["team"] == []
    assert result["scan"]["report"]["clean"] is True


@pytest.mark.asyncio
async def test_add_report_flags_unconfigured_model(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "vbot")
    _write_agent(repo, "weird.md", model="ghost/model-x")

    result = await rpc_result(state, "project.add", cwd=str(repo), display_name="vBot")

    assert result["scan"]["report"]["clean"] is False
    assert any(finding["type"] == "bad_model" for finding in result["scan"]["report"]["findings"])


@pytest.mark.asyncio
async def test_add_detects_mixed_sources(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "repo")
    _write_agent(repo, "builder.md")
    _write_claude_agent(repo, "reviewer.md", "reviewer")
    # An instruction file is no Source: the main instruction file loads on its own.
    (repo / "CLAUDE.md").write_text("Instructions", encoding="utf-8")
    detected = await rpc_result(state, "project.detect", cwd=str(repo))
    assert {item["id"] for item in detected["sources"]} == {"opencode.agents", "claude.agents"}
    result = await rpc_result(state, "project.add", cwd=str(repo))
    assert [member["agent_id"] for member in result["scan"]["team"]] == ["builder", "reviewer"]
    assert await rpc_result(state, "project.detect", cwd=str(tmp_path / "missing")) == {
        "cwd_exists": False,
        "sources": [],
    }


@pytest.mark.parametrize(
    ("method", "params", "code", "named"),
    [
        (
            "project.add",
            {"cwd": "{repo}", "display_name": "vBot Two"},
            "project_already_exists",
            "",
        ),
        ("project.add", {"cwd": "{missing}"}, "invalid_request", "{missing}"),
        ("project.add", {"cwd": "{fresh}", "display_name": "!!!"}, "invalid_request", ""),
        # The derived id would name a Windows device; another display_name helps.
        ("project.add", {"cwd": "{fresh}", "display_name": "Aux"}, "invalid_request", "'Aux'"),
        ("project.add", {"cwd": "{fresh}", "bogus": 1}, "invalid_request", "bogus"),
        (
            "project.add",
            {"cwd": "{fresh}", "sources": "cursor"},
            "invalid_request",
            "sources",
        ),
        ("project.add", {"cwd": "{fresh}", "default_temperature": 3.0}, "invalid_request", ""),
        ("project.detect", {"cwd": "{fresh}", "bogus": 1}, "invalid_request", "bogus"),
        ("project.show", {"project_id": "ghost"}, "project_not_found", ""),
        # Ids are exact even where the filesystem would open ``vbot`` for ``VBOT``.
        ("project.show", {"project_id": "VBOT"}, "project_not_found", ""),
        ("project.set", {"project_id": "vbot"}, "invalid_request", ""),
        ("project.set", {"project_id": "vbot", "cwd": "{missing}"}, "invalid_request", ""),
        (
            "project.set",
            {"project_id": "vbot", "allowed_tools": ["read", 7]},
            "invalid_request",
            "",
        ),
        (
            "project.set",
            {"project_id": "vbot", "allowed_tools": ["read", "*"]},
            "invalid_request",
            "",
        ),
        (
            "project.set",
            {"project_id": "vbot", "allowed_tools": ["read", "missing_extension_tool"]},
            "invalid_request",
            "missing_extension_tool",
        ),
        # Registered, but not eligible for Projects.
        (
            "project.set",
            {"project_id": "vbot", "allowed_tools": ["read", "memory"]},
            "invalid_request",
            "memory",
        ),
        (
            "project.set",
            {"project_id": "vbot", "default_thinking_effort": "ultra"},
            "invalid_request",
            "",
        ),
        ("project.set", {"project_id": "vbot", "default_temperature": 3.0}, "invalid_request", ""),
        # The retired override method is gone from the method table.
        (
            "project.clear_model_override",
            {"project_id": "vbot", "agent_id": "builder"},
            "method_not_found",
            "",
        ),
    ],
)
@pytest.mark.asyncio
async def test_a_refused_project_request_leaves_the_projects_unchanged(
    tmp_path: Path, method: str, params: JsonObject, code: str, named: str
) -> None:
    state, repo = await _vbot_state(tmp_path, "builder.md")
    paths = {
        "repo": str(repo),
        "fresh": str(_make_repo(tmp_path, "fresh")),
        "missing": str(tmp_path / "nope"),
    }
    params = {
        key: value.format(**paths) if isinstance(value, str) else value
        for key, value in params.items()
    }
    before = state.runtime.projects.list()

    error = await rpc_error(state, method, **params)

    assert error["code"] == code
    assert named.format(**paths) in error["message"]
    assert state.runtime.projects.list() == before


# ---------------------------------------------------------------------------
# project.set
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        pytest.param(
            {"default_model": "openai/gpt-mini"}, {"default_model": "openai/gpt-mini"}, id="model"
        ),
        pytest.param({"display_name": None}, {"display_name": "vbot"}, id="clear-display-name"),
        pytest.param(
            {
                "allowed_tools": ["read", "search_files"],
                "skills_bundled_enabled": ["frontend-design"],
                "skills_global_enabled": ["pdf"],
                "skills_project_disabled": ["debugging"],
            },
            {
                "allowed_tools": ["read", "search_files"],
                "skills_bundled_enabled": ["frontend-design"],
                "skills_global_enabled": ["pdf"],
                "skills_project_disabled": ["debugging"],
            },
            id="whitelists",
        ),
        pytest.param({"allowed_tools": []}, {"allowed_tools": []}, id="empty-tool-whitelist"),
        pytest.param(
            {"allowed_tools": ["read", "extension_tool"]},
            {"allowed_tools": ["read", "extension_tool"]},
            id="registered-extension-tool",
        ),
        pytest.param(
            {"default_temperature": 0.2, "default_top_p": 0.9, "default_thinking_effort": "low"},
            {"default_temperature": 0.2, "default_top_p": 0.9, "default_thinking_effort": "low"},
            id="sampling-and-thinking",
        ),
        # "" (provider default) is a real value, distinct from null.
        pytest.param(
            {"default_thinking_effort": ""}, {"default_thinking_effort": ""}, id="provider-default"
        ),
        pytest.param(
            {"default_thinking_effort": None},
            {"default_thinking_effort": None},
            id="clear-thinking",
        ),
    ],
)
async def test_set_changes_project_fields(
    tmp_path: Path, changes: JsonObject, expected: JsonObject
) -> None:
    state, _repo = await _vbot_state(tmp_path, default_thinking_effort="high")
    state.runtime.tools.names.add("extension_tool")

    updated = await rpc_result(state, "project.set", project_id="vbot", **changes)
    shown = await rpc_result(state, "project.show", project_id="vbot")

    for field, value in expected.items():
        assert updated["project"][field] == value
        assert shown["project"][field] == value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("changes", "invalidated"),
    [
        pytest.param({"default_model": "openai/gpt-mini"}, ["projects"], id="unrelated-field"),
        pytest.param(
            {"skills_global_enabled": ["pdf"]}, ["projects", "skills"], id="skill-whitelist"
        ),
        # The display name labels the Project's Skills in the inventory.
        pytest.param({"display_name": "Renamed"}, ["projects", "skills"], id="skill-origin"),
        pytest.param({"skills_global_enabled": []}, ["projects"], id="unchanged-skill-field"),
    ],
)
async def test_set_invalidates_the_skill_inventory_only_when_a_skill_field_changes(
    tmp_path: Path, changes: JsonObject, invalidated: list[str]
) -> None:
    state, _repo = await _vbot_state(tmp_path)
    state.event_bus = ServerEventBus()

    await rpc_result(state, "project.set", project_id="vbot", **changes)

    assert [change["kind"] for change in resource_changes(state)] == invalidated


@pytest.mark.asyncio
async def test_a_stored_unavailable_tool_is_kept_and_reported_but_not_rejected(
    tmp_path: Path,
) -> None:
    state, _repo = await _vbot_state(tmp_path)
    state.runtime.projects.update("vbot", allowed_tools=["read", "disabled_extension_tool"])

    shown = await rpc_result(state, "project.show", project_id="vbot")
    edited = await rpc_result(
        state,
        "project.set",
        project_id="vbot",
        allowed_tools=["read", "search_files", "disabled_extension_tool"],
    )

    assert shown["project"]["allowed_tools"] == ["read", "disabled_extension_tool"]
    assert shown["scan"]["report"] == {
        "clean": False,
        "findings": [
            {
                "type": "unavailable_tool",
                "detail": (
                    "Tool Whitelist entry 'disabled_extension_tool' is not a currently "
                    "registered Project tool. It remains stored but grants no access "
                    "unless the tool becomes available again."
                ),
                "agent_id": "",
                "source_path": None,
            }
        ],
    }
    assert edited["project"]["allowed_tools"] == [
        "read",
        "search_files",
        "disabled_extension_tool",
    ]


@pytest.mark.asyncio
async def test_set_cwd_rescans_team(tmp_path: Path) -> None:
    state, _repo = await _vbot_state(tmp_path, "builder.md")
    moved = _make_repo(tmp_path, "vbot-moved", "builder.md", "tester.md")

    result = await rpc_result(state, "project.set", project_id="vbot", cwd=str(moved))

    assert _team(result) == ["builder", "tester"]


@pytest.mark.asyncio
async def test_set_sources_switches_team_without_restart(tmp_path: Path) -> None:
    # A format switch invalidates like a cwd change, so the returned scan (and any
    # later show) reflects the other format's team immediately.
    state, repo = await _vbot_state(tmp_path, "builder.md")
    _write_claude_agent(repo, "reviewer.md", "reviewer")

    switched = await rpc_result(
        state,
        "project.set",
        project_id="vbot",
        sources=[
            {"id": "claude.agents", "enabled": True},
            {"id": "opencode.agents", "enabled": False},
        ],
    )
    shown = await rpc_result(state, "project.show", project_id="vbot")

    assert switched["project"]["sources"] == [
        {"id": "claude.agents", "enabled": True},
        {"id": "opencode.agents", "enabled": False},
    ]
    assert _team(switched) == ["reviewer"]
    assert _team(shown) == ["reviewer"]


@pytest.mark.asyncio
async def test_set_adopts_skill_folders_inside_and_outside_the_repository(
    tmp_path: Path,
) -> None:
    # A folder inside the repository is stored relative to it, one elsewhere
    # absolute; a folder that is gone stays listed with a finding instead of
    # failing the Project.
    state, repo = await _vbot_state(tmp_path, "builder.md")
    _write_skill(repo / ".vbot/skills", "review", "Repository review.")
    outside = tmp_path / "shared-skills"
    _write_skill(outside, "review", "Shared review.")
    _write_skill(outside, "deploy", "Shared deploy.")

    added = await rpc_result(
        state,
        "project.set",
        project_id="vbot",
        sources=[
            {"id": f"folder:{repo / '.vbot' / 'skills'}", "enabled": True},
            {"id": f"folder:{outside}", "enabled": True},
        ],
    )
    stored = [source["id"] for source in added["project"]["sources"]]
    duplicate = await rpc_error(
        state,
        "project.set",
        project_id="vbot",
        sources=[*added["project"]["sources"], {"id": "folder:.vbot/skills/", "enabled": True}],
    )
    missing = await rpc_error(
        state,
        "project.set",
        project_id="vbot",
        sources=[{"id": "folder:nowhere", "enabled": True}],
    )
    shutil.rmtree(repo / ".vbot")
    gone = await rpc_result(state, "project.show", project_id="vbot")

    assert stored == ["folder:.vbot/skills", f"folder:{outside.resolve().as_posix()}"]
    assert [(row["kind"], row["detected"], row["skills"]) for row in added["scan"]["sources"]] == [
        ("skills", True, 1),
        ("skills", True, 2),
    ]
    assert "already a Source" in duplicate["message"]
    assert "not an existing directory: nowhere" in missing["message"]
    # Opening the Project appends its detected Agent Source behind the folders.
    assert [source["id"] for source in gone["project"]["sources"]][:2] == stored
    assert [row["detected"] for row in gone["scan"]["sources"]][:2] == [False, True]
    assert "Skill folder not found: .vbot/skills" in [
        finding["detail"] for finding in gone["scan"]["report"]["findings"]
    ]


# ---------------------------------------------------------------------------
# project.show / project.list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_show_rescans_repo_changes(tmp_path: Path) -> None:
    state, repo = await _vbot_state(tmp_path, "builder.md")
    _write_agent(repo, "tester.md")

    result = await rpc_result(state, "project.show", project_id="vbot")

    assert result["project"]["project_id"] == "vbot"
    assert _team(result) == ["builder", "tester"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "params", "writes"),
    [
        ("project.show", {"project_id": "vbot"}, False),
        ("project.add", {"display_name": "Other"}, True),
        ("project.set", {"project_id": "vbot", "display_name": "Renamed"}, True),
        (
            "project.set_override",
            {"project_id": "vbot", "agent_id": "builder", "field": "temperature", "value": 0.4},
            True,
        ),
    ],
)
async def test_project_requests_scan_and_write_off_the_event_loop(
    tmp_path: Path, method: str, params: JsonObject, writes: bool
) -> None:
    state, _repo = await _vbot_state(tmp_path, "builder.md")
    if method == "project.add":
        params = {**params, "cwd": str(_make_repo(tmp_path, "other", "builder.md"))}
    resolver = state.runtime.agent_resolver
    scan_project_report = resolver.scan_project_report
    projects = state.runtime.projects
    write_project = projects._write_project
    entered = threading.Event()
    release = threading.Event()
    threads: list[int] = []
    write_threads: list[int] = []

    def blocked_scan(*args: Any, **kwargs: Any) -> Any:
        threads.append(threading.get_ident())
        entered.set()
        release.wait(timeout=5)
        return scan_project_report(*args, **kwargs)

    def recorded_write(project: Any) -> None:
        write_threads.append(threading.get_ident())
        write_project(project)

    resolver.scan_project_report = blocked_scan
    projects._write_project = recorded_write
    requesting = asyncio.create_task(rpc_result(state, method, **params))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        loop = asyncio.get_running_loop()
        ticked_at = loop.time()
        for _ in range(5):
            await asyncio.sleep(0.01)
        assert loop.time() - ticked_at < 1
        assert not requesting.done()
    finally:
        release.set()

    result = await asyncio.wait_for(requesting, timeout=5)
    assert _team(result) == ["builder"]
    assert threads and threading.get_ident() not in threads
    assert bool(write_threads) is writes
    assert threading.get_ident() not in write_threads


@pytest.mark.asyncio
async def test_show_drops_team_cache_so_a_new_repo_agent_resolves(tmp_path: Path) -> None:
    # Open drops the Team cache together with the skill cache, so an agent added to
    # the repo after an earlier run resolves on the next run instead of being
    # rejected by a stale Team cache.
    state, repo = await _vbot_state(tmp_path, "builder.md")
    resolver = state.runtime.agent_resolver
    resolver.resolve_agent("vbot", "builder")
    _write_agent(repo, "tester.md")

    await rpc_result(state, "project.show", project_id="vbot")

    assert resolver.resolve_agent("vbot", "tester").id == "tester"


@pytest.mark.asyncio
async def test_show_reloads_skills_and_reports_the_editor_skill_pool(tmp_path: Path) -> None:
    state, _repo = await _vbot_state(tmp_path, "builder.md")
    reload_calls: list[bool] = []

    async def reload_skills_async(*, only_if_changed: bool = False) -> None:
        reload_calls.append(only_if_changed)

    state.runtime.reload_skills_async = reload_skills_async
    refreshed: list[Any] = []
    state.runtime.refresh_project_skills = refreshed.append
    # The Skill runtime owns the classification (shadowing, global vs. bundled);
    # the editor response projects each group to name and description.
    state.runtime.project_skill_pool = lambda _project_id: {
        "project": [
            SimpleNamespace(name="glossary", description="Maintain the glossary."),
            SimpleNamespace(name="refactoring", description="Refactor code safely."),
        ],
        "global": [SimpleNamespace(name="deploy", description="Deploy the app.")],
        "bundled": [SimpleNamespace(name="pdf", description="Work with PDFs.")],
    }

    result = await rpc_result(state, "project.show", project_id="vbot")

    # A show reloads the global registry, so a hand-dropped global skill surfaces
    # without a restart, and rescans the Project's own Skills from its Sources.
    assert reload_calls == [True]
    assert [project.project_id for project in refreshed] == ["vbot"]
    assert result["scan"]["skills"] == {
        "project": [
            {"name": "glossary", "description": "Maintain the glossary."},
            {"name": "refactoring", "description": "Refactor code safely."},
        ],
        "bundled": [{"name": "pdf", "description": "Work with PDFs."}],
        "global": [{"name": "deploy", "description": "Deploy the app."}],
    }


@pytest.mark.asyncio
async def test_list_returns_projects(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    await rpc_result(
        state, "project.add", cwd=str(_make_repo(tmp_path, "alpha")), display_name="Alpha"
    )
    await rpc_result(
        state, "project.add", cwd=str(_make_repo(tmp_path, "beta")), display_name="Beta"
    )

    result = await rpc_result(state, "project.list")

    assert [project["project_id"] for project in result["projects"]] == ["alpha", "beta"]
    assert all("scan" not in project for project in result["projects"])
    assert result["order_revision"] == 0


@pytest.mark.asyncio
async def test_reorder_stores_the_order_that_project_list_returns(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    for name in ("alpha", "beta"):
        await rpc_result(state, "project.add", cwd=str(_make_repo(tmp_path, name)))
    changes_before = len(resource_changes(state))

    reordered = await rpc_result(
        state, "project.reorder", project_ids=["beta", "alpha"], expected_revision=0
    )
    stale = await rpc_error(
        state, "project.reorder", project_ids=["alpha", "beta"], expected_revision=0
    )
    listed = await rpc_result(state, "project.list")

    assert [project["project_id"] for project in reordered["projects"]] == ["beta", "alpha"]
    assert reordered["order_revision"] == 1
    assert [change["kind"] for change in resource_changes(state)[changes_before:]] == ["projects"]
    assert stale["code"] == "project_order_conflict"
    assert [project["project_id"] for project in listed["projects"]] == ["beta", "alpha"]
    assert listed["order_revision"] == 1


@pytest.mark.asyncio
async def test_list_with_scan_reports_cached_teams_without_rescanning(tmp_path: Path) -> None:
    state, repo = await _vbot_state(tmp_path, "builder.md")
    await rpc_result(
        state, "project.add", cwd=str(_make_repo(tmp_path, "empty")), display_name="Empty"
    )
    resolver = state.runtime.agent_resolver
    await rpc_result(state, "project.list", include_scan=True)
    # Once cached, a repo change shows only after an open re-scans it.
    _write_agent(repo, "tester.md")
    scans: list[str] = []
    scan_project_report = resolver.scan_project_report

    def recorded_scan(project: Any) -> Any:
        scans.append(project.project_id)
        return scan_project_report(project)

    resolver.scan_project_report = recorded_scan

    listed = await rpc_result(state, "project.list", include_scan=True)

    by_id = {project["project_id"]: project for project in listed["projects"]}
    assert scans == []
    assert _team(by_id["vbot"]) == ["builder"]
    # The same Team + report shape as project.show, without its skill pool.
    assert "effective" in by_id["vbot"]["scan"]["team"][0]
    assert isinstance(by_id["vbot"]["scan"]["report"]["clean"], bool)
    assert _team(by_id["empty"]) == []
    assert "skills" not in by_id["vbot"]["scan"]

    await rpc_result(state, "project.show", project_id="vbot")
    relisted = await rpc_result(state, "project.list", include_scan=True)

    assert _team(next(p for p in relisted["projects"] if p["project_id"] == "vbot")) == [
        "builder",
        "tester",
    ]


# ---------------------------------------------------------------------------
# Per-Project skill cache over a real Runtime
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_project_skill_cache_follows_settings_and_repo_changes(tmp_path: Path) -> None:
    # The minimal ``_make_state`` runtime has no skill seam, so this one test starts
    # a real test-mode Runtime to exercise the per-Project skill cache behind
    # ``skills_for`` and ``project_skill_names`` end to end.
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(Config(data_dir=tmp_path / "data"), safe_startup_mode="test")
    runtime.start()
    try:
        state = SimpleNamespace(
            runtime=runtime,
            event_bus=ServerEventBus(),
            server_bind={"listen_host": "127.0.0.1", "listen_port": 8420, "port_source": "default"},
        )
        repo = _make_repo(tmp_path, "repo")
        _write_project_skill(repo, "project-playbook", "Project instructions")
        global_root = runtime.global_skills_dir / "global-playbook"
        global_root.mkdir(parents=True)
        (global_root / "SKILL.md").write_text(
            "---\nname: global-playbook\ndescription: Global instructions\n---\nBody.\n",
            encoding="utf-8",
        )
        runtime.reload_skills()
        runtime.projects.create("p", "P", repo)

        def allowed() -> set[str]:
            return {skill.name for skill in runtime.skills_for("p", "main").filter_allowed([])}

        initial = allowed()
        await rpc_result(
            state, "project.set", project_id="p", skills_project_disabled=["project-playbook"]
        )
        disabled = allowed()
        await rpc_result(
            state,
            "project.set",
            project_id="p",
            skills_project_disabled=[],
            skills_global_enabled=["global-playbook"],
        )
        opted_in = allowed()
        runtime.projects.create("other", "Other", _make_repo(tmp_path, "other"))
        unrelated = runtime.skills_for("other", "main")
        global_registry = runtime.skills
        # A skill added to the repo after the cache was primed surfaces on the next
        # open, in both the editor pool and the resolver's skill input.
        _write_project_skill(repo, "beta", "Beta playbook.")
        shown = await rpc_result(state, "project.show", project_id="p")

        assert initial == {"project-playbook"}
        assert disabled == set()
        assert opted_in == {"project-playbook", "global-playbook"}
        assert shown["scan"]["skills"]["project"] == [
            {"name": "beta", "description": "Beta playbook."},
            {"name": "project-playbook", "description": "Project instructions"},
        ]
        assert runtime.project_skill_names("p") == frozenset({"project-playbook", "beta"})
        # An unchanged global catalog must not invalidate unrelated scopes.
        assert runtime.skills is global_registry
        assert runtime.skills_for("other", "main") is unrelated
        await rpc_result(state, "project.show", project_id="p")
        assert runtime.skills_for("other", "main") is unrelated

        # A real external global edit still reaches both the editor and runtime
        # scopes when the Project opens, without an explicit global reload.
        (global_root / "SKILL.md").write_text(
            "---\nname: global-playbook\ndescription: Changed fixture\n---\nBody.\n",
            encoding="utf-8",
        )
        refreshed = await rpc_result(state, "project.show", project_id="p")
        assert refreshed["scan"]["skills"]["global"] == [
            {"name": "global-playbook", "description": "Changed fixture"}
        ]
        assert runtime.skills is not global_registry
        assert runtime.skills_for("other", "main") is not unrelated
        assert runtime.skills_for("other", "main").get("global-playbook").description == (
            "Changed fixture"
        )
    finally:
        await runtime.aclose()
