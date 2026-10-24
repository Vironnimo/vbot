"""Moving an Identity Agent's files into an archive payload and back (AgentStore)."""

import errno
import json
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from core.agents import AgentAlreadyExistsError, AgentError, AgentStore, InvalidAgentIdError
from core.utils import tree_move
from tests.core.agents.agents_test_support import persisted, rewrite
from tests.core.agents.agents_test_support import store as store
from tests.core.agents.agents_test_support import template_dir as template_dir


def _payload(store: AgentStore) -> Path:
    return store.data_dir / "archive" / "entries" / "arc_test" / "agent"


def test_archive_files_moves_the_agent_home_and_leaves_an_external_workspace(
    store: AgentStore, tmp_path: Path
) -> None:
    store.create("coder", "Coder Agent")
    external = tmp_path / "rooted-repo"
    store.create("rooted", "Rooted Agent", workspace=external)
    store.create("beta", "Beta")
    store.reorder(
        ["beta", "coder", "rooted"], expected_revision=store.list_with_order().order_revision
    )
    payload = _payload(store)

    with store.archive_files("coder", payload) as coder:
        assert not (store.data_dir / "agents" / "coder").exists()
    with store.archive_files("rooted", payload.with_name("rooted")) as rooted:
        pass

    assert (coder.agent.id, coder.roster_index, coder.workspace_external) == ("coder", 1, False)
    # The default Workspace lives inside the Agent's directory and moves with it.
    assert (payload / "agent.json").is_file()
    assert (payload / "workspace" / "SOUL.md").is_file()
    assert rooted.workspace_external is True
    assert rooted.workspace == str(external.resolve())
    assert (external / "SOUL.md").is_file()
    assert [agent.id for agent in store.list_with_order().agents] == ["beta"]


@pytest.mark.parametrize("move_back", ["succeeds", "refused"])
def test_archive_files_moves_the_home_back_when_the_commit_fails(
    store: AgentStore, monkeypatch: pytest.MonkeyPatch, move_back: str
) -> None:
    store.create("coder", "Coder Agent")
    home = store.data_dir / "agents" / "coder"
    payload = _payload(store)
    real_replace = os.replace

    def replace(source: Any, destination: Any) -> None:
        if move_back == "refused" and Path(destination) == home:
            raise PermissionError(errno.EACCES, "held open by another program")
        real_replace(source, destination)

    monkeypatch.setattr(tree_move, "_replace", replace)
    expected = (
        (RuntimeError, "database unavailable")
        if move_back == "succeeds"
        else (AgentError, f"Agent files retained at {re.escape(str(payload))}")
    )
    with pytest.raises(expected[0], match=expected[1]), store.archive_files("coder", payload):
        raise RuntimeError("database unavailable")

    if move_back == "succeeds":
        assert store.get("coder").name == "Coder Agent"
        assert not payload.exists()
    else:  # the files stay whole in the payload, never deleted
        assert (payload / "agent.json").is_file()
        assert not home.exists()


@pytest.mark.parametrize(
    "blocker",
    [
        "refused-rename",
        pytest.param(
            "open-file",
            marks=pytest.mark.skipif(
                os.name != "nt", reason="only Windows refuses to rename a tree with open files"
            ),
        ),
    ],
)
def test_archive_files_keeps_the_agent_when_the_move_cannot_complete(
    store: AgentStore, monkeypatch: pytest.MonkeyPatch, blocker: str
) -> None:
    store.create("coder", "Coder Agent")
    home = store.data_dir / "agents" / "coder"
    real_replace = os.replace

    def replace(source: Any, destination: Any) -> None:
        if Path(source) == home:
            raise PermissionError(errno.EACCES, "held open by another program")
        real_replace(source, destination)

    if blocker == "refused-rename":
        monkeypatch.setattr(tree_move, "_replace", replace)
        held = None
    else:
        held = (home / "held.txt").open("w", encoding="utf-8")
    try:
        with (
            pytest.raises(AgentError, match="Agent archival failed"),
            store.archive_files("coder", _payload(store)),
        ):
            pytest.fail("the body runs only after a complete move")
    finally:
        if held is not None:
            held.close()

    # A move that fails means nothing moved: no partial copy replaces or removes a tree.
    assert store.get("coder").name == "Coder Agent"
    assert not _payload(store).exists()


