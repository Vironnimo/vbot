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
    events: list[tuple[str, JsonObject]] = []
    return SimpleNamespace(
        runtime=SimpleNamespace(
            agents=agents,
            agent_resolver=resolver,
            chat_sessions=sessions,
            projects=projects,
            storage=StorageManager(data_dir),
        ),
        # What the RPCs published, as (event, payload).
        events=events,
        event_bus=SimpleNamespace(publish=lambda event, payload: events.append((event, payload))),
    )


def _posix(path: Path) -> str:
    return path.as_posix()


# The stored form of the Full access preset: every part set, nothing restricted.
_FULL_ACCESS: JsonObject = {
    "read": "anywhere",
    "write": "anywhere",
    "commands": "any",
    "web": "any",
    "allow": [],
    "deny": [],
}


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
    # Beside the rule an editor shows only the rule and the reason, without the field path.
    assert result["errors"][2]["reason"].startswith('"Foo(x)": unknown kind Foo')
    assert result["errors"][1]["reason"].startswith('Use "anywhere" or "work_area"')
    # The presets come along as the change each makes (None unsets a part), so an
    # editor applies them without its own copy.
    assert result["presets"] == {
        "full_access": _FULL_ACCESS,
        "work_dir": {"read": "work_area", "write": "work_area", "commands": None, "web": None},
        "work_dir_read_only": {
            "read": "work_area",
            "write": "scratch",
            "commands": None,
            "web": None,
        },
    }
    # A part that is no valid base counts as not set while the editor shows the bases.
    assert result["bases"]["read"] == {
        "value": "anywhere",
        "source": "default",
        "inherit": {"value": "anywhere", "source": "default"},
        "choices": ["anywhere", "work_area"],
        "excluded": [],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("rules", "preset"),
    [
        pytest.param(None, "full_access", id="no-rules"),
        pytest.param(_FULL_ACCESS, "full_access", id="explicit-full-access"),
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
    # Each base with what applies and its source; write stays within the inherited read.
    bases = result["bases"]
    assert [(bases[part]["value"], bases[part]["source"]) for part in bases] == [
        ("work_area", "inherited"),
        ("work_area", "own"),
        ("any", "default"),
        ("any", "default"),
    ]
    assert (bases["write"]["inherit"], bases["write"]["excluded"]) == (
        {"value": "work_area", "source": "default"},
        ["anywhere"],
    )
    assert bases["write"]["narrowed_by_read"] == {"work_area": {"anywhere": "work_area"}}
    preview = result["preview"]
    root = _posix(workspace.resolve())
    home = _posix(Path.home())
    assert preview["workdir"] == root
    assert preview["read"] == [root, "{scratch}", f"{home}/x", f"{root}/docs/**"]
    assert preview["write"] == [root, "{scratch}", f"{root}/docs/**"]
    assert (preview["commands"], preview["web"]) == (None, None)
    assert preview["deny"] == [f"Read({root}/.env)"]


@pytest.mark.asyncio
async def test_explicit_full_access_overrides_a_restrictive_default(tmp_path: Path) -> None:
    state = _state(tmp_path, {"permission_rules": {"read": "work_area", "deny": ["Read(.env)"]}})
    state.runtime.agents.create("coder", "Coder")

    result = await rpc_result(
        state, "permissions.validate", agent_id="coder", permission_rules=_FULL_ACCESS
    )

    assert (result["preset"], result["inherited_preset"]) == ("full_access", "custom")
    assert (result["permission_rules"], result["effective"]) == (_FULL_ACCESS, None)
    assert result["preview"] is None


@pytest.mark.asyncio
async def test_validate_for_a_new_agent_inherits_the_shared_default(tmp_path: Path) -> None:
    state = _state(tmp_path, {"permission_rules": {"read": "work_area", "write": "work_area"}})

    result = await rpc_result(
        state, "permissions.validate", new_agent=True, permission_rules={"write": "scratch"}
    )

    assert result["inherited"] == {"read": "work_area", "write": "work_area"}
    assert (result["preset"], result["inherited_preset"]) == ("work_dir_read_only", "work_dir")
    # The Agent has no folders yet, so the preview keeps their placeholders.
    assert result["preview"]["read"] == ["{workdir}", "{scratch}"]


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
    # The preset of the resulting rules, not of the override alone.
    assert (result["preset"], result["inherited_preset"]) == ("work_dir_read_only", "custom")
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
        pytest.param(
            {"permission_rules": None, "new_agent": True, "agent_id": "coder"},
            "new_agent",
            id="new-agent-with-agent",
        ),
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
    state.runtime.agents.create("free", "Free", permission_rules=_FULL_ACCESS)
    state.runtime.chat_sessions.create("coder", session_id="s1")

    for agent_id, params in (("coder", {"session_id": "s1"}), ("coder", {}), ("free", {})):
        result = await rpc_result(state, "permissions.session", agent_id=agent_id, **params)

        assert result == {"restricted": False, "layers": [], "stored_ceiling": False}


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
    assert result["stored_ceiling"] is True

    # The user lifts the inherited ceiling; the Agent's own rules stay.
    cleared = await rpc_result(
        state, "permissions.clear_session_ceiling", agent_id="coder", session_id="s1"
    )
    repeated = await rpc_result(
        state, "permissions.clear_session_ceiling", agent_id="coder", session_id="s1"
    )
    after = await rpc_result(state, "permissions.session", agent_id="coder", session_id="s1")

    assert (cleared, repeated) == ({"cleared": True}, {"cleared": False})
    # Only the change tells open views of the Session to reload it.
    assert [payload["kind"] for _event, payload in state.events] == ["sessions"]
    assert [layer["source"] for layer in after["layers"]] == ["agent"]
    assert after["stored_ceiling"] is False
