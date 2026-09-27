"""Skill RPCs: scope-bound authoring and installation, plus the manager surface.

``global`` and ``agent:<id>`` scopes are written through the real authoring
service. The manager's inventory and policy file are canned on the runtime
double; their own contracts live in ``tests/core/runtime/test_runtime_shared_skills.py``.
"""

from __future__ import annotations

import asyncio
import io
import threading
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.skills import SkillPolicyError
from core.skills.authoring import SkillAuthoringService
from core.skills.skills import SkillRegistry
from server.events import ServerEventBus
from server.rpc.skill_methods import install_skill_upload
from tests.server.rpc_test_support import call, resource_changes, rpc_error, rpc_result

JsonObject = dict[str, Any]
_REMOTE_SOURCE = "https://example.org/download.skill"
_SHARE = {"agent_id": "builder", "name": "deploy", "shared": True, "receivers": ["reviewer"]}


def _skill_md(
    name: str = "demo", description: str = "Do a demo task.", body: str = "# Demo\n"
) -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n\n{body}"


class _SkillRuntime:
    """The runtime surface of the Skill RPCs; records live refreshes and policy writes."""

    def __init__(self, root: Path) -> None:
        self._root = root
        # Private Skill homes are identity-only: only these Agents accept writes.
        self.known_agents = {"builder", "reviewer"}
        self.agents = SimpleNamespace(exists=lambda agent_id: agent_id in self.known_agents)
        self.global_skills_dir = root / "skills"
        self.skill_authoring = SkillAuthoringService(
            protected_roots=[root / "resources" / "skills"]
        )
        self.reload_calls = 0
        self.invalidated: list[str | None] = []
        self.inventory: JsonObject = {"skills": [{"name": "deploy"}], "stale_shared": []}
        self.policy_calls: list[tuple[Any, ...]] = []
        self.policy_error: SkillPolicyError | None = None
        self.skill_policy = SimpleNamespace(
            set_disabled=self._set_disabled, set_shared=self._set_shared
        )

    def agent_skills_dir(self, agent_id: str) -> Path:
        return self._root / "agents" / agent_id / "skills"

    async def reload_skills_async(self) -> None:
        self.reload_calls += 1

    def invalidate_agent_skills(self, agent_id: str | None) -> None:
        self.invalidated.append(agent_id)

    def skill_inventory(self) -> JsonObject:
        return self.inventory

    def inspect_skill(self, entry_id: str) -> JsonObject:
        if entry_id != "opaque-id":
            raise ValueError(f"no Skill package has id {entry_id}")
        return {"id": entry_id, "content": "inspection-sentinel"}

    def agent_owns_private_skill(self, agent_id: str, name: str) -> bool:
        return agent_id == "builder" and name == "deploy"

    def _set_disabled(self, name: str, *, disabled: bool) -> None:
        if self.policy_error is not None:
            raise self.policy_error
        self.policy_calls.append(("set_disabled", name, disabled))

    def _set_shared(self, agent_id: str, name: str, *, shared: bool, receivers: list[str]) -> None:
        if self.policy_error is not None:
            raise self.policy_error
        self.policy_calls.append(("set_shared", agent_id, name, shared, receivers))


def _state(tmp_path: Path) -> Any:
    return SimpleNamespace(
        runtime=_SkillRuntime(tmp_path),
        event_bus=ServerEventBus(),
        agent_delete_lock=asyncio.Lock(),
    )


def _scope_root(state: Any, scope: str) -> Path:
    if scope == "global":
        return Path(state.runtime.global_skills_dir)
    return Path(state.runtime.agent_skills_dir(scope.removeprefix("agent:")))


def _package(tmp_path: Path, name: str = "demo") -> Path:
    source = tmp_path / "package"
    source.mkdir()
    (source / "SKILL.md").write_text(_skill_md(name), encoding="utf-8")
    return source


def _expected_refresh(scope: str, count: int) -> tuple[int, list[str | None]]:
    """A global write reloads the whole registry; a private one invalidates its Agent."""
    if scope == "global":
        return count, []
    return 0, [scope.removeprefix("agent:")] * count


