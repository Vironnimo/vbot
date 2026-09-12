"""Tests for project methods."""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.runtime.runtime import Runtime
from core.utils.config import Config
from server.rpc.errors import RpcError
from server.rpc.project_methods import (
    _add_project,
    _detect_project,
    _list_projects,
    _set_project,
    _show_project,
)
from tests.server.rpc.project_methods_test_support import (
    _make_repo,
    _make_state,
    _write_agent,
)


def _write_claude_agent(repo: Path, filename: str, name: str) -> None:
    agents_dir = repo / ".claude" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / filename).write_text(
        f"---\nname: {name}\ndescription: A Claude agent.\n---\nBody.\n", encoding="utf-8"
    )


def _build_started_runtime(tmp_path: Path) -> Runtime:
    """Start a real Runtime so the per-project skill cache is exercised end-to-end.

    The minimal ``_make_state`` runtime has no skill seam, so the skill-cache half
    of the open-time refresh needs the real runtime cache behind
    ``project_skill_names`` / ``invalidate_project_skills``.
    """
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(Config(data_dir=tmp_path / "data"))
    runtime.start()
    return runtime


def _write_project_skill(repo: Path, name: str, description: str) -> None:
    """Write a project-owned skill under ``<repo>/.opencode/skills/<name>/``."""
    skill_dir = repo / ".opencode" / "skills" / name
    skill_dir.mkdir(parents=True)
    skill_dir.joinpath("SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\nUse this skill.\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# add: create + scan preview.
# ---------------------------------------------------------------------------
def test_add_creates_project_and_returns_scan_preview(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "vbot", "builder.md")

    result = _add_project(state, {"cwd": str(repo), "display_name": "vBot"})

    assert result["project"]["project_id"] == "vbot"
    assert result["project"]["cwd_exists"] is True
    assert [member["agent_id"] for member in result["scan"]["team"]] == ["builder"]
    assert result["scan"]["report"]["clean"] is True
    assert state.runtime.projects.exists("vbot")


def test_add_seeds_agents_file_into_auto_load(tmp_path: Path) -> None:
    # project.add seeds AGENTS.md as the first auto-load entry, so a freshly added
    # project loads the convention file with no extra configuration.
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "vbot", "builder.md")

    result = _add_project(state, {"cwd": str(repo), "display_name": "vBot"})

    assert result["project"]["auto_load"] == ["AGENTS.md"]


def test_add_derives_project_id_from_cwd_when_no_display_name(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "my-repo")

    result = _add_project(state, {"cwd": str(repo)})

    assert result["project"]["project_id"] == "my-repo"
    assert result["scan"]["team"] == []
    assert result["scan"]["report"]["clean"] is True


def test_add_report_flags_unconfigured_model(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "vbot")
    _write_agent(repo, "weird.md", model="ghost/model-x")

    result = _add_project(state, {"cwd": str(repo), "display_name": "vBot"})

    findings = result["scan"]["report"]["findings"]
    assert result["scan"]["report"]["clean"] is False
    assert any(finding["type"] == "bad_model" for finding in findings)


def test_add_rejects_missing_cwd(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    missing = tmp_path / "nope"

    with pytest.raises(RpcError) as exc_info:
        _add_project(state, {"cwd": str(missing), "display_name": "vBot"})

    assert exc_info.value.code == "invalid_request"
    assert str(missing) in exc_info.value.message


def test_add_rejects_duplicate_cwd(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "vbot")
    _add_project(state, {"cwd": str(repo), "display_name": "vBot"})

    with pytest.raises(RpcError) as exc_info:
        _add_project(state, {"cwd": str(repo), "display_name": "vBot Two"})

    assert exc_info.value.code == "project_already_exists"


def test_add_rejects_unslugifiable_display_name(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "vbot")

    with pytest.raises(RpcError) as exc_info:
        _add_project(state, {"cwd": str(repo), "display_name": "!!!"})

    assert exc_info.value.code == "invalid_request"


def test_add_rejects_unknown_field(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "vbot")

    with pytest.raises(RpcError) as exc_info:
        _add_project(state, {"cwd": str(repo), "bogus": 1})
    assert exc_info.value.code == "invalid_request"
    assert "bogus" in exc_info.value.message


# ---------------------------------------------------------------------------
# source format: auto-detection at add, explicit set, switch, detect.
# ---------------------------------------------------------------------------
def test_add_auto_detects_claude_only_repo(tmp_path: Path) -> None:
    # No explicit source_format + exactly one format present → that one, silently.
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "claude-repo")
    _write_claude_agent(repo, "reviewer.md", "reviewer")

    result = _add_project(state, {"cwd": str(repo)})

    assert result["project"]["source_format"] == "claude"
    assert [member["agent_id"] for member in result["scan"]["team"]] == ["reviewer"]
    assert state.runtime.projects.get("claude-repo").source_format == "claude"


