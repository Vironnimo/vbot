"""Bash: modes behavior."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import core.tools.bash as bash_module
from core.tools.bash import (
    _resolve_workdir,
    bash_handler,
    register_bash_tool,
)
from core.tools.contracts import ToolContractError
from core.tools.process import PROCESS_TOOL_NAME, make_process_handler
from core.tools.process_manager import ProcessManager
from core.tools.tools import (
    ToolContext,
    ToolRegistry,
)
from tests.core.tools.bash_helpers import (
    AGENT_ID,
    RUN_ID,
    delivered_future,
    kill_background,
    make_context,
    make_spool_manager,
    python_command,
)
from tests.core.tools.bash_helpers import (
    manager as manager,
)
from tests.core.tools.bash_helpers import (
    shell_env_cache as shell_env_cache,
)


async def _dispatch_bash(
    manager: ProcessManager, context: ToolContext, arguments: dict[str, Any]
) -> dict[str, Any]:
    registry = ToolRegistry()
    register_bash_tool(registry, manager)
    return await registry.dispatch(context, arguments)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [None, "foreground"])
async def test_background_after_expiry_backgrounds_running_process(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str | None,
) -> None:
    assert bash_module.FOREGROUND_HANDOFF_SECONDS == 90
    monkeypatch.setattr(bash_module, "FOREGROUND_HANDOFF_SECONDS", 0.01)
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    context = make_context(tmp_path)

    result = await bash_handler(
        context,
        {
            "command": "import time; time.sleep(30)",
            **({"mode": mode} if mode is not None else {}),
        },
        manager,
    )

    assert result["ok"] is True
    assert result["data"]["status"] == "running"
    assert "mode" not in result["data"]
    assert isinstance(result["data"]["handoff_note"], str)
    assert result["data"]["handoff_note"]

    await kill_background(manager, result)


@pytest.mark.asyncio
async def test_automatic_handoff_includes_capped_output_and_usable_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bash_module, "FOREGROUND_HANDOFF_SECONDS", 0.5)
    spool_manager = make_spool_manager(tmp_path)
    try:
        monkeypatch.setattr(bash_module, "_shell_argv", python_command)
        context = make_context(tmp_path)

        result = await bash_handler(
            context,
            {
                "command": (
                    "print('x' * 5000 + 'HANDOFF-END', flush=True); import time; time.sleep(30)"
                ),
                "mode": "foreground",
            },
            spool_manager,
        )

        assert result["ok"] is True
        data = result["data"]
        assert data["status"] == "running"
        assert "mode" not in data
        assert data["delivery"] == "automatic"
        assert "process_note" not in data
        process_id = data["process_id"]
        assert isinstance(process_id, str) and process_id
        # Handoff can beat child startup or stdout collection. Its snapshot is
        # bounded even when the command has not produced its output yet.
        assert isinstance(data["truncated"], bool)
        assert len(data["output"]) <= 4000
        if "HANDOFF-END" in data["output"]:
            assert data["truncated"] is True
            assert "[earlier output truncated" in data["output"]
        log_file = Path(data["log_file"])
        assert log_file.exists()

        process_context = ToolContext(
            agent_id=AGENT_ID,
            session_id=context.session_id,
            run_id=RUN_ID,
            tool_call_id="call-process",
            tool_name=PROCESS_TOOL_NAME,
            tool_call_index=1,
            workspace=tmp_path,
            vbot_root=tmp_path,
            data_root=tmp_path,
        )
        process_result = await make_process_handler(spool_manager)(
            process_context,
            {
                "action": "wait",
                "process_id": process_id,
                "pattern": "HANDOFF-END",
                "timeout": 10,
            },
        )

        assert process_result["ok"] is True
        process_data = process_result["data"]
        assert process_data["process_id"] == process_id
        assert process_data["status"] == "running"
        assert "matched" in process_data
        assert process_data["truncated"] is True
        assert len(process_data["output"]) <= 4000
        assert process_data["output"].replace("\r\n", "\n").endswith("HANDOFF-END\n")
        assert "[earlier output truncated" in process_data["output"]
        assert "x" * 5000 + "HANDOFF-END" in log_file.read_text(encoding="utf-8")
    finally:
        tracked_processes = spool_manager.list_processes(AGENT_ID)
        for tracked in tracked_processes:
            if tracked.status == "running":
                await spool_manager.kill(tracked.process_id, AGENT_ID)
        await spool_manager.aclose()


@pytest.mark.asyncio
async def test_short_foreground_command_finishes_inline(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    watcher_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    monkeypatch.setattr(
        bash_module,
        "_maybe_spawn_completion_watcher",
        lambda *args, **kwargs: watcher_calls.append((args, kwargs)),
    )
    context = make_context(tmp_path)

    result = await bash_handler(
        context,
        {
            "command": "import time; time.sleep(0.05); print('finished-inline')",
            "mode": "foreground",
        },
        manager,
    )

    assert result["ok"] is True
    assert result["data"]["status"] == "completed"
    assert "mode" not in result["data"]
    assert "finished-inline" in result["data"]["output"]
    assert watcher_calls == []


@pytest.mark.asyncio
async def test_handoff_delay_from_other_harnesses_keeps_the_requested_mode(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # OpenClaw yieldMs and the retired background_after_seconds only move the
    # moment control returns; vBot hands off after its own delay instead.
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)

    result = await _dispatch_bash(
        manager,
        make_context(tmp_path),
        {"command": "print('ran inline')", "mode": "foreground", "yieldMs": 10000},
    )

    assert result["ok"] is True
    assert result["data"]["status"] == "completed"
    assert "ran inline" in result["data"]["output"]


@pytest.mark.asyncio
async def test_zero_handoff_delay_means_background_now(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)

    result = await _dispatch_bash(
        manager,
        make_context(tmp_path),
        {"command": "import time; time.sleep(30)", "background_after_seconds": 0},
    )

    assert result["data"]["status"] == "running"
    assert result["data"]["delivery"] == "automatic"
    await kill_background(manager, result)


@pytest.mark.asyncio
async def test_zero_handoff_delay_conflicts_with_foreground_mode(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)

    with pytest.raises(
        ValueError, match='mode is "foreground" but yield_after asks for background'
    ):
        await _dispatch_bash(
            manager,
            make_context(tmp_path),
            {"command": "print('never runs')", "mode": "foreground", "yield_after": 0},
        )

    assert manager.list_processes(AGENT_ID) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("nesting_depth", [0, 1])
async def test_omitted_execution_mode_defaults_to_foreground(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    nesting_depth: int,
) -> None:
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    context = make_context(tmp_path, nesting_depth=nesting_depth)

    result = await bash_handler(
        context,
        {"command": "print('default-foreground')"},
        manager,
    )

    assert result["ok"] is True
    assert result["data"]["status"] == "completed"
    assert "mode" not in result["data"]
    assert result["data"]["output"].strip() == "default-foreground"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["front", "auto"])
async def test_invalid_execution_mode_is_rejected_before_spawn(
    manager: ProcessManager,
    tmp_path: Path,
    mode: str,
) -> None:
    result = await bash_handler(
        make_context(tmp_path),
        {"command": "print('never runs')", "mode": mode},
        manager,
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_arguments"
    assert manager.list_processes(AGENT_ID) == []


@pytest.mark.asyncio
async def test_explicit_background_at_depth_is_rejected_without_spawning(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sub-agent's explicit background request fails before any process spawns."""
    watcher_calls: list[Any] = []
    trigger_calls: list[str] = []

    def record_watcher(*args: Any, **kwargs: Any) -> None:
        watcher_calls.append((args, kwargs))

    class RecordingTriggerService:
        def submit_completion(self, *_args: Any, **_kwargs: Any) -> asyncio.Future[None]:
            trigger_calls.append("called")
            return delivered_future()

    monkeypatch.setattr(bash_module, "_maybe_spawn_completion_watcher", record_watcher)
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    context = make_context(tmp_path, nesting_depth=1)

    result = await bash_handler(
        context,
        {"command": "import time; time.sleep(30)", "mode": "background"},
        manager,
        trigger_service=RecordingTriggerService(),
    )

    assert result["ok"] is False
    assert result["error"]["code"] == bash_module.BACKGROUND_AT_DEPTH_FAILURE_CODE
    assert watcher_calls == []
    await asyncio.sleep(0)
    assert trigger_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("depth", [1, 3])
