"""Skill scans cannot publish registries invalidated while their worker I/O ran."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from core.extensions.extensions import ExtensionRegistry
from core.runtime.runtime import Runtime
from core.skills.skills import SkillRegistry
from core.utils.config import Config
from tests.core.runtime.runtime_test_support import write_skill

RELOADED_SKILL_NAME = "runtime-reloaded-skill"


def _requirement_skill(root: Path, name: str, env: str, description: str = "Fixture.") -> Path:
    package = root / name
    package.mkdir(parents=True)
    path = package / "SKILL.md"
    path.write_text(
        f"---\nname: {name}\ndescription: {description}\n"
        f"metadata:\n  vbot:\n    requirements:\n      env: {env}\n---\nBody\n",
        encoding="utf-8",
    )
    return path


def _pause_first_global_scan(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> tuple[asyncio.Event, threading.Event]:
    """Hold the first global Skill scan on its worker after it read the sources.

    Returns the loop-side event set once the scan is held and the release switch.
    Later scans, including synchronous reloads on the loop, pass through.
    """
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    entered = asyncio.Event()
    release = threading.Event()
    first = threading.Lock()
    owner = runtime._skill_operations()  # noqa: SLF001 - the worker scan seam.
    original_load = owner.load_global_registry

    def paused_load() -> SkillRegistry:
        if not first.acquire(blocking=False):
            return original_load()
        # Fail on a synchronous path instead of blocking the loop that must release.
        assert threading.get_ident() != loop_thread
        registry = original_load()
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(10)
        return registry

    monkeypatch.setattr(owner, "load_global_registry", paused_load)
    return entered, release


@pytest.mark.asyncio
@pytest.mark.parametrize("newer_reload", ["sync", "async"])
async def test_older_skill_scan_cannot_overwrite_newer_policy(
    config: Config, monkeypatch: pytest.MonkeyPatch, newer_reload: str
) -> None:
    write_skill(config.data_dir / "skills", RELOADED_SKILL_NAME, "Reload race fixture.")
    runtime = Runtime(config, safe_startup_mode="test")
    runtime.start()
    entered, release = _pause_first_global_scan(runtime, monkeypatch)
    older = asyncio.create_task(runtime.reload_skills_async())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        runtime.skill_policy.set_disabled(RELOADED_SKILL_NAME, disabled=True)
        if newer_reload == "sync":
            runtime.reload_skills()
        else:
            await runtime.reload_skills_async()
        latest = runtime.skills
        with pytest.raises(KeyError):
            latest.get(RELOADED_SKILL_NAME)
        release.set()
        await asyncio.wait_for(older, 5)
        assert runtime.skills is latest
        with pytest.raises(KeyError):
            runtime.skills_for(None, "main").get(RELOADED_SKILL_NAME)
    finally:
        release.set()
        await asyncio.gather(older, return_exceptions=True)
        await runtime.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(("shutdown", "restart"), [("stop", True), ("aclose", False)])
async def test_skill_scan_cannot_publish_across_runtime_shutdown(
    config: Config, monkeypatch: pytest.MonkeyPatch, shutdown: str, restart: bool
) -> None:
    write_skill(config.data_dir / "skills", RELOADED_SKILL_NAME, "Reload race fixture.")
    runtime = Runtime(config, safe_startup_mode="test")
    runtime.start()
    entered, release = _pause_first_global_scan(runtime, monkeypatch)
    older = asyncio.create_task(runtime.reload_skills_async())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        runtime.skill_policy.set_disabled(RELOADED_SKILL_NAME, disabled=True)
        if shutdown == "stop":
            runtime.stop()
        else:
            await runtime.aclose()
        if restart:
            runtime.start()
            latest = runtime.skills
        release.set()
        await asyncio.wait_for(older, 5)
        if restart:
            assert runtime.skills is latest
            with pytest.raises(KeyError):
                runtime.skills.get(RELOADED_SKILL_NAME)
        else:
            with pytest.raises(RuntimeError, match="Runtime not started"):
                runtime.skills  # noqa: B018 - the stopped Runtime published nothing.
    finally:
        release.set()
        await asyncio.gather(older, return_exceptions=True)
        await runtime.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("credential_change", ["add", "process_override"])
async def test_global_scan_keeps_latest_credential_availability(
    config: Config, monkeypatch: pytest.MonkeyPatch, credential_change: str
) -> None:
    key = "VBOT_GLOBAL_SKILL_SCAN_CREDENTIAL"
    monkeypatch.delenv(key, raising=False)
    runtime = Runtime(config, safe_startup_mode="test")
    runtime.start()
    _requirement_skill(runtime.global_skills_dir, RELOADED_SKILL_NAME, key)
    if credential_change == "process_override":
        runtime.storage.set_data_dir_credential(key, "test-initial")
        runtime.reload_environment_credentials()
    runtime.reload_skills()
    held_registry = runtime.skills
    write_skill(runtime.global_skills_dir, "added-during-credential-refresh", "Pending addition.")
    entered, release = _pause_first_global_scan(runtime, monkeypatch)
    pending = asyncio.create_task(runtime.reload_skills_async())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        if credential_change == "add":
            runtime.storage.set_data_dir_credential(key, "test-current")
            expected = "available"
        else:
            # Even an explicitly empty process value takes precedence over the
            # file snapshot captured by the pending scan.
            monkeypatch.setenv(key, "")
            expected = "unavailable"
        runtime.reload_environment_credentials()
        assert held_registry.availability_for(RELOADED_SKILL_NAME, ["*"]).state == expected
        release.set()
        await pending
        # The scanned package change survives; the availability is the current one.
        assert runtime.skills is not held_registry
        assert runtime.skills.get("added-during-credential-refresh").name
        assert runtime.skills.availability_for(RELOADED_SKILL_NAME, ["*"]).state == expected
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        await runtime.aclose()


def test_credential_reload_updates_existing_skill_registries(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = "VBOT_SKILL_RELOAD_TEST"
    monkeypatch.delenv(key, raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    project = runtime.projects.create("p", "P", repo)
    for home, name in (
        (runtime.global_skills_dir, "global-env"),
        (runtime.agent_skills_dir("main"), "private-env"),
        (repo / ".opencode" / "skills", "project-env"),
    ):
        _requirement_skill(home, name, key, "Requires a credential.")
    runtime.reload_skills()
    scopes = [
        (None, None),
        (None, "main"),
        (project.project_id, None),
        (project.project_id, "main"),
    ]
    registries = [runtime.skills_for(*scope) for scope in scopes]

    def assert_availability(expected: str) -> None:
        for scope, registry in zip(scopes, registries, strict=True):
            # Credential reloads update the cached registries in place.
            assert runtime.skills_for(*scope) is registry
            for skill in registry.list_all():
                if skill.name.endswith("-env"):
                    assert registry.availability_for(skill.name, ["*"]).state == expected

    assert_availability("unavailable")
    runtime.storage.set_data_dir_credential(key, "test-value")
    runtime.reload_environment_credentials()
    assert_availability("available")
    runtime.storage.remove_data_dir_credential(key)
    runtime.reload_environment_credentials()
    assert_availability("unavailable")
    monkeypatch.setenv(key, "process-value")
    runtime.reload_environment_credentials()
    assert_availability("available")


# One case per invalidation source (Project package, Agent package, policy
# reload, credential reload). The Project bundle and the Agent registry (reached
# without and with a Project) each run once with and once without a newer build
# published before the older scan returns.
@pytest.mark.parametrize(
    ("scope", "change", "rebuild_before_release"),
    [
        ("project", "package", False),
        ("project", "policy", True),
        ("agent", "package", True),
        ("rooted_agent", "environment", False),
    ],
)
def test_scoped_scan_rechecks_changes_before_cache_publication(
    config: Config,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scope: str,
    change: str,
    rebuild_before_release: bool,
) -> None:
    key = "VBOT_SCOPED_SKILL_CACHE_TEST"
    monkeypatch.delenv(key, raising=False)
    runtime = Runtime(config, safe_startup_mode="test")
    runtime.start()
    release = threading.Event()
    entered = threading.Event()
    try:
        repo = tmp_path / "repo"
        repo.mkdir()
        project = runtime.projects.create("cache", "Cache", repo)
        project_id = project.project_id if scope != "agent" else None
        agent_id = None if scope == "project" else "main"
        root = (
            repo / ".opencode" / "skills"
            if scope == "project"
            else runtime.agent_skills_dir("main")
        )
        path = _requirement_skill(root, "changing", key, "Before change.")
        owner = runtime._skill_operations()  # noqa: SLF001 - the scoped scan seam.
        method = (
            "_build_project_skill_bundle" if scope == "project" else "_build_agent_skill_registry"
        )
        original_build = getattr(owner, method)

        def paused_build(*args: object) -> object:
            snapshot = original_build(*args)
            if not entered.is_set():
                entered.set()
                assert release.wait(10)
            return snapshot

        monkeypatch.setattr(owner, method, paused_build)
        with ThreadPoolExecutor(max_workers=1) as workers:
            older = workers.submit(runtime.skills_for, project_id, agent_id)
            try:
                assert entered.wait(10)
                if change == "package":
                    path.write_text(
                        path.read_text(encoding="utf-8").replace("Before change", "After change"),
                        encoding="utf-8",
                    )
                    if scope == "project":
                        runtime.invalidate_project_skills(project_id)
                    else:
                        runtime.invalidate_agent_skills("main")
                elif change == "policy":
                    runtime.skill_policy.set_disabled("changing", disabled=True)
                    runtime.reload_skills()
                else:
                    runtime.storage.set_data_dir_credential(key, "test-value")
                    runtime.reload_environment_credentials()
                current = (
                    runtime.skills_for(project_id, agent_id) if rebuild_before_release else None
                )
                release.set()
                result = older.result(timeout=10)
                if current is not None:
                    assert result is current
                assert runtime.skills_for(project_id, agent_id) is result
                if change == "package":
                    assert result.get("changing").description == "After change."
                elif change == "policy":
                    with pytest.raises(KeyError):
                        result.get("changing")
                else:
                    assert result.availability_for("changing", ["*"]).state == "available"
            finally:
                release.set()
    finally:
        release.set()
        runtime.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(("operation", "cancel_caller"), [("reload", False), ("disable", True)])
async def test_extension_skill_scan_yields_and_settles_before_shutdown(
    config: Config, monkeypatch: pytest.MonkeyPatch, operation: str, cancel_caller: bool
) -> None:
    runtime = Runtime(config, safe_startup_mode="test")
    runtime.start()
    write_skill(runtime.global_skills_dir, "extension-refresh-fixture", "Refresh fixture.")
    loop_thread = threading.get_ident()
    installed: list[SkillRegistry] = []
    entered, release = _pause_first_global_scan(runtime, monkeypatch)
    original_apply = runtime._apply_reloaded_skills  # noqa: SLF001 - the loop-side install.

    def install(skills: SkillRegistry, *, generation: object) -> None:
        assert threading.get_ident() == loop_thread
        assert runtime._started  # noqa: SLF001
        original_apply(skills, generation=generation)
        assert runtime.skills is skills
        installed.append(skills)

    async def load_no_extensions(*_args: object, **_kwargs: object) -> ExtensionRegistry:
        return ExtensionRegistry()

    monkeypatch.setattr(runtime, "_apply_reloaded_skills", install)
    monkeypatch.setattr(ExtensionRegistry, "aload", load_no_extensions)
    mutation = asyncio.create_task(
        runtime.reload_extensions()
        if operation == "reload"
        else runtime.apply_extension_disabled_change({"refresh-fixture"})
    )
    scan_started = asyncio.create_task(entered.wait())
    closing: asyncio.Task[None] | None = None
    try:
        done, _ = await asyncio.wait({mutation, scan_started}, return_when=asyncio.FIRST_COMPLETED)
        if mutation in done:
            await mutation
        # The Extension mutation scans on a worker while the loop stays free.
        assert entered.is_set()
        assert not installed
        with pytest.raises(KeyError):
            runtime.skills.get("extension-refresh-fixture")

        if cancel_caller:
            mutation.cancel()
            await asyncio.sleep(0)
            assert not mutation.done()

        # Shutdown waits for the admitted scan to install before closing.
        closing = asyncio.create_task(runtime.aclose())
        await asyncio.sleep(0)
        assert not closing.done()
        assert runtime._started  # noqa: SLF001
        release.set()
        if cancel_caller:
            with pytest.raises(asyncio.CancelledError):
                await mutation
        else:
            await mutation
        await closing
        assert len(installed) == 1
        assert installed[0].get("extension-refresh-fixture").name == "extension-refresh-fixture"
        assert not runtime._started  # noqa: SLF001
    finally:
        release.set()
        scan_started.cancel()
        await asyncio.gather(mutation, scan_started, return_exceptions=True)
        if closing is not None:
            await closing
        await runtime.aclose()
