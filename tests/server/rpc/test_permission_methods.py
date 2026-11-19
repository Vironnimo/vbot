"""Permission Rules RPCs: editor validation and the limits a Session works under."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, override

import pytest

from core.agents.agents import AgentStore
from core.agents.temporary import TemporaryAgentConfig, TemporaryAgentRegistry
from core.database import write_bootstrap_marker
from core.permissions import BoundaryContext, PermissionBoundary, normalize_permission_rules
from core.projects import AgentOverrides
from core.projects.resolver import AgentResolver, ModelConfigurationChecker
from core.projects.store import ProjectStore
from core.sessions import ChatSessionManager, SessionAddress
from core.storage import StorageManager
from core.tools.availability import ToolAccess
from server.access import bound_principal
from server.access._principal import HOST
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
    temporary = TemporaryAgentRegistry(sessions)
    storage = StorageManager(data_dir)
    resolver = AgentResolver(
        agents,
        projects,
        checker,
        lambda: defaults,
        sessions=sessions,
        temporary_agents=temporary,
        permission_profiles=lambda: storage.load_settings().get("permission_profiles", {}),
    )
    events: list[tuple[str, JsonObject]] = []
    return SimpleNamespace(
        runtime=SimpleNamespace(
            agents=agents,
            agent_resolver=resolver,
            temporary_agents=temporary,
            chat_sessions=sessions,
            projects=projects,
            storage=storage,
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

        assert result == {
            "restricted": False,
            "layers": [],
            "stored_ceiling": False,
            "session_rules": None,
            "session_preset": None,
            "session_grants": [],
        }


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
    assert inherited["write"] == ["{scratch}"]
    assert result["stored_ceiling"] is True

    # Extension-owned Sessions describe their actual limits in full Chat too.
    binding = state.runtime.temporary_agents.create(
        owner_name="review",
        group_id="review",
        participant_id="worker",
        config=TemporaryAgentConfig(
            name="Reviewer",
            model="example/model",
            cwd=workspace,
            tool_access=ToolAccess(mode="none"),
            allowed_skills=[],
            tools={},
            permission_rules={"read": "work_area"},
        ),
    )
    temporary = await rpc_result(
        state,
        "permissions.session",
        agent_id=binding.address.agent_id,
        session_id=binding.address.session_id,
    )
    assert temporary["restricted"] is True
    assert temporary["layers"][0]["read"] == [root, "{scratch}"]

    # Only the user lifts the inherited ceiling; the Agent's own rules stay.
    refused = await rpc_error(
        state, "permissions.clear_session_ceiling", agent_id="coder", session_id="s1"
    )
    assert refused["code"] == "access_denied"
    with bound_principal(HOST):
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


@pytest.mark.asyncio
async def test_the_user_sets_a_sessions_own_rules(tmp_path: Path) -> None:
    state = _state(tmp_path, {})
    workspace = tmp_path / "workspace"
    state.runtime.agents.create(
        "coder", "Coder", permission_rules={"read": "work_area", "web": "listed"}
    )
    state.runtime.agents.update("coder", workspace=str(workspace))
    state.runtime.chat_sessions.create("coder", session_id="s1")
    state.runtime.chat_sessions.create("coder", session_id="s2")
    address = SessionAddress(None, "coder", "s1")
    resolver = state.runtime.agent_resolver
    params = {"agent_id": "coder", "session_id": "s1"}

    # An Agent's own call cannot widen its Session.
    refused = await rpc_error(
        state, "permissions.set_session_rules", **params, permission_rules=_FULL_ACCESS
    )
    assert refused["code"] == "access_denied"

    with bound_principal(HOST):
        invalid = await rpc_error(
            state, "permissions.set_session_rules", **params, permission_rules={"write": "nowhere"}
        )
        assert invalid["code"] == "invalid_request"
        invalid_presets: tuple[Any, ...] = ("root", {}, [], None)
        for preset in invalid_presets:
            unknown = await rpc_error(
                state, "permissions.set_session_rules", **params, preset=preset
            )
            assert unknown["code"] == "invalid_request"

        # Session rules replace whole parts of the Agent's rules, for this Session only.
        narrowed = await rpc_result(
            state,
            "permissions.set_session_rules",
            **params,
            permission_rules={"write": "work_area"},
        )
        assert narrowed["session_rules"]["write"] == "work_area"
        assert narrowed["ceiling_cleared"] is False
        rules = resolver.resolve_agent(None, "coder", session_id="s1").permission_rules
        assert (rules.read, rules.write, rules.web) == ("work_area", "work_area", "listed")
        assert resolver.resolve_agent(None, "coder", session_id="s2").permission_rules.write is None
        assert (
            resolver.effective_config(None, "coder", session_id="s1")["permission_rules"]["source"]
            == "session"
        )
        described = await rpc_result(state, "permissions.session", **params)
        assert described["session_rules"] == narrowed["session_rules"]

        # Full access lifts the Agent's rules and an inherited ceiling.
        parent = PermissionBoundary.build(
            normalize_permission_rules({"write": "scratch"}),
            BoundaryContext(workdir=tmp_path, home=tmp_path, data_dir=tmp_path / "data"),
        ).ceiling(source="parent")
        assert parent is not None
        stored = AgentOverrides(permission_ceiling=parent.to_ceiling_dict())

        def link(metadata: dict[str, Any]) -> None:
            metadata.update(stored.session_metadata())

        state.runtime.chat_sessions.mutate_metadata(address, link)
        full = await rpc_result(
            state, "permissions.set_session_rules", **params, preset="full_access"
        )
        assert (full["session_preset"], full["ceiling_cleared"]) == ("full_access", True)
        assert await rpc_result(state, "permissions.session", **params) == {
            "restricted": False,
            "layers": [],
            "stored_ceiling": False,
            "session_rules": full["session_rules"],
            "session_preset": "full_access",
            "session_grants": [],
        }

        # null gives the Session the Agent's rules again.
        reset = await rpc_result(
            state, "permissions.set_session_rules", **params, permission_rules=None
        )
        assert (reset["session_rules"], reset["session_preset"]) == (None, None)
        assert (await rpc_result(state, "permissions.session", **params))["restricted"] is True
    # Every change tells open views of the Session to reload it.
    assert [payload["kind"] for _event, payload in state.events] == ["sessions"] * 3


@pytest.mark.asyncio
async def test_approval_rpcs_project_pending_and_resolve_once() -> None:
    class Approvals:
        def approvals_snapshot(self) -> JsonObject:
            return {"revision": 2, "requests": [{"id": "approval-one"}]}

        def resolve_approval(self, request_id: str, decision: str) -> bool:
            return request_id == "approval-one" and decision == "allow"

    state = SimpleNamespace(chat_runs=Approvals())
    assert await rpc_result(state, "permissions.pending") == {
        "revision": 2,
        "requests": [{"id": "approval-one"}],
    }
    assert await rpc_result(state, "permissions.resolve", id="approval-one", decision="allow") == {
        "resolved": True
    }
    assert (await rpc_error(state, "permissions.resolve", id="gone", decision="reject"))[
        "code"
    ] == "approval_not_found"
    assert (await rpc_error(state, "permissions.resolve", id="approval-one", decision="always"))[
        "code"
    ] == "invalid_request"


@pytest.mark.asyncio
async def test_the_user_remembers_an_approval_for_the_session(tmp_path: Path) -> None:
    state = _state(tmp_path, {})
    state.runtime.agents.create(
        "coder", "Coder", permission_rules={"commands": "listed", "allow": ["Shell(ls *)"]}
    )
    state.runtime.chat_sessions.create("coder", session_id="s1")
    state.runtime.chat_sessions.create("coder", session_id="s2")
    resolved: list[tuple[str, str]] = []
    folder = (tmp_path / "client").as_posix()

    class Approvals:
        def approvals_snapshot(self) -> JsonObject:
            request = {"id": "approval-one", "project_id": None, "agent_id": "coder"}
            return {
                "revision": 1,
                "requests": [
                    {**request, "session_id": "s1", "session_grant": {"folders": [folder]}}
                ],
            }

        async def remember_approval(self, request_id, save, *, folder_read):
            await save(self.approvals_snapshot()["requests"][0])
            resolved.append((request_id, "allow"))
            return True

    state.chat_runs = Approvals()
    resolver = state.runtime.agent_resolver
    remember = {"id": "approval-one", "decision": "allow", "remember": ["Bash(git push:*)"]}

    # An Agent's own call cannot widen its Session.
    assert (await rpc_error(state, "permissions.resolve", **remember))["code"] == "access_denied"
    with bound_principal(HOST):
        for wrong in (
            {**remember, "decision": "reject"},
            {**remember, "remember": ["Shell(rm *)", "Read(/a)", 7]},
            {**remember, "remember": []},
        ):
            assert (await rpc_error(state, "permissions.resolve", **wrong))[
                "code"
            ] == "invalid_request"
        assert resolved == []

        # The grant is stored before the waiting call goes on, for this Session only.
        assert await rpc_result(state, "permissions.resolve", **remember) == {"resolved": True}
        assert resolved == [("approval-one", "allow")]
        allow = resolver.resolve_agent(None, "coder", session_id="s1").permission_rules.allow
        assert [rule.text for rule in allow] == ["Shell(ls *)", "Shell(git push *)"]
        other = resolver.resolve_agent(None, "coder", session_id="s2").permission_rules.allow
        assert [rule.text for rule in other] == ["Shell(ls *)"]
        params = {"agent_id": "coder", "session_id": "s1"}
        described = await rpc_result(state, "permissions.session", **params)
        assert described["session_grants"] == ["Shell(git push *)"]
        assert described["session_rules"] is None

        removed = await rpc_result(
            state, "permissions.remove_session_grants", **params, rules=["Shell(git push *)"]
        )
        assert removed == {"session_grants": []}
        allow = resolver.resolve_agent(None, "coder", session_id="s1").permission_rules.allow
        assert [rule.text for rule in allow] == ["Shell(ls *)"]
        choose = {"id": "approval-one", "decision": "allow", "read_folder": folder}
        assert (
            await rpc_error(
                state, "permissions.resolve", **{**choose, "read_folder": tmp_path.as_posix()}
            )
        )["code"] == "invalid_request"
        assert await rpc_result(state, "permissions.resolve", **choose) == {"resolved": True}
        assert (await rpc_result(state, "permissions.session", **params))[
            "session_read_folders"
        ] == [{"computer_id": "server", "folder": folder}]
        assert resolver.session_overrides(SessionAddress(None, "coder", "s2")).read_folders == {}
        assert await rpc_result(
            state, "permissions.remove_session_grants", **params, read_folder=folder
        ) == {"session_grants": []}
    assert [payload["kind"] for _event, payload in state.events] == ["sessions"] * 4


@pytest.mark.asyncio
async def test_operator_manages_shared_permission_profiles(tmp_path: Path) -> None:
    state = _state(tmp_path, {})
    state.agent_delete_lock = asyncio.Lock()
    state.runtime.models = _NoModels()
    save = {"name": "Project only", "permission_rules": {"read": "work_area"}, "base": None}
    assert (await rpc_error(state, "permissions.save_profile", **save))["code"] == "access_denied"
    assert (await rpc_error(state, "permissions.delete_profile", name=save["name"]))[
        "code"
    ] == "access_denied"
    assert (await rpc_error(state, "agent.create", id="coder", permission_profile="full_access"))[
        "code"
    ] == "access_denied"
    first = await rpc_result(state, "permissions.profiles")
    assert {row["name"] for row in first["profiles"] if row["builtin"]} == {
        "restricted",
        "full_access",
    }
    with bound_principal(HOST):
        saved = await rpc_result(state, "permissions.save_profile", **save)
        assert await rpc_result(state, "permissions.profiles") == saved
        (profile,) = [row for row in saved["profiles"] if not row["builtin"]]
        assert profile["name"] == save["name"]
        assert profile["rules"]["read"] == "work_area"
        assert (
            await rpc_error(
                state,
                "permissions.save_profile",
                **{**save, "permission_rules": {"write": "scratch"}},
            )
        )["code"] == "settings_conflict"
        assert (await rpc_error(state, "permissions.delete_profile", name="restricted"))[
            "code"
        ] == "invalid_request"
        state.runtime.agents.create("coder", permission_profile=save["name"])
        used = await rpc_error(state, "permissions.delete_profile", name=save["name"])
        assert used["code"] == "permission_profile_in_use"
        assert used["data"]["agents"] == ["coder"]
        stale_edit = await rpc_error(
            state,
            "agent.update",
            id="coder",
            permission_rules={},
            require_no_permission_profile=True,
        )
        assert "permission profile is selected" in stale_edit["message"]
        assert state.runtime.agents.get_raw("coder").permission_profile == save["name"]
        state.runtime.agents.update("coder", permission_profile=None)
        assert (
            await rpc_result(
                state, "permissions.delete_profile", name=save["name"], base=profile["rules"]
            )
            == first
        )
    # The first Agent RPC also observes the fixture-created Agent for setup.
    catalog_events = [payload["kind"] for _, payload in state.events if payload["kind"] != "setup"]
    assert catalog_events == ["settings", "agents", "projects"] * 2


@pytest.mark.asyncio
async def test_reviewed_agent_change_requires_the_same_effective_rules(tmp_path: Path) -> None:
    defaults: JsonObject = {"permission_rules": {"write": "scratch"}}
    state = _state(tmp_path, defaults)
    state.agent_delete_lock = asyncio.Lock()
    state.runtime.models = _NoModels()
    selection = {"permission_profile": "Shared"}
    state.runtime.agents.create("coder")
    with bound_principal(HOST):
        catalog = await rpc_result(
            state, "permissions.save_profile", name="Shared", permission_rules={"write": "scratch"}
        )
        reviewed = next(row["rules"] for row in catalog["profiles"] if row["name"] == "Shared")
        await rpc_result(
            state, "permissions.save_profile", name="Shared", permission_rules=_FULL_ACCESS
        )

        for method, agent_id in (("agent.create", "new"), ("agent.update", "coder")):
            refused = await rpc_error(
                state, method, id=agent_id, expected_profile_rules=reviewed, **selection
            )
            assert refused["code"] == "settings_conflict"
        assert not state.runtime.agents.exists("new")
        assert state.runtime.agents.get_raw("coder").permission_profile is None

        created = await rpc_result(
            state, "agent.create", id="new", expected_profile_rules=_FULL_ACCESS, **selection
        )
        inherited = created["permission_rules_default"]
        # A missing current profile can be repaired by a clear. Review only
        # what this Agent would inherit, including refusal of broken defaults.
        state.runtime.agents.update("new", permission_profile="Missing")
        before = await rpc_result(state, "agent.get", id="new")
        assert before["effective"]["permission_rules"]["error"]
        preview = {"agent_id": "new", "permission_rules": None}
        reviewed_clear = await rpc_result(state, "permissions.validate", **preview)
        assert reviewed_clear["effective"] == inherited
        clear = {"id": "new", "permission_rules": None, "permission_profile": None}
        defaults["permission_rules"] = None
        full_access = await rpc_result(state, "permissions.validate", **preview)
        assert full_access["valid"] and full_access["effective"] is None
        defaults["permission_rules"] = {"unknown": "part"}
        invalid = await rpc_error(state, "permissions.validate", **preview)
        assert invalid["code"] == "domain_error"
        refused = await rpc_error(
            state, "agent.update", **clear, expected_profile_rules=full_access["effective"]
        )
        assert refused["code"] == "settings_conflict"
        defaults["permission_rules"] = {"read": "work_area"}
        refused = await rpc_error(state, "agent.update", **clear, expected_profile_rules=inherited)
        assert refused["code"] == "settings_conflict"
        assert state.runtime.agents.get_raw("new").permission_profile == "Missing"
        # An unknown Agent parameter with a Project field name must reach the Agent validator.
        async with asyncio.timeout(2):
            malformed = await rpc_error(state, "agent.update", **clear, field="permission_profile")
        assert malformed["code"] == "invalid_request"
        cleared = await rpc_result(
            state, "agent.update", **clear, expected_profile_rules={"read": "work_area"}
        )
        assert cleared["config"]["permission_profile"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "params"),
    [
        (
            "agent.update",
            {"id": "coder", "permission_profile": "full_access", "expected_profile_rules": None},
        ),
        ("permissions.delete_profile", {"name": "Shared"}),
        ("channel.create", {"id": "notify", "platform": "telegram", "agent_id": "coder"}),
    ],
)
async def test_settings_waiter_leaves_agent_references_available_for_callbacks(
    tmp_path: Path, method: str, params: JsonObject
) -> None:
    waiting = asyncio.Event()

    class PublicationLock(asyncio.Lock):
        @override
        async def acquire(self) -> Literal[True]:
            if self.locked():
                waiting.set()
            return await super().acquire()

    state = _state(tmp_path, {})
    state.agent_delete_lock = asyncio.Lock()
    state._settings_mutation_lock = PublicationLock()
    state.runtime.models = _NoModels()
    state.runtime.agents.create("coder")

    async def external_request() -> JsonObject:
        with bound_principal(HOST):
            return await rpc_result(state, method, **params)

    async with state._settings_mutation_lock:
        request = asyncio.create_task(external_request())
        async with asyncio.timeout(2):
            await waiting.wait()
        references_available = not state.agent_delete_lock.locked()
        if references_available:
            # An Extension callback can update an Agent during Settings publication.
            await rpc_result(state, "agent.update", id="coder", name="from callback")
        request.cancel()
        await asyncio.sleep(0)

    with pytest.raises(asyncio.CancelledError):
        await request
    assert references_available
    assert state.runtime.agents.get_raw("coder").permission_profile is None