# ---------------------------------------------------------------------------
# Authoring and installation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["global", "agent:builder"])
async def test_authoring_writes_the_scope_and_refreshes_it_live(tmp_path: Path, scope: str) -> None:
    state = _state(tmp_path)
    root = _scope_root(state, scope)
    support_file = root / "demo" / "scripts" / "run.py"

    empty = await rpc_result(state, "skill.read", scope=scope)
    created = await rpc_result(
        state,
        "skill.create",
        scope=scope,
        name="demo",
        content=_skill_md(body="# Demo\nGo."),
        source="wiki",
    )
    provenance = SkillRegistry.load(root).get("demo").metadata["vbot"]
    listed = await rpc_result(state, "skill.read", scope=scope)
    # A document without a description is accepted with a warning; the body describes it.
    edited = await rpc_result(
        state, "skill.update", scope=scope, name="demo", content="---\nname: demo\n---\n\nbody\n"
    )
    edited_description = SkillRegistry.load(root).get("demo").description
    await rpc_result(
        state,
        "skill.write_file",
        scope=scope,
        name="demo",
        path="scripts/run.py",
        content="x = 1\n",
    )
    written = support_file.read_text(encoding="utf-8")
    await rpc_result(state, "skill.remove_file", scope=scope, name="demo", path="scripts/run.py")
    removed = not support_file.exists()
    deleted = await rpc_result(state, "skill.delete", scope=scope, name="demo")

    assert empty == {"skills": []}
    assert (created["name"], created["operation"]) == ("demo", "create")
    assert (provenance["author"], provenance["source"]) == ("human", "wiki")
    [entry] = listed["skills"]
    assert (entry["name"], entry["description"]) == ("demo", "Do a demo task.")
    assert "# Demo\nGo." in entry["content"]
    assert edited["warnings"]
    assert edited_description == "body"
    assert written == "x = 1\n"
    assert removed
    assert deleted["operation"] == "delete"
    assert not (root / "demo").exists()
    runtime = state.runtime
    assert (runtime.reload_calls, runtime.invalidated) == _expected_refresh(scope, 5)


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["global", "agent:builder"])
async def test_install_publishes_the_complete_package_and_refreshes_the_scope(
    tmp_path: Path, scope: str
) -> None:
    state = _state(tmp_path)
    source = _package(tmp_path)
    (source / "assets").mkdir()
    (source / "assets" / "template.bin").write_bytes(b"\x00\xff")

    result = await rpc_result(state, "skill.install", scope=scope, source=str(source))

    assert (result["name"], result["operation"], result["files"]) == ("demo", "installed", 2)
    assert result["scope"] == scope
    installed = _scope_root(state, scope) / "demo" / "assets" / "template.bin"
    assert installed.read_bytes() == b"\x00\xff"
    runtime = state.runtime
    assert (runtime.reload_calls, runtime.invalidated) == _expected_refresh(scope, 1)
    assert resource_changes(state) == [{"kind": "skills"}]