async def test_subagent_waits_past_handoff_window_without_registering_background(
    manager, tmp_path, monkeypatch, depth
):
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    monkeypatch.setattr(bash_module, "FOREGROUND_HANDOFF_SECONDS", 0.01)
    background_hooks = []
    context = replace(
        make_context(tmp_path, nesting_depth=depth),
        background_registration_hook=background_hooks.append,
    )
    result = await asyncio.wait_for(
        bash_handler(
            context,
            {"command": "import time; time.sleep(0.1); print('finished')", "timeout": 0},
            manager,
        ),
        5,
    )
    assert result["ok"]
    assert result["data"]["status"] == "completed"
    assert result["data"]["output"].strip() == "finished"
    assert "delivery" not in result["data"]
    assert background_hooks == []
    assert not manager.list_processes(AGENT_ID)[0].backgrounded


@pytest.mark.asyncio
async def test_fast_foreground_command_at_depth_succeeds(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A short Sub-Agent command finishes inline."""
    watcher_calls: list[Any] = []

    def record_watcher(*args: Any, **kwargs: Any) -> None:
        watcher_calls.append((args, kwargs))

    monkeypatch.setattr(bash_module, "_maybe_spawn_completion_watcher", record_watcher)
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    context = make_context(tmp_path, nesting_depth=1)

    result = await bash_handler(
        context,
        {"command": "print('quick')", "mode": "foreground"},
        manager,
    )

    assert result["ok"] is True
    assert result["data"]["status"] == "completed"
    assert "quick" in result["data"]["output"]
    assert watcher_calls == []


@pytest.mark.asyncio
async def test_background_at_top_level_is_not_blocked(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: at depth 0 an explicit background request still backgrounds and watches."""
    trigger_called = asyncio.Event()

    class MockTriggerService:
        def submit_completion(
            self,
            _agent_id: str,
            session_id: str,
            *,
            notice_id: str,
            origin_run_id: str,
            body: str,
            project_id: str | None = None,
            execution_owner: object | None = None,
        ) -> asyncio.Future[None]:
            assert session_id
            assert notice_id.startswith("bash:")
            assert origin_run_id == context.run_id
            assert body
            trigger_called.set()
            return delivered_future()

    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    context = make_context(tmp_path, nesting_depth=0)

    result = await bash_handler(
        context,
        {"command": "import sys; sys.exit(0)", "mode": "background"},
        manager,
        trigger_service=MockTriggerService(),
    )

    assert result["ok"] is True
    assert result["data"]["status"] == "running"
    await asyncio.wait_for(trigger_called.wait(), timeout=2)


@pytest.mark.asyncio
async def test_non_zero_exit_code_is_successful_tool_result(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    context = make_context(tmp_path)

    result = await bash_handler(
        context,
        {
            "command": "import sys; print('bad', file=sys.stderr); raise SystemExit(7)",
            "mode": "foreground",
        },
        manager,
    )

    assert result["ok"] is True
    assert result["data"]["status"] == "completed"
    assert result["data"]["exit_code"] == 7
    assert "bad" in result["data"]["output"]


@pytest.mark.asyncio
async def test_foreground_failure_includes_hint_field(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    context = make_context(tmp_path)

    result = await bash_handler(
        context,
        {
            "command": (
                "import sys; sys.stderr.write('bash: python: command not found\\n'); sys.exit(127)"
            ),
            "mode": "foreground",
        },
        manager,
    )

    assert result["ok"] is True
    assert result["data"]["status"] == "completed"
    assert result["data"]["exit_code"] == 127
    assert result["data"]["hint"] == (
        "This system has no bare `python` — use `python3`, or the project "
        "venv's interpreter (e.g. .venv/bin/python)."
    )


@pytest.mark.asyncio
async def test_foreground_success_omits_hint(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    context = make_context(tmp_path)

    result = await bash_handler(
        context,
        {"command": "print('ok')", "mode": "foreground"},
        manager,
    )

    assert result["ok"] is True
    assert result["data"]["status"] == "completed"
    assert result["data"]["exit_code"] == 0
    assert "hint" not in result["data"]


@pytest.mark.asyncio
async def test_spawn_failure_returns_failure_envelope(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bash_module, "_shell_argv", lambda command: ["missing-vbot-shell"])
    context = make_context(tmp_path)

    result = await bash_handler(
        context,
        {"command": "ignored", "mode": "foreground"},
        manager,
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "process_spawn_failed"


def test_resolve_workdir_defaults_to_cwd_not_workspace(tmp_path: Path) -> None:
    # A project session sets cwd to the repo; with no workdir argument, bash
    # must default its working directory to the cwd, not the agent workspace.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    context = make_context(workspace, cwd=repo)

    assert _resolve_workdir(context, None) == repo.resolve()


def test_resolve_workdir_defaults_to_workspace_without_cwd(tmp_path: Path) -> None:
    # No project cwd: the working directory stays the workspace, today's behavior.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    context = make_context(workspace)

    assert _resolve_workdir(context, None) == workspace.resolve()


def test_resolve_workdir_resolves_relative_workdir_against_cwd(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repo = tmp_path / "repo"
    (repo / "sub").mkdir(parents=True)
    context = make_context(workspace, cwd=repo)

    assert _resolve_workdir(context, "sub") == (repo / "sub").resolve()


@pytest.mark.asyncio
async def test_bash_runs_in_cwd_when_no_workdir_argument(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # End-to-end: the spawned process runs in the cwd, so a relative-path write
    # lands in the repo (cwd), not the agent workspace.
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    context = make_context(workspace, cwd=repo)

    result = await bash_handler(
        context,
        {
            "command": "open('marker.txt', 'w').write('here')",
            "mode": "foreground",
        },
        manager,
    )

    assert result["ok"] is True
    assert result["data"]["status"] == "completed"
    assert result["data"]["exit_code"] == 0
    assert (repo / "marker.txt").read_text(encoding="utf-8") == "here"
    assert not (workspace / "marker.txt").exists()


@pytest.mark.asyncio
async def test_env_argument_must_be_an_object(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)

    with pytest.raises(ToolContractError, match='"env" must be an object'):
        await _dispatch_bash(
            manager,
            make_context(tmp_path),
            {"command": "print('never runs')", "env": "SAFE_VALUE=1"},
        )

    assert manager.list_processes(AGENT_ID) == []


@pytest.mark.asyncio
async def test_description_is_accepted_without_affecting_command_execution(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    description = "Run the focused Bash verification command with a deliberately long title"

    result = await bash_handler(
        make_context(tmp_path),
        {
            "command": "print('done')",
            "description": description,
            "mode": "foreground",
        },
        manager,
    )

    assert result["ok"] is True
    assert result["data"]["output"].strip() == "done"


@pytest.mark.asyncio
async def test_non_string_description_is_rejected(
    manager: ProcessManager,
    tmp_path: Path,
) -> None:
    result = await bash_handler(
        make_context(tmp_path),
        {"command": "echo ignored", "description": 123, "mode": "foreground"},
        manager,
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_arguments"


@pytest.mark.asyncio
async def test_workdir_defaults_to_workspace(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    context = make_context(tmp_path)

    result = await bash_handler(
        context,
        {"command": "import os; print(os.getcwd())", "mode": "foreground"},
        manager,
    )

    assert result["ok"] is True
    assert result["data"]["output"].strip() == str(tmp_path)
