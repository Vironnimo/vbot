"""Skill scans during Extension mutations respect the async Runtime boundary."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from core.extensions.extensions import ExtensionRegistry
from core.runtime.runtime import Runtime
from core.skills.skills import SkillRegistry
from core.utils.config import Config


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["reload", "disable"])
@pytest.mark.parametrize("cancel_caller", [False, True])
async def test_extension_skill_scan_yields_and_settles_before_shutdown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    cancel_caller: bool,
) -> None:
    runtime = Runtime(Config(data_dir=tmp_path / "data"), safe_startup_mode="test")
    runtime.start()
    package = runtime.global_skills_dir / "extension-refresh-fixture"
    package.mkdir(parents=True)
    (package / "SKILL.md").write_text(
        "---\nname: extension-refresh-fixture\ndescription: Refresh fixture.\n---\nBody\n",
        encoding="utf-8",
    )
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    entered = asyncio.Event()
    release = threading.Event()
    installed: list[SkillRegistry] = []
    owner = runtime._skill_operations()
    original_load = owner.load_global_registry
    original_apply = runtime._apply_reloaded_skills

    def scan() -> SkillRegistry:
        # Fail immediately on the old synchronous path instead of blocking the
        # test loop on the barrier that the loop itself must release.
        assert threading.get_ident() != loop_thread
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(10)
        return original_load()

    def install(skills: SkillRegistry, *, generation: object) -> None:
        assert threading.get_ident() == loop_thread
        assert runtime._started
        original_apply(skills, generation=generation)
        assert runtime.skills is skills
        installed.append(skills)

    async def load_extensions(*_args, **_kwargs) -> ExtensionRegistry:
        return ExtensionRegistry()

    monkeypatch.setattr(owner, "load_global_registry", scan)
    monkeypatch.setattr(runtime, "_apply_reloaded_skills", install)
    monkeypatch.setattr(ExtensionRegistry, "aload", load_extensions)
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
        assert entered.is_set()
        assert not installed
        with pytest.raises(KeyError):
            runtime.skills.get("extension-refresh-fixture")

        if cancel_caller:
            mutation.cancel()
            await asyncio.sleep(0)
            assert not mutation.done()

        closing = asyncio.create_task(runtime.aclose())
        await asyncio.sleep(0)
        assert not closing.done()
        assert runtime._started
        release.set()
        if cancel_caller:
            with pytest.raises(asyncio.CancelledError):
                await mutation
        else:
            await mutation
        await closing
        assert len(installed) == 1
        assert installed[0].get("extension-refresh-fixture").name == "extension-refresh-fixture"
        assert not runtime._started
    finally:
        release.set()
        scan_started.cancel()
        await asyncio.gather(mutation, scan_started, return_exceptions=True)
        if closing is not None:
            await closing
        await runtime.aclose()