@pytest.mark.asyncio
async def test_install_preview_writes_and_refreshes_nothing(tmp_path: Path) -> None:
    state = _state(tmp_path)
    source = _package(tmp_path)

    result = await rpc_result(
        state, "skill.install", scope="agent:builder", source=str(source), dry_run=True
    )

    assert result["operation"] == "preview"
    assert not state.runtime.agent_skills_dir("builder").exists()
    assert (state.runtime.reload_calls, state.runtime.invalidated) == (0, [])
    assert resource_changes(state) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "params", "code", "named", "policy_fails"),
    [
        # Scopes: only the global pool or an existing Identity Agent's private home.
        (
            "skill.create",
            {"scope": "project:vbot", "name": "demo", "content": _skill_md()},
            "invalid_request",
            "project:vbot",
            False,
        ),
        (
            "skill.create",
            {"scope": "agent:../escape", "name": "demo", "content": _skill_md()},
            "invalid_request",
            "../escape",
            False,
        ),
        # A well-formed id that names no Identity Agent (e.g. a Project team slug)
        # would create a stray home no Agent owns.
        (
            "skill.create",
            {"scope": "agent:ghost", "name": "demo", "content": _skill_md()},
            "agent_not_found",
            "ghost",
            False,
        ),
        (
            "skill.create",
            {"name": "demo", "content": _skill_md()},
            "invalid_request",
            "scope",
            False,
        ),
        (
            "skill.write_file",
            {"scope": "global", "name": "demo", "path": "a.py", "content": 1},
            "invalid_request",
            "content",
            False,
        ),
        # Installation flags are checked before the source is read.
        (
            "skill.install",
            {"scope": "agent:ghost", "source": _REMOTE_SOURCE},
            "agent_not_found",
            "ghost",
            False,
        ),
        (
            "skill.install",
            {"scope": "global", "source": _REMOTE_SOURCE, "replace": "false"},
            "invalid_request",
            "replace",
            False,
        ),
        (
            "skill.install",
            {"scope": "global", "source": _REMOTE_SOURCE, "path": ""},
            "invalid_request",
            "path",
            False,
        ),
        (
            "skill.install",
            {"scope": "global", "source": _REMOTE_SOURCE, "expected_sha256": "abc"},
            "invalid_request",
            "SHA-256",
            False,
        ),
        (
            "skill.install",
            {"scope": "global", "source": _REMOTE_SOURCE, "unknown_option": True},
            "invalid_request",
            "",
            False,
        ),
        ("skill.inventory", {"scope": "global"}, "invalid_request", "", False),
        ("skill.inspect", {"id": "missing-id"}, "invalid_request", "missing-id", False),
        (
            "skill.set_disabled",
            {"name": "ghost", "disabled": True},
            "skill_not_found",
            "ghost",
            False,
        ),
        (
            "skill.set_disabled",
            {"name": "deploy", "disabled": "yes"},
            "invalid_request",
            "disabled",
            False,
        ),
        # An unreadable or invalid policy document is refused, never overwritten.
        ("skill.set_disabled", {"name": "deploy", "disabled": True}, "domain_error", "", True),
        ("skill.share", {**_SHARE, "agent_id": "ghost"}, "agent_not_found", "ghost", False),
        ("skill.share", {**_SHARE, "name": "notes"}, "skill_not_found", "notes", False),
        ("skill.share", {**_SHARE, "receivers": ["ghost"]}, "agent_not_found", "ghost", False),
        ("skill.share", {**_SHARE, "receivers": ["builder"]}, "invalid_request", "itself", False),
        ("skill.share", {**_SHARE, "receivers": []}, "invalid_request", "receiver", False),
        ("skill.share", _SHARE, "domain_error", "", True),
    ],
)
async def test_skill_rpc_refusals_change_nothing(
    tmp_path: Path,
    method: str,
    params: JsonObject,
    code: str,
    named: str,
    policy_fails: bool,
) -> None:
    state = _state(tmp_path)
    if policy_fails:
        state.runtime.policy_error = SkillPolicyError("Cannot update invalid skill policy")

    error = await rpc_error(state, method, **params)

    assert error["code"] == code
    assert named in error["message"]
    runtime = state.runtime
    assert (runtime.reload_calls, runtime.invalidated, runtime.policy_calls) == (0, [], [])
    assert resource_changes(state) == []
    assert not (tmp_path / "skills").exists()
    assert not (tmp_path / "agents").exists()