def test_add_defaults_to_opencode_when_both_formats_present(tmp_path: Path) -> None:
    # Deterministic non-interactive default (decision 2): both present → opencode.
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "mixed", "builder.md")
    _write_claude_agent(repo, "reviewer.md", "reviewer")

    result = _add_project(state, {"cwd": str(repo)})

    assert result["project"]["source_format"] == "opencode"
    assert [member["agent_id"] for member in result["scan"]["team"]] == ["builder"]


def test_add_defaults_to_opencode_when_neither_format_present(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "bare")

    result = _add_project(state, {"cwd": str(repo)})

    assert result["project"]["source_format"] == "opencode"


def test_add_accepts_explicit_source_format(tmp_path: Path) -> None:
    # An explicit choice wins over auto-detection.
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "mixed", "builder.md")
    _write_claude_agent(repo, "reviewer.md", "reviewer")

    result = _add_project(state, {"cwd": str(repo), "source_format": "claude"})

    assert result["project"]["source_format"] == "claude"
    assert [member["agent_id"] for member in result["scan"]["team"]] == ["reviewer"]


def test_add_rejects_unknown_source_format(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "vbot")

    with pytest.raises(RpcError) as exc_info:
        _add_project(state, {"cwd": str(repo), "source_format": "cursor"})

    assert exc_info.value.code == "invalid_request"
    assert "source_format" in exc_info.value.message


def test_set_source_format_switches_team_without_restart(tmp_path: Path) -> None:
    # A format switch invalidates like a cwd change, so the returned scan (and any
    # later show) reflects the other format's team immediately.
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "mixed", "builder.md")
    _write_claude_agent(repo, "reviewer.md", "reviewer")
    _add_project(state, {"cwd": str(repo)})

    switched = _set_project(state, {"project_id": "mixed", "source_format": "claude"})

    assert switched["project"]["source_format"] == "claude"
    assert [member["agent_id"] for member in switched["scan"]["team"]] == ["reviewer"]
    shown = _show_project(state, {"project_id": "mixed"})
    assert [member["agent_id"] for member in shown["scan"]["team"]] == ["reviewer"]


def test_detect_reports_formats_and_context_files(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "mixed", "builder.md")
    _write_claude_agent(repo, "reviewer.md", "reviewer")
    _write_claude_agent(repo, "helper.md", "helper")
    (repo / "CLAUDE.md").write_text("# Claude\n", encoding="utf-8")

    result = _detect_project(state, {"cwd": str(repo)})

    assert result["cwd_exists"] is True
    assert result["formats"]["opencode"] == {"agents": 1, "skills": 0}
    assert result["formats"]["claude"] == {"agents": 2, "skills": 0}
    assert result["context_files"] == {"agents_md": False, "claude_md": "CLAUDE.md"}


def test_detect_nonexistent_cwd_is_success_with_empty_data(tmp_path: Path) -> None:
    # The add dialog calls this while the user types — never an error envelope.
    state = _make_state(tmp_path)

    result = _detect_project(state, {"cwd": str(tmp_path / "nope")})

    assert result == {
        "cwd_exists": False,
        "formats": {},
        "context_files": {"agents_md": False, "claude_md": None},
    }


def test_detect_rejects_unknown_field(tmp_path: Path) -> None:
    state = _make_state(tmp_path)

    with pytest.raises(RpcError) as exc_info:
        _detect_project(state, {"cwd": str(tmp_path), "bogus": 1})
    assert exc_info.value.code == "invalid_request"
    assert "bogus" in exc_info.value.message


# ---------------------------------------------------------------------------
# show / list.
# ---------------------------------------------------------------------------
def test_show_returns_config_team_and_report(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "vbot", "builder.md", "tester.md")
    _add_project(state, {"cwd": str(repo), "display_name": "vBot"})

    result = _show_project(state, {"project_id": "vbot"})

    assert result["project"]["project_id"] == "vbot"
    assert [member["agent_id"] for member in result["scan"]["team"]] == ["builder", "tester"]


def test_show_rescans_repo_changes(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "vbot", "builder.md")
    _add_project(state, {"cwd": str(repo), "display_name": "vBot"})
    _write_agent(repo, "tester.md")

    result = _show_project(state, {"project_id": "vbot"})

    assert [member["agent_id"] for member in result["scan"]["team"]] == ["builder", "tester"]


