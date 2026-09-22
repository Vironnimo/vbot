"""Shared Skill writes remain attached to the lifetime of their owning Agent."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.runtime.runtime import Runtime
from core.skills import authoring as authoring_module
from core.tools import SKILL_MANAGE_TOOL_NAME, ToolContext
from core.utils.config import Config
from server.rpc import agent_methods
from server.rpc.agent_methods import _delete_agent, _rename_agent


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["delete", "rename"])
@pytest.mark.parametrize("write_first", [False, True])
@pytest.mark.parametrize("reload_tools", [False, True])
async def test_shared_write_serializes_with_owner_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    write_first: bool,
    reload_tools: bool,
) -> None:
    runtime = Runtime(Config(data_dir=tmp_path / "data"), safe_startup_mode="test")
    runtime.start()
    release = threading.Event()
    tasks: list[asyncio.Task[Any]] = []
    try:
        runtime.agents.create("receiver")
        root = runtime.agent_skills_dir("main")
        runtime.skill_authoring.create(
            root,
            "demo",
            "---\nname: demo\ndescription: Shared fixture\n---\nBefore\n",
            author="human",
        )
        runtime.skill_policy.set_shared("main", "demo", shared=True, receivers=["receiver"])
        if reload_tools:
            runtime.reload_skills()
        before = runtime.skills_for(None, "receiver")
        state = SimpleNamespace(runtime=runtime, chat_runs=runtime.chat_runs)
        context = ToolContext(
            agent_id="receiver",
            session_id="receiver-session",
            run_id="receiver-run",
            tool_call_id="write",
            tool_name=SKILL_MANAGE_TOOL_NAME,
            tool_call_index=0,
            workspace=tmp_path,
            cwd=tmp_path,
            vbot_root=tmp_path,
            data_root=tmp_path / "data",
        )
        loop = asyncio.get_running_loop()
        loop_thread = threading.get_ident()
        entered = asyncio.Event()
        attempted = asyncio.Event()
        atomic_write = authoring_module.atomic_write_bytes

        def blocked_write(path: Path, content: bytes, **kwargs: Any) -> None:
            if path == root / "demo" / "SKILL.md":
                assert threading.get_ident() != loop_thread
                loop.call_soon_threadsafe(entered.set)
                assert release.wait(10)
            atomic_write(path, content, **kwargs)

        async def lifecycle() -> Any:
            if operation == "delete":
                return await _delete_agent(state, {"id": "main"})
            return await _rename_agent(state, {"id": "main", "new_id": "renamed"})

        def start_write() -> asyncio.Task[Any]:
            return asyncio.create_task(
                runtime.tools.dispatch(
                    context,
                    {"action": "patch", "name": "demo", "match": "Before", "content": "After"},
                    [SKILL_MANAGE_TOOL_NAME],
                )
            )

        if write_first:
            monkeypatch.setattr(authoring_module, "atomic_write_bytes", blocked_write)
            write = start_write()
            tasks.append(write)
            await asyncio.wait_for(entered.wait(), 10)
            # Observe the first AgentStore call in each lifecycle path. A
            # nonblocking probe proves contention without sleeping or blocking
            # the test loop on a lock that the loop must release.
            method_name = "list" if operation == "delete" else "rename"
            original = getattr(runtime.agents, method_name)

            def observe_lifecycle(*args: Any, **kwargs: Any) -> Any:
                assert threading.get_ident() != loop_thread
                acquired = runtime.agents._write_lock.acquire(blocking=False)
                if acquired:
                    runtime.agents._write_lock.release()
                loop.call_soon_threadsafe(attempted.set)
                assert not acquired
                return original(*args, **kwargs)

            monkeypatch.setattr(runtime.agents, method_name, observe_lifecycle)
            change = asyncio.create_task(lifecycle())
            tasks.append(change)
            await asyncio.wait_for(attempted.wait(), 10)
            assert not change.done()
            monkeypatch.setattr(runtime.agents, method_name, original)
            release.set()
            assert (await write)["ok"] is True
            await change
        else:
            await lifecycle()
            result = await start_write()
            assert result["ok"] is False

        assert not (runtime.agents.data_dir / "agents" / "main").exists()
        destination = (
            runtime.agents.data_dir / "archive" / "agents" / "main" / "agent" / "skills"
            if operation == "delete"
            else runtime.agent_skills_dir("renamed")
        )
        assert (
            (destination / "demo" / "SKILL.md")
            .read_text(encoding="utf-8")
            .endswith("After\n" if write_first else "Before\n")
        )
        assert runtime.skills_for(None, "receiver") is not before
        assert "demo" not in {
            skill.name for skill in runtime.skills_for(None, "receiver").list_all()
        }
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await runtime.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["delete", "rename"])
@pytest.mark.parametrize("admitted", [False, True])
async def test_lifecycle_cancellation_preserves_admission_and_invalidation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str, admitted: bool
) -> None:
    runtime = Runtime(Config(data_dir=tmp_path / "data"), safe_startup_mode="test")
    runtime.start()
    release = threading.Event()
    task: asyncio.Task[Any] | None = None
    reference_lock = asyncio.Lock()
    try:
        runtime.agents.create("receiver")
        root = runtime.agent_skills_dir("main")
        runtime.skill_authoring.create(root, "demo", "Before", author="human")
        runtime.skill_policy.set_shared("main", "demo", shared=True, receivers=["receiver"])
        before = runtime.skills_for(None, "receiver")
        state = SimpleNamespace(
            runtime=runtime, chat_runs=runtime.chat_runs, agent_delete_lock=reference_lock
        )
        published: list[str] = []
        monkeypatch.setattr(
            agent_methods,
            "publish_resource_changed",
            lambda _state, kind, **_kwargs: published.append(kind),
        )
        loop = asyncio.get_running_loop()
        committed = asyncio.Event()
        original = getattr(runtime.agents, operation)

        def paused_after_commit(*args: Any, **kwargs: Any) -> Any:
            result = original(*args, **kwargs)
            loop.call_soon_threadsafe(committed.set)
            assert release.wait(10)
            return result

        monkeypatch.setattr(runtime.agents, operation, paused_after_commit)
        if not admitted:
            await reference_lock.acquire()

        async def lifecycle() -> Any:
            if operation == "delete":
                return await _delete_agent(state, {"id": "main"})
            return await _rename_agent(state, {"id": "main", "new_id": "renamed"})

        task = asyncio.create_task(lifecycle())
        if admitted:
            await asyncio.wait_for(committed.wait(), 10)
        else:
            await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        if admitted:
            assert not task.done()
            assert reference_lock.locked()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        if admitted:
            assert not reference_lock.locked()
            assert not runtime.agents.exists("main")
            assert runtime.skills_for(None, "receiver") is not before
            assert "agents" in published
        else:
            assert runtime.agents.exists("main")
            assert not committed.is_set()
            assert published == []
    finally:
        release.set()
        if reference_lock.locked() and not admitted:
            reference_lock.release()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        await runtime.aclose()