@pytest.mark.parametrize("target_id", ["coder", "coder-restored"])
def test_delegation_grants_are_recorded_before_removal_and_return_at_their_position(
    store: AgentStore, monkeypatch: pytest.MonkeyPatch, target_id: str
) -> None:
    store.create("coder", "Coder")
    # A qualified address names a Project's Team Agent, not the archived one.
    beta = store.create("beta", "Beta", tools={"subagent": {"allowed_agents": ["a", "coder", "b"]}})
    store.create("gamma", "Gamma", tools={"subagent": {"allowed_agents": ["coder@project"]}})
    delta = store.create("delta", "Delta", tools={"subagent": {"allowed_agents": ["coder"]}})
    write_agent = store._write_agent

    def fail_delta(agent: Any) -> None:
        if agent.id == "delta":
            raise AgentError("disk full")
        write_agent(agent)

    monkeypatch.setattr(store, "_write_agent", fail_delta)
    recorded: list[list[Any]] = []

    def record(grants: list[Any]) -> None:
        # The grants reach the entry before any config changes.
        assert persisted(store, "beta")["tools"]["subagent"]["allowed_agents"] == [
            "a",
            "coder",
            "b",
        ]
        recorded.append(list(grants))

    # Best effort: a config that cannot be written keeps its grant.
    assert store.remove_delegation_grants("coder", record) == ("beta",)
    monkeypatch.setattr(store, "_write_agent", write_agent)

    assert recorded == [
        [
            {
                "agent_id": "beta",
                "agent_created_at": beta.created_at,
                "index": 1,
                "target_created_at": store.get("coder").created_at,
            },
            {
                "agent_id": "delta",
                "agent_created_at": delta.created_at,
                "index": 0,
                "target_created_at": store.get("coder").created_at,
            },
        ]
    ]
    assert store.get("beta").tools["subagent"]["allowed_agents"] == ["a", "b"]
    assert store.get("gamma").tools["subagent"]["allowed_agents"] == ["coder@project"]
    assert store.get("delta").tools["subagent"]["allowed_agents"] == ["coder"]
    expected_changed = ("beta",) if target_id == "coder" else ("beta", "delta")
    assert store.restore_delegation_grants(target_id, recorded[0]) == expected_changed
    assert store.get("beta").tools["subagent"]["allowed_agents"] == ["a", target_id, "b"]
    assert store.get("delta").tools["subagent"]["allowed_agents"] == (
        ["coder"] if target_id == "coder" else [target_id, "coder"]
    )
    assert store.restore_delegation_grants(target_id, recorded[0]) == ()


@pytest.mark.parametrize(
    ("holder_state", "problem"),
    [
        ("live", None),
        ("restored", None),
        ("replaced", "holder_replaced"),
        ("missing", "holder_missing"),
        ("legacy", "holder_unproven"),
        ("legacy-replaced", "holder_unproven"),
        ("legacy-missing", "holder_missing"),
        ("invalid-stamp", "holder_unproven"),
        ("holder-stamp-missing", "holder_unproven"),
        ("invalid-holder-config", "holder_unproven"),
        ("unavailable-holder-config", "holder_unproven"),
        ("target-proof-missing", None),
        ("target-proof-invalid", None),
        ("unproven-existing-entry", None),
    ],
)
def test_delegation_grants_restore_only_the_recorded_identities(
    store: AgentStore,
    monkeypatch: pytest.MonkeyPatch,
    deny_access: Callable[[Path], None],
    holder_state: str,
    problem: str | None,
) -> None:
    monkeypatch.setattr(
        "core.agents.agents.utc_now_timestamp", lambda: "2026-01-01T00:00:00.000000Z"
    )
    store.create("coder")
    beta = store.create("beta", tools={"subagent": {"allowed_agents": ["a", "coder", "b"]}})
    recorded: list[Any] = []
    assert store.remove_delegation_grants("coder", recorded.extend) == ("beta",)

    missing = holder_state in {"missing", "legacy-missing"}
    if holder_state in {"restored", "replaced", "legacy-replaced"} or missing:
        holder_payload = _payload(store).with_name("beta")
        with store.archive_files("beta", holder_payload):
            pass
        if holder_state == "restored":
            with store.restore_files(holder_payload, "beta", workspace=None, root_project_id=None):
                pass
            assert store.get("beta").created_at == beta.created_at
        elif holder_state in {"replaced", "legacy-replaced"}:
            monkeypatch.setattr(
                "core.agents.agents.utc_now_timestamp", lambda: "2026-01-02T00:00:00.000000Z"
            )
            replacement = store.create("beta", tools={"subagent": {"allowed_agents": ["a", "b"]}})
            assert replacement.created_at != beta.created_at
    if holder_state in {"legacy", "legacy-replaced", "legacy-missing"}:
        recorded[0].pop("agent_created_at")
    elif holder_state == "invalid-stamp":
        recorded[0]["agent_created_at"] = "not-a-timestamp"
        rewrite(store, "beta", created_at="not-a-timestamp")
    elif holder_state == "holder-stamp-missing":
        rewrite(store, "beta", "created_at")
    elif holder_state == "invalid-holder-config":
        rewrite(store, "beta", name=123)
    elif holder_state == "target-proof-missing":
        recorded[0].pop("target_created_at")
    elif holder_state == "target-proof-invalid":
        recorded[0]["target_created_at"] = "not-a-timestamp"
    elif holder_state == "unproven-existing-entry":
        # An older config may already list this name without proving its target.
        # It must not mask the archived grant's valid identity evidence.
        rewrite(store, "beta", tools={"subagent": {"allowed_agents": ["a", "coder-restored", "b"]}})

    holder_config = store.data_dir / "agents" / "beta" / "agent.json"
    holder_soul = Path(store.default_workspace("beta")) / "SOUL.md"
    if not missing:
        # Checking must not seed the Workspace or repair a dangling Session pointer.
        rewrite(store, "beta", "workspace", current_session_id="gone")
        holder_soul.unlink()
        before = holder_config.read_bytes()
    unavailable = holder_state == "unavailable-holder-config"
    if unavailable:
        deny_access(holder_config)
    assert store.delegation_grant_problem(recorded[0]) == problem
    if not missing:
        if not unavailable:
            assert holder_config.read_bytes() == before
        assert not holder_soul.exists()

    restored = problem is None and not holder_state.startswith("target-proof-")
    assert store.restore_delegation_grants("coder-restored", recorded) == (
        ("beta",) if restored else ()
    )
    if unavailable:
        monkeypatch.undo()
        assert holder_config.read_bytes() == before
    if not missing:
        assert persisted(store, "beta")["tools"]["subagent"]["allowed_agents"] == (
            ["a", "coder-restored", "b"] if restored else ["a", "b"]
        )
        if restored:
            assert (
                persisted(store, "beta")["delegation_bindings"]["coder-restored"]
                == recorded[0]["target_created_at"]
            )