def test_show_reflects_a_newly_added_repo_skill(tmp_path: Path) -> None:
    # Open re-scans the Team on every call; the skill pool must keep pace. A skill
    # newly added under <cwd>/.opencode/skills surfaces after project.show in both
    # the editor pool and the resolver's effective-skills input — not only after a
    # cwd change or a restart.
    runtime = _build_started_runtime(tmp_path)
    state = SimpleNamespace(runtime=runtime)
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_project_skill(repo, "alpha", "Alpha playbook.")
    runtime.projects.create("p", "P", repo)

    # The first open primes the per-project skill cache against the current repo.
    primed = _show_project(state, {"project_id": "p"})
    assert primed["scan"]["skills"]["project"] == [
        {"name": "alpha", "description": "Alpha playbook."},
    ]

    # A new project skill lands in the repo after that first open.
    _write_project_skill(repo, "beta", "Beta playbook.")

    refreshed = _show_project(state, {"project_id": "p"})

    # The editor pool reflects the new skill (name + description carried per entry)...
    assert refreshed["scan"]["skills"]["project"] == [
        {"name": "alpha", "description": "Alpha playbook."},
        {"name": "beta", "description": "Beta playbook."},
    ]
    # ...and so does project_skill_names, which is exactly what the resolver feeds
    # into a config agent's effective skills, so the next resolve sees it too.
    assert runtime.project_skill_names("p") == frozenset({"alpha", "beta"})


def test_show_drops_team_cache_so_a_new_repo_agent_resolves(tmp_path: Path) -> None:
    # Open drops the Team cache together with the skill cache, so an agent added to
    # the repo after an earlier run resolves on the next run instead of being
    # rejected by a stale Team cache.
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "vbot", "builder.md")
    _add_project(state, {"cwd": str(repo), "display_name": "vBot"})
    resolver = state.runtime.agent_resolver
    # An earlier run caches the Team (builder only).
    resolver.resolve_agent("vbot", "builder")
    # A new agent is added to the repo afterwards.
    _write_agent(repo, "tester.md")

    _show_project(state, {"project_id": "vbot"})

    assert resolver.resolve_agent("vbot", "tester").id == "tester"


def test_show_unknown_project_errors(tmp_path: Path) -> None:
    state = _make_state(tmp_path)

    with pytest.raises(RpcError) as exc_info:
        _show_project(state, {"project_id": "ghost"})

    assert exc_info.value.code == "project_not_found"


def test_list_returns_projects(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    _add_project(state, {"cwd": str(_make_repo(tmp_path, "alpha")), "display_name": "Alpha"})
    _add_project(state, {"cwd": str(_make_repo(tmp_path, "beta")), "display_name": "Beta"})

    result = _list_projects(state, {})

    assert [project["project_id"] for project in result["projects"]] == ["alpha", "beta"]


# ---------------------------------------------------------------------------
# set: mutate + re-scan on cwd change.
# ---------------------------------------------------------------------------
def test_set_changes_default_model(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "vbot", "builder.md")
    _add_project(state, {"cwd": str(repo), "display_name": "vBot"})

    result = _set_project(state, {"project_id": "vbot", "default_model": "openai/gpt-mini"})

    assert result["project"]["default_model"] == "openai/gpt-mini"


def test_set_clears_display_name_to_project_id(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "vbot")
    _add_project(state, {"cwd": str(repo), "display_name": "vBot"})

    result = _set_project(state, {"project_id": "vbot", "display_name": None})

    assert result["project"]["display_name"] == "vbot"


def test_set_cwd_rescans_team(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    old_repo = _make_repo(tmp_path, "vbot", "builder.md")
    new_repo = _make_repo(tmp_path, "vbot-moved", "builder.md", "tester.md")
    _add_project(state, {"cwd": str(old_repo), "display_name": "vBot"})

    result = _set_project(state, {"project_id": "vbot", "cwd": str(new_repo)})

    assert [member["agent_id"] for member in result["scan"]["team"]] == ["builder", "tester"]


def test_set_rejects_missing_cwd(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    _add_project(state, {"cwd": str(_make_repo(tmp_path, "vbot")), "display_name": "vBot"})

    with pytest.raises(RpcError) as exc_info:
        _set_project(state, {"project_id": "vbot", "cwd": str(tmp_path / "nope")})

    assert exc_info.value.code == "invalid_request"


def test_set_requires_a_change(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    _add_project(state, {"cwd": str(_make_repo(tmp_path, "vbot")), "display_name": "vBot"})

    with pytest.raises(RpcError) as exc_info:
        _set_project(state, {"project_id": "vbot"})
    assert exc_info.value.code == "invalid_request"
