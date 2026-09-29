"""Project RPCs: add, detect, show, list and set, plus the per-Project skill cache.

Team payloads and per-Agent Overrides live in ``test_project_methods_team.py``;
removal lives in ``test_project_methods_delete.py``.
"""

from __future__ import annotations

import asyncio
import logging
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
    skill_dir = repo / ".opencode" / "skills" / name
    skill_dir.mkdir(parents=True)
    skill_dir.joinpath("SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\nUse this skill.\n",
        encoding="utf-8",
    )


def _team(result: JsonObject) -> list[str]:
    return [member["agent_id"] for member in result["scan"]["team"]]


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
    # AGENTS.md is the first auto-load entry, so the convention file loads with
    # no extra configuration.
    assert project["auto_load"] == ["AGENTS.md"]
    assert project["allowed_tools"] == list(PROJECT_DEFAULT_ALLOWED_TOOLS)
    assert project["skills_bundled_enabled"] == []
    assert project["skills_global_enabled"] == []
    assert project["skills_project_disabled"] == []
    assert project["default_temperature"] == 0.4
    assert project["default_thinking_effort"] == "high"
    assert _team(result) == ["builder"]
    assert result["scan"]["report"]["clean"] is True
    assert state.runtime.projects.exists("vbot")


@pytest.mark.asyncio
async def test_add_derives_the_project_id_from_the_cwd_of_a_bare_repo(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "my-repo")

    result = await rpc_result(state, "project.add", cwd=str(repo))

    assert result["project"]["project_id"] == "my-repo"
    # Neither Agent format is present: the non-interactive default is opencode.
    assert result["project"]["source_format"] == "opencode"
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
@pytest.mark.parametrize(
    ("opencode", "explicit", "source_format", "team"),
    [
        # Exactly one format present: that one, silently.
        pytest.param(False, None, "claude", ["reviewer"], id="claude-only"),
        # Both present: the deterministic default is opencode.
        pytest.param(True, None, "opencode", ["builder"], id="both-default"),
        # An explicit choice wins over detection.
        pytest.param(True, "claude", "claude", ["reviewer"], id="both-explicit"),
    ],
)
async def test_add_detects_or_accepts_the_source_format(
    tmp_path: Path, opencode: bool, explicit: str | None, source_format: str, team: list[str]
) -> None:
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "repo", *(["builder.md"] if opencode else []))
    _write_claude_agent(repo, "reviewer.md", "reviewer")
    params = {"source_format": explicit} if explicit else {}

    result = await rpc_result(state, "project.add", cwd=str(repo), **params)

    assert result["project"]["source_format"] == source_format
    assert _team(result) == team
    assert state.runtime.projects.get("repo").source_format == source_format


@pytest.mark.asyncio
async def test_detect_reports_formats_and_context_files(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "mixed", "builder.md")
    _write_claude_agent(repo, "reviewer.md", "reviewer")
    _write_claude_agent(repo, "helper.md", "helper")
    (repo / "CLAUDE.md").write_text("# Claude\n", encoding="utf-8")

    found = await rpc_result(state, "project.detect", cwd=str(repo))
    # The add dialog calls this while the user types: never an error envelope.
    missing = await rpc_result(state, "project.detect", cwd=str(tmp_path / "nope"))

    assert found["cwd_exists"] is True
    assert found["formats"]["opencode"] == {"agents": 1, "skills": 0}
    assert found["formats"]["claude"] == {"agents": 2, "skills": 0}
    assert found["context_files"] == {"agents_md": False, "claude_md": "CLAUDE.md"}
    assert missing == {
        "cwd_exists": False,
        "formats": {},
        "context_files": {"agents_md": False, "claude_md": None},
    }


@pytest.mark.asyncio
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
        ("project.add", {"cwd": "{fresh}", "bogus": 1}, "invalid_request", "bogus"),
        (
            "project.add",
            {"cwd": "{fresh}", "source_format": "cursor"},
            "invalid_request",
            "source_format",
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
            {"default_temperature": 0.2, "default_thinking_effort": "low"},
            {"default_temperature": 0.2, "default_thinking_effort": "low"},
            id="temperature-and-thinking",
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
async def test_set_source_format_switches_team_without_restart(tmp_path: Path) -> None:
    # A format switch invalidates like a cwd change, so the returned scan (and any
    # later show) reflects the other format's team immediately.
    state, repo = await _vbot_state(tmp_path, "builder.md")
    _write_claude_agent(repo, "reviewer.md", "reviewer")

    switched = await rpc_result(state, "project.set", project_id="vbot", source_format="claude")
    shown = await rpc_result(state, "project.show", project_id="vbot")

    assert switched["project"]["source_format"] == "claude"
    assert _team(switched) == ["reviewer"]
    assert _team(shown) == ["reviewer"]


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
async def test_show_scans_the_repo_off_the_event_loop(tmp_path: Path) -> None:
    state, _repo = await _vbot_state(tmp_path, "builder.md")
    resolver = state.runtime.agent_resolver
    scan_project_report = resolver.scan_project_report
    entered = threading.Event()
    release = threading.Event()
    threads: list[int] = []

    def blocked_scan(*args: Any, **kwargs: Any) -> Any:
        threads.append(threading.get_ident())
        entered.set()
        release.wait(timeout=5)
        return scan_project_report(*args, **kwargs)

    resolver.scan_project_report = blocked_scan
    showing = asyncio.create_task(rpc_result(state, "project.show", project_id="vbot"))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        loop = asyncio.get_running_loop()
        ticked_at = loop.time()
        for _ in range(5):
            await asyncio.sleep(0.01)
        assert loop.time() - ticked_at < 1
        assert not showing.done()
    finally:
        release.set()

    result = await asyncio.wait_for(showing, timeout=5)
    assert _team(result) == ["builder"]
    assert threads and threading.get_ident() not in threads


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

    async def reload_skills_async() -> None:
        reload_calls.append(True)

    state.runtime.reload_skills_async = reload_skills_async
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
    # without a restart.
    assert reload_calls == [True]
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


# ---------------------------------------------------------------------------
# Per-Project skill cache over a real Runtime
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_project_skill_cache_follows_settings_and_repo_changes(tmp_path: Path) -> None:
    # The minimal ``_make_state`` runtime has no skill seam, so this one test starts
    # a real Runtime (about a second) to exercise the per-Project skill cache
    # behind ``skills_for`` and ``project_skill_names`` end to end.
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(Config(data_dir=tmp_path / "data"))
    runtime.start()
    try:
        state = SimpleNamespace(runtime=runtime)
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
    finally:
        await runtime.aclose()