def test_restore_files_rewrites_the_payload_to_the_target_and_keeps_unknown_fields(
    store: AgentStore,
) -> None:
    store.create("coder", "Coder Agent", tools={"subagent": {"allowed_agents": ["legacy", "beta"]}})
    store.create("beta", "Beta")
    payload = _payload(store)
    with store.archive_files("coder", payload):
        pass
    inspected = store.inspect_archived(payload)
    assert inspected.problem is None and inspected.agent is not None
    assert inspected.agent.id == "coder"
    document = json.loads((payload / "agent.json").read_text(encoding="utf-8"))
    document["future_field"] = {"kept": True}
    document["delegation_bindings"].pop("legacy")
    (payload / "agent.json").write_text(json.dumps(document), encoding="utf-8")
    archived = (payload / "agent.json").read_bytes()

    with (
        pytest.raises(AgentAlreadyExistsError),
        store.restore_files(payload, "beta", workspace=None, root_project_id=None),
    ):
        pytest.fail("a taken id is refused before anything moves")
    with (
        pytest.raises(InvalidAgentIdError, match="'aux' is reserved on Windows"),
        store.restore_files(payload, "aux", workspace=None, root_project_id=None),
    ):
        pytest.fail("a new id Windows reserves is refused before anything moves")
    with (
        pytest.raises(RuntimeError, match="database unavailable"),
        store.restore_files(payload, "coder-2", workspace=None, root_project_id=None),
    ):
        raise RuntimeError("database unavailable")
    assert not store.exists("coder-2")
    # A failed restore leaves the payload exactly as archived.
    assert (payload / "agent.json").read_bytes() == archived
    with store.restore_files(payload, "coder-2", workspace=None, root_project_id=None) as agent:
        pass

    assert not payload.exists()
    assert (agent.id, agent.name) == ("coder-2", "Coder Agent")
    # The default Workspace moved along and follows the new id.
    assert agent.workspace == str((store.data_dir / "agents" / "coder-2" / "workspace").resolve())
    assert (Path(agent.workspace) / "SOUL.md").is_file()
    restored = persisted(store, "coder-2")
    assert (restored["id"], restored["future_field"]) == ("coder-2", {"kept": True})
    assert restored["tools"]["subagent"]["allowed_agents"] == ["beta"]
    assert agent.tools["subagent"]["allowed_agents"] == ["beta"]
    assert store.get("coder-2").name == "Coder Agent"


@pytest.mark.parametrize(
    ("document", "problem"),
    [
        pytest.param(None, "payload_missing", id="missing"),
        pytest.param("{ not json", "payload_invalid", id="invalid"),
        pytest.param({"id": "coder"}, "older_format", id="pre-generation-1"),
        pytest.param({"format_version": 99, "id": "coder"}, "newer_format", id="newer"),
    ],
)
def test_inspect_archived_reports_why_a_payload_cannot_return(
    store: AgentStore, document: object, problem: str
) -> None:
    payload = _payload(store)
    payload.mkdir(parents=True)
    if document is not None:
        text = document if isinstance(document, str) else json.dumps(document)
        (payload / "agent.json").write_text(text, encoding="utf-8")

    inspected = store.inspect_archived(payload)

    assert (inspected.agent, inspected.problem) == (None, problem)