# ---------------------------------------------------------------------------
# Worker offload, serialization and Agent lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["skill.create", "skill.install", "skill.read"])
@pytest.mark.parametrize("scope", ["global", "agent:builder"])
async def test_skill_rpc_io_runs_off_the_loop_and_refreshes_on_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str, scope: str
) -> None:
    state = _state(tmp_path)
    await rpc_result(state, "skill.create", scope=scope, name="demo", content=_skill_md())
    params: JsonObject = {"scope": scope}
    if method == "skill.create":
        owner, name = state.runtime.skill_authoring, "create"
        params.update(name="new", content=_skill_md("new"))
    elif method == "skill.install":
        owner, name = state.runtime.skill_authoring, "install"
        params["source"] = str(_package(tmp_path, "imported"))
    else:
        owner, name = SkillRegistry, "load"
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    entered = asyncio.Event()
    release = threading.Event()
    refreshed: list[bool] = []
    original = getattr(owner, name)

    def slow_io(*args: Any, **kwargs: Any) -> Any:
        assert threading.get_ident() != loop_thread
        # Private mutations hold the Agent reference lock through the write.
        assert state.agent_delete_lock.locked() == (
            scope == "agent:builder" and method != "skill.read"
        )
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(2)
        return original(*args, **kwargs)

    async def reload_skills() -> None:
        assert threading.get_ident() == loop_thread
        refreshed.append(True)

    monkeypatch.setattr(owner, name, slow_io)
    monkeypatch.setattr(state.runtime, "reload_skills_async", reload_skills)
    task = asyncio.create_task(call(state, method, **params))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert not task.done()
    finally:
        release.set()
    response = await task
    assert response["ok"] is True, response
    assert bool(refreshed) == (scope == "global" and method != "skill.read")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("upload", "cancel_caller"), [(False, False), (True, True)], ids=["rpc", "upload-cancelled"]
)
async def test_private_install_settles_before_agent_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, upload: bool, cancel_caller: bool
) -> None:
    state = _state(tmp_path)
    source = _package(tmp_path)
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("SKILL.md", _skill_md())
    entered = asyncio.Event()
    archive_started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original = state.runtime.skill_authoring.install

    def install(*args: Any, **kwargs: Any) -> Any:
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(10)
        return original(*args, **kwargs)

    monkeypatch.setattr(state.runtime.skill_authoring, "install", install)
    archived = tmp_path / "archived-builder"

    async def archive_agent() -> None:
        archive_started.set()
        async with state.agent_delete_lock:
            state.runtime.known_agents.remove("builder")
            agent_root = state.runtime.agent_skills_dir("builder").parent
            if agent_root.exists():
                agent_root.rename(archived)

    request = asyncio.create_task(
        install_skill_upload(
            state, {"scope": "agent:builder"}, data=archive.getvalue(), filename="demo.skill"
        )
        if upload
        else call(state, "skill.install", scope="agent:builder", source=str(source))
    )
    deletion = None
    try:
        await asyncio.wait_for(entered.wait(), 10)
        if cancel_caller:
            request.cancel()
            await asyncio.sleep(0)
        deletion = asyncio.create_task(archive_agent())
        await archive_started.wait()
        assert not deletion.done()
        release.set()
        if cancel_caller:
            with pytest.raises(asyncio.CancelledError):
                await request
        else:
            assert (await request)["result"]["operation"] == "installed"
        await deletion
        # The install settled into the home before the archive moved it.
        assert not state.runtime.agent_skills_dir("builder").parent.exists()
        assert (archived / "skills" / "demo" / "SKILL.md").is_file()
        assert state.runtime.invalidated == ["builder"]
    finally:
        release.set()
        await asyncio.gather(request, return_exceptions=True)
        if deletion is not None:
            await deletion


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_waiter", [False, True])
async def test_private_install_waits_for_lifecycle_before_scope_validation(
    tmp_path: Path, cancel_waiter: bool
) -> None:
    state = _state(tmp_path)
    source = _package(tmp_path)
    await state.agent_delete_lock.acquire()
    request = asyncio.create_task(
        call(state, "skill.install", scope="agent:builder", source=str(source))
    )
    try:
        await asyncio.sleep(0)
        if cancel_waiter:
            request.cancel()
            await asyncio.sleep(0)
            assert request.done()
        state.runtime.known_agents.remove("builder")
    finally:
        state.agent_delete_lock.release()
    if cancel_waiter:
        with pytest.raises(asyncio.CancelledError):
            await request
    else:
        # Validated only after admission: the Agent removed meanwhile is unknown.
        assert (await request)["error"]["code"] == "agent_not_found"
    assert not state.runtime.agent_skills_dir("builder").exists()


