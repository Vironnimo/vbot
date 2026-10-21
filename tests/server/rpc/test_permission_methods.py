"""Permission Rules RPCs: editor validation and the limits a Session works under."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.agents.agents import AgentStore
from core.database import write_bootstrap_marker
from core.permissions import BoundaryContext, PermissionBoundary, normalize_permission_rules
from core.projects import AgentOverrides
from core.projects.resolver import AgentResolver, ModelConfigurationChecker
from core.projects.store import ProjectStore
from core.sessions import ChatSessionManager, SessionAddress
from core.storage import StorageManager
from tests.server.rpc_test_support import JsonObject, rpc_error, rpc_result


class _NoModels:
    def get(self, *_ids: str) -> Any:
        raise KeyError("no catalog")


class _NoCredentials:
    def has_credentials(self, *_ids: object) -> bool:
        return False

    def is_usable(self, *_ids: object) -> bool:
        return False


def _state(tmp_path: Path, defaults: JsonObject) -> SimpleNamespace:
    """Real Agent, Project and Session stores sharing one ``defaults.agent`` map."""
    templates = tmp_path / "templates"
    templates.mkdir()
    for filename in ("SOUL.md", "USER.md", "MEMORY.md"):
        (templates / filename).write_text(f"# {filename}\n", encoding="utf-8")
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    write_bootstrap_marker(data_dir)
    sessions = ChatSessionManager(data_dir)
    agents = AgentStore(
        data_dir, template_dir=templates, defaults_provider=lambda: defaults, sessions=sessions
    )
    projects = ProjectStore(data_dir)
    checker = ModelConfigurationChecker(_NoModels(), _NoModels(), _NoCredentials())
    resolver = AgentResolver(agents, projects, checker, lambda: defaults, sessions=sessions)
    return SimpleNamespace(
        runtime=SimpleNamespace(
            agents=agents,
            agent_resolver=resolver,
            chat_sessions=sessions,
            projects=projects,
            storage=StorageManager(data_dir),
        )
    )


def _posix(path: Path) -> str:
    return path.as_posix()


@pytest.mark.asyncio
async def test_validate_reports_every_problem_and_saves_nothing(tmp_path: Path) -> None:
    state = _state(tmp_path, {})

    result = await rpc_result(
        state,
        "permissions.validate",
        permission_rules={
            "read": "nowhere",
            "allow": ["Write(src/**)", "Foo(x)"],
            "colour": "blue",
        },
    )

    assert result["valid"] is False
    assert (result["permission_rules"], result["preset"], result["preview"]) == (None, None, None)
    assert [(error["part"], error["index"], error["rule"]) for error in result["errors"]] == [
        ("colour", None, None),
        ("read", None, None),
        ("allow", 1, "Foo(x)"),
    ]
    assert 'permission_rules.allow[1] "Foo(x)": unknown kind Foo' in result["errors"][2]["message"]
    # The presets come along, so an editor applies them without its own copy.
    assert result["presets"] == {
        "full_access": None,
        "work_dir": {"read": "work_area", "write": "work_area"},
        "work_dir_read_only": {"read": "work_area", "write": "scratch"},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("rules", "preset"),
    [
        pytest.param(None, "full_access", id="no-rules"),
        pytest.param({"read": "work_area", "write": "work_area"}, "work_dir", id="work-dir"),
        # Allow and deny rules do not change the preset a rule set matches.
        pytest.param(
            {"read": "work_area", "write": "scratch", "deny": ["Shell(git push *)"]},
            "work_dir_read_only",
            id="read-only-with-rules",
        ),
        pytest.param({"commands": "listed"}, "custom", id="custom"),
    ],
)
async def test_validate_names_the_matching_preset(
    tmp_path: Path, rules: JsonObject | None, preset: str
) -> None:
    result = await rpc_result(_state(tmp_path, {}), "permissions.validate", permission_rules=rules)

    assert (result["valid"], result["preset"]) == (True, preset)


@pytest.mark.asyncio
async def test_validate_for_an_agent_previews_its_new_sessions(tmp_path: Path) -> None:
    state = _state(tmp_path, {"permission_rules": {"read": "work_area", "deny": ["Read(.env)"]}})
    workspace = tmp_path / "workspace"
    state.runtime.agents.create("coder", "Coder")
    state.runtime.agents.update("coder", workspace=str(workspace))

    result = await rpc_result(
        state,
        "permissions.validate",
        agent_id="coder",
        permission_rules={"write": "work_area", "allow": ["Write({workdir}/docs/**)", "Read(~/x)"]},
    )

    assert result["valid"] is True
    assert result["notes"] == ["Write({workdir}/docs/**) was saved as Edit({workdir}/docs/**)."]
    assert result["permission_rules"] == {
        "write": "work_area",
        "allow": ["Edit({workdir}/docs/**)", "Read(~/x)"],
    }
    # The own rule set replaces the inherited parts it sets, part by part.
    assert result["inherited"] == {"read": "work_area", "deny": ["Read(.env)"]}
    assert result["effective"] == {
        "read": "work_area",
        "write": "work_area",
        "allow": ["Edit({workdir}/docs/**)", "Read(~/x)"],
        "deny": ["Read(.env)"],
    }
    preview = result["preview"]
    root = _posix(workspace.resolve())
    home = _posix(Path.home())
    assert preview["workdir"] == root
    assert preview["read"] == [root, "{scratch}", f"{home}/x", f"{root}/docs/**"]
    assert preview["write"] == [root, "{scratch}", f"{root}/docs/**"]
    assert (preview["commands"], preview["web"]) == (None, None)
    assert preview["deny"] == [f"Read({root}/.env)"]


@pytest.mark.asyncio
async def test_validate_for_a_team_member_inherits_and_works_in_the_repository(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path, {"permission_rules": {"write": "scratch"}})
    repo = tmp_path / "repo"
    agents_dir = repo / ".opencode" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "builder.md").write_text(
        "---\ndescription: Builds.\n---\nBody.\n", encoding="utf-8"
    )
    state.runtime.projects.create("site", "Site", repo)

    result = await rpc_result(
        state,
        "permissions.validate",
        agent_id="builder@site",
        permission_rules={"read": "work_area"},
    )

    assert result["inherited"] == {"write": "scratch"}
    assert result["preset"] == "custom"
    assert result["effective"] == {"read": "work_area", "write": "scratch"}
    assert result["preview"]["read"] == [_posix(repo.resolve()), "{scratch}"]
    assert result["preview"]["write"] == ["{scratch}"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("params", "named"),
    [
        pytest.param({}, "params.permission_rules is required", id="rules-missing"),
        pytest.param({"permission_rules": None, "agent_id": 5}, "agent_id", id="bad-agent"),
        pytest.param({"permission_rules": None, "scope": "x"}, "scope", id="unknown-param"),
    ],
)
async def test_validate_rejects_malformed_requests(
    tmp_path: Path, params: JsonObject, named: str
) -> None:
    error = await rpc_error(_state(tmp_path, {}), "permissions.validate", **params)

    assert error["code"] == "invalid_request"
    assert named in error["message"]


@pytest.mark.asyncio
async def test_an_unrestricted_session_reports_full_access(tmp_path: Path) -> None:
    state = _state(tmp_path, {})
    state.runtime.agents.create("coder", "Coder")
    state.runtime.chat_sessions.create("coder", session_id="s1")

    for params in ({"session_id": "s1"}, {}):
        result = await rpc_result(state, "permissions.session", agent_id="coder", **params)

        assert result == {"restricted": False, "layers": []}


@pytest.mark.asyncio
async def test_a_restricted_session_describes_its_rules_and_inherited_ceiling(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path, {})
    workspace = tmp_path / "workspace"
    state.runtime.agents.create("coder", "Coder", permission_rules={"read": "work_area"})
    state.runtime.agents.update("coder", workspace=str(workspace))
    state.runtime.chat_sessions.create("coder", session_id="s1")
    parent = PermissionBoundary.build(
        normalize_permission_rules({"write": "scratch"}),
        BoundaryContext(
            workdir=tmp_path / "parent",
            scratch_root=tmp_path / "parent-scratch",
            home=tmp_path,
            data_dir=tmp_path / "data",
        ),
    ).ceiling(source="parent")
    assert parent is not None
    stored = AgentOverrides(permission_ceiling=parent.to_ceiling_dict())

    def link(metadata: dict[str, Any]) -> None:
        metadata.update(stored.session_metadata())

    state.runtime.chat_sessions.mutate_metadata(SessionAddress(None, "coder", "s1"), link)

    result = await rpc_result(state, "permissions.session", agent_id="coder", session_id="s1")

    assert result["restricted"] is True
    own, inherited = result["layers"]
    root = _posix(workspace.resolve())
    assert (own["source"], own["read"], own["write"]) == ("agent", [root, "{scratch}"], None)
    assert inherited["source"] == "parent"
    assert inherited["read"] is None
    assert inherited["write"] == [_posix(tmp_path / "parent-scratch")]