# ---------------------------------------------------------------------------
# Manager surface: inventory, inspection, disable and share policy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manager_reads_return_runtime_answers_from_a_worker(tmp_path: Path) -> None:
    state = _state(tmp_path)
    runtime = state.runtime
    loop_thread = threading.get_ident()
    threads: list[int] = []
    inventory, inspect_skill = runtime.skill_inventory, runtime.inspect_skill

    def on_worker(function: Any) -> Any:
        def recorded(*args: Any) -> Any:
            threads.append(threading.get_ident())
            return function(*args)

        return recorded

    runtime.skill_inventory = on_worker(inventory)
    runtime.inspect_skill = on_worker(inspect_skill)

    listed = await rpc_result(state, "skill.inventory")
    inspected = await rpc_result(state, "skill.inspect", id="opaque-id")

    assert listed == {"skills": [{"name": "deploy"}], "stale_shared": []}
    assert inspected == {"id": "opaque-id", "content": "inspection-sentinel"}
    assert len(threads) == 2
    assert loop_thread not in threads


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "params", "result", "policy_call", "cancel"),
    [
        (
            "skill.set_disabled",
            {"name": "deploy", "disabled": True},
            {"name": "deploy", "disabled": True},
            ("set_disabled", "deploy", True),
            False,
        ),
        (
            "skill.share",
            _SHARE,
            _SHARE,
            ("set_shared", "builder", "deploy", True, ["reviewer"]),
            False,
        ),
        # Unsharing needs no receivers and clears them.
        (
            "skill.share",
            {"agent_id": "builder", "name": "deploy", "shared": False},
            {"agent_id": "builder", "name": "deploy", "shared": False, "receivers": []},
            ("set_shared", "builder", "deploy", False, []),
            False,
        ),
        (
            "skill.share",
            _SHARE,
            None,
            ("set_shared", "builder", "deploy", True, ["reviewer"]),
            True,
        ),
    ],
    ids=["disable", "share", "unshare", "share-cancelled"],
)
async def test_policy_mutation_persists_off_the_loop_and_applies_after_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    params: JsonObject,
    result: JsonObject | None,
    policy_call: tuple[Any, ...],
    cancel: bool,
) -> None:
    state = _state(tmp_path)
    runtime = state.runtime
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    entered = asyncio.Event()
    release = threading.Event()
    policy_method = "set_disabled" if method == "skill.set_disabled" else "set_shared"
    original = getattr(runtime.skill_policy, policy_method)

    def on_worker(function: Any) -> Any:
        def checked(*args: Any, **kwargs: Any) -> Any:
            assert threading.get_ident() != loop_thread
            return function(*args, **kwargs)

        return checked

    monkeypatch.setattr(runtime, "skill_inventory", on_worker(runtime.skill_inventory))
    monkeypatch.setattr(
        runtime, "agent_owns_private_skill", on_worker(runtime.agent_owns_private_skill)
    )

    def slow_write(*args: Any, **kwargs: Any) -> Any:
        assert threading.get_ident() != loop_thread
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(2)
        return original(*args, **kwargs)

    async def reload_skills() -> None:
        assert threading.get_ident() == loop_thread
        assert runtime.policy_calls
        runtime.reload_calls += 1

    monkeypatch.setattr(runtime.skill_policy, policy_method, slow_write)
    monkeypatch.setattr(runtime, "reload_skills_async", reload_skills)
    task = asyncio.create_task(call(state, method, **params))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        if cancel:
            task.cancel()
            await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
    if cancel:
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        assert (await task)["result"] == result
    assert runtime.policy_calls == [policy_call]
    assert resource_changes(state) == [{"kind": "skills"}]
    # Disabling reloads the registry; shared Skills never enter the global pool.
    if method == "skill.set_disabled":
        assert (runtime.reload_calls, runtime.invalidated) == (1, [])
    else:
        assert (runtime.reload_calls, runtime.invalidated) == (0, [None])


@pytest.mark.asyncio
async def test_skill_mutations_remain_serialized_through_cancelled_reload(tmp_path: Path) -> None:
    state = _state(tmp_path)
    runtime = state.runtime
    reload_entered = asyncio.Event()
    release_reload = asyncio.Event()

    async def reload_skills() -> None:
        reload_entered.set()
        await release_reload.wait()
        runtime.reload_calls += 1

    runtime.reload_skills_async = reload_skills
    first = asyncio.create_task(call(state, "skill.set_disabled", name="deploy", disabled=True))
    second = None
    try:
        await asyncio.wait_for(reload_entered.wait(), 2)
        first.cancel()
        await asyncio.sleep(0)
        first.cancel()
        second = asyncio.create_task(
            call(state, "skill.set_disabled", name="deploy", disabled=False)
        )
        await asyncio.sleep(0)
        assert not first.done()
        assert not second.done()
        assert len(runtime.policy_calls) == 1
    finally:
        release_reload.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        if second is not None:
            assert (await second)["ok"] is True
    assert runtime.policy_calls[-1] == ("set_disabled", "deploy", False)
    assert runtime.reload_calls == 2
    assert resource_changes(state) == [{"kind": "skills"}, {"kind": "skills"}]
