"""Shell Tool handoff to the background, background completion, and handoff snapshots."""

from __future__ import annotations

import asyncio
import json
import sys
import types
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import core.tools._bash_results as bash_results
import core.tools.bash as bash_module
from core.chat import ChatMessage
from core.tools.bash import background_bash_statuses, bash_handler
from core.tools.process import PROCESS_TOOL_NAME, make_process_handler
from core.tools.process_manager import ProcessManager
from core.tools.tools import ToolContext, tool_success
from tests.core.tools.bash_test_support import (
    AGENT_ID,
    RUN_ID,
    RecordingTrigger,
    kill_background,
    make_context,
    make_spool_manager,
    python_command,
)
from tests.core.tools.bash_test_support import manager as manager
from tests.core.tools.bash_test_support import shell_env_cache as shell_env_cache

_WAIT_FOR_RELEASE = (
    "from pathlib import Path\nimport time\nprint('ready', flush=True)\n"
    "while not Path('release').exists():\n    time.sleep(0.01)\nprint('finished')"
)


def _process_context(tmp_path: Path, **fields: Any) -> ToolContext:
    return ToolContext(
        agent_id=AGENT_ID,
        session_id="session-a",
        run_id=RUN_ID,
        tool_call_id="call-process",
        tool_name=PROCESS_TOOL_NAME,
        tool_call_index=1,
        workspace=tmp_path,
        vbot_root=tmp_path,
        data_root=tmp_path,
        **fields,
    )


# --- Handoff ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_foreground_hands_off_after_its_delay_and_delivers_the_result_later(
    manager: ProcessManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert bash_module.FOREGROUND_HANDOFF_SECONDS == 90
    monkeypatch.setattr(bash_module, "FOREGROUND_HANDOFF_SECONDS", 0.01)
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    trigger = RecordingTrigger()

    result = await bash_handler(
        make_context(tmp_path),
        {"command": "import time; print('yield-marker', flush=True); time.sleep(0.2)"},
        manager,
        trigger_service=trigger,
    )

    data = result["data"]
    assert result["ok"] is True
    assert (data["status"], data["delivery"]) == ("running", "automatic")
    assert "mode" not in data
    assert "handed off to vBot after 0.01 seconds" in data["handoff_note"]
    # A foreground command was expected to finish: its result arrives on its own,
    # so the note ends the turn instead of pointing to a wait.
    assert '"wait"' not in data["handoff_note"]
    assert "end your turn" in data["handoff_note"]
    assert "yield-marker" in await trigger.body()


@pytest.mark.asyncio
async def test_automatic_handoff_includes_capped_output_and_usable_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bash_module, "FOREGROUND_HANDOFF_SECONDS", 0.5)
    spool_manager = make_spool_manager(tmp_path)
    try:
        monkeypatch.setattr(bash_module, "_shell_argv", python_command)

        result = await bash_handler(
            make_context(tmp_path),
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
        assert data["delivery"] == "automatic"
        assert "process_note" not in data
        process_id = data["process_id"]
        # Handoff can beat child startup or stdout collection. Its snapshot is
        # bounded even when the command has not produced its output yet.
        assert data.get("truncated") in {None, True}
        assert len(data["output"]) <= 4000
        if "HANDOFF-END" in data["output"]:
            assert data["truncated"] is True
            assert "[earlier output truncated" in data["output"]
        log_file = Path(data["log_file"])
        assert log_file.exists()

        process_result = await make_process_handler(spool_manager)(
            _process_context(tmp_path),
            {"action": "wait", "process_id": process_id, "pattern": "HANDOFF-END", "timeout": 10},
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
        for tracked in spool_manager.list_processes(AGENT_ID):
            if tracked.status == "running":
                await spool_manager.kill(tracked.process_id, AGENT_ID)
        await spool_manager.aclose()


@pytest.mark.asyncio
async def test_user_handoff_preserves_process_and_automatic_delivery(
    manager: ProcessManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.runs import Run

    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    run = Run(run_id=RUN_ID, agent_id=AGENT_ID, session_id="session-a")
    run.begin_tool_call("call-a")
    ready = asyncio.Event()
    trigger = RecordingTrigger()

    def register(callback: Callable[[], bool]) -> None:
        run.register_tool_background("call-a", callback)
        ready.set()

    context = replace(make_context(tmp_path), background_registration_hook=register)
    task = asyncio.create_task(
        bash_handler(context, {"command": _WAIT_FOR_RELEASE}, manager, trigger_service=trigger)
    )
    await asyncio.wait_for(ready.wait(), 5)
    assert run.background_tool_call("call-a")
    result = await asyncio.wait_for(task, 5)

    assert result["ok"] and result["data"]["delivery"] == "automatic"
    handoff_note = result["data"]["handoff_note"]
    assert handoff_note.startswith("The user moved this command to the background after ")
    assert '"wait"' not in handoff_note
    assert "end your turn" in handoff_note
    process_id = result["data"]["process_id"]
    assert manager.get_process(process_id, AGENT_ID).status == "running"
    (tmp_path / "release").write_text("continue", encoding="utf-8")
    assert "finished" in await trigger.body()


@pytest.mark.asyncio
async def test_a_sub_agent_waits_for_foreground_commands_without_handoff(
    manager: ProcessManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    monkeypatch.setattr(bash_module, "FOREGROUND_HANDOFF_SECONDS", 0.01)
    background_hooks: list[Callable[[], bool]] = []
    context = replace(
        make_context(tmp_path, nesting_depth=1),
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
async def test_explicit_background_at_depth_is_rejected_without_spawning(
    manager: ProcessManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trigger = RecordingTrigger()
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)

    result = await bash_handler(
        make_context(tmp_path, nesting_depth=1),
        {"command": "import time; time.sleep(30)", "mode": "background"},
        manager,
        trigger_service=trigger,
    )

    assert result["ok"] is False
    assert result["error"]["code"] == bash_module.BACKGROUND_AT_DEPTH_FAILURE_CODE
    assert manager.list_processes(AGENT_ID) == []
    await asyncio.sleep(0)
    assert trigger.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("timeout", "limit"),
    [
        (None, "No tool timeout applies to this command."),
        (120, "A tool timeout of 120 s still applies"),
    ],
)
async def test_background_mode_returns_a_running_process_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, timeout: int | None, limit: str
) -> None:
    watcher_started = asyncio.Event()

    async def unexpected_watch(*_args: Any, **_kwargs: Any) -> None:
        watcher_started.set()

    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    monkeypatch.setattr(bash_module, "_watch_background_process", unexpected_watch)
    spool_manager = make_spool_manager(tmp_path)
    try:
        result = await bash_handler(
            make_context(tmp_path),
            {
                "command": "import time; time.sleep(30)",
                "mode": "background",
                **({"timeout": timeout} if timeout is not None else {}),
            },
            spool_manager,
        )

        data = result["data"]
        assert result["ok"] is True
        assert (data["status"], data["delivery"]) == ("running", "automatic")
        assert "mode" not in data
        assert isinstance(data["process_id"], str)
        assert limit in data["handoff_note"]
        # A background start is typically a server whose ready line the Agent waits for.
        assert 'action "wait"' in data["handoff_note"]
        assert "pattern" in data["handoff_note"]
        assert Path(data["log_file"]).exists()
        # Without a trigger service nothing watches for the completion.
        await asyncio.sleep(0)
        assert watcher_started.is_set() is False
        await kill_background(spool_manager, result)
    finally:
        await spool_manager.aclose()


@pytest.mark.parametrize(("newline", "has_log"), [("\n", False), ("\r\n", True)])
def test_handoff_snapshot_keeps_twenty_newest_lines(tmp_path, newline, has_log):
    tracked = types.SimpleNamespace(
        log_file=tmp_path / "command.log" if has_log else None, truncated=False
    )
    lines = [f"line-{index}\n" for index in range(40)]
    output = "".join(line.replace("\n", newline) for line in lines)

    fields = bash_results._shape_output_fields(tracked, output, handoff=True)

    assert fields["truncated"] is True
    assert fields["output"].split("\n", 1)[1] == "".join(lines[-20:])
    assert len(fields["output"]) <= 4000
    if has_log:
        assert Path(fields["log_file"]) == tracked.log_file


@pytest.mark.parametrize(
    ("output", "already_truncated"),
    [("", False), ("x" * 4000, False), ("x" * 5000, False), ("tiny\n", True), ("x\n" * 20, False)],
)
def test_handoff_snapshot_character_budget_and_upstream_truncation(output, already_truncated):
    tracked = types.SimpleNamespace(log_file=None, truncated=already_truncated)

    fields = bash_results._shape_output_fields(tracked, output, handoff=True)

    truncated = already_truncated or len(output) > 4000
    assert fields.get("truncated", False) is truncated
    assert len(fields["output"]) <= 4000
    if not truncated:
        assert fields["output"] == output
    else:
        assert output.endswith(fields["output"].split("\n", 1)[1])


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["foreground", "background"])
async def test_every_handoff_mode_uses_snapshot(manager, tmp_path, monkeypatch, mode):
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    output = "".join(f"activity-{index}\n" for index in range(40))

    async def captured_output(*args):
        return output

    monkeypatch.setattr(bash_results, "_combined_output", captured_output)
    context = make_context(tmp_path)
    if mode == "foreground":
        context = replace(context, background_registration_hook=lambda callback: callback())
    arguments = {"command": "import sys; sys.stdin.readline()", "mode": mode}
    result = await bash_handler(context, arguments, manager)
    try:
        data = result["data"]
        assert data["status"] == "running"
        assert data["delivery"] == "automatic"
        assert data["truncated"] is True
        assert data["output"].split("\n", 1)[1] == "".join(output.splitlines(True)[-20:])
    finally:
        await kill_background(manager, result)


# --- Background completion -------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("fails", [False, True])
async def test_background_completion_reaches_the_session_that_started_it(
    manager: ProcessManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fails: bool
) -> None:
    from core.runs import RunExecutionOwner

    monkeypatch.setattr(
        bash_module, "_shell_argv", lambda command: [sys.executable, *command.split()[1:]]
    )
    project = tmp_path / "project"
    (project / "shop").mkdir(parents=True)
    (project / "tests").mkdir()
    (project / "tests" / "test_cart.py").write_text(
        "import sys\nassert sys.stdin.read() == ''\nprint('result-marker', flush=True)\n"
        + ("import shop\n" if fails else ""),
        encoding="utf-8",
    )
    owner = RunExecutionOwner("swarm", "group", "peer", "generation", "epoch")
    context = replace(make_context(tmp_path, project_id="acme"), execution_owner=owner)
    trigger = RecordingTrigger()
    command = "python tests/test_cart.py"

    result = await bash_handler(
        context,
        {"command": command, "mode": "background", "workdir": str(project)},
        manager,
        trigger_service=trigger,
    )

    assert result["ok"] is True and result["data"]["status"] == "running"
    body = await trigger.body()
    call = trigger.calls[0]
    assert call["notice_id"].startswith("bash:")
    assert (
        call["agent_id"],
        call["session_id"],
        call["origin_run_id"],
        call["project_id"],
        call["execution_owner"],
    ) == (AGENT_ID, "session-a", RUN_ID, "acme", owner)
    assert f"### Bash process — {'failed' if fails else 'completed'}" in body
    assert "aborted by the user" not in body
    assert f"Command: {command}" in body
    assert f"Exit code: {1 if fails else 0}" in body
    assert "result-marker" in body
    if fails:
        # The hint judges the import failure against the command's workdir.
        assert body.endswith(
            "Run it as a module from the working directory instead: `python -m tests.test_cart`."
        )
    else:
        assert "Hint: " not in body
    # Watching for the completion does not consume what Process shows.
    poll = await manager.poll(result["data"]["process_id"], AGENT_ID, project_id="acme")
    assert "result-marker" in str(poll["output"])


@pytest.mark.asyncio
async def test_background_watcher_reports_aborted_by_user_when_process_is_user_cancelled(
    manager: ProcessManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    trigger = RecordingTrigger()

    result = await bash_handler(
        make_context(tmp_path),
        {"command": "import time; time.sleep(30)", "mode": "background"},
        manager,
        trigger_service=trigger,
    )
    process_id = result["data"]["process_id"]
    await manager.cancel_for_user(process_id, AGENT_ID)

    message = await trigger.body()
    assert "aborted by the user after" in message
    assert "Background process completed." not in message
    assert "Exit code:" not in message
    assert f"Process ID: {process_id}" in message


@pytest.mark.asyncio
async def test_terminal_process_status_cancels_already_pending_completion_delivery(
    manager: ProcessManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    completion_submitted = asyncio.Event()
    completion_cancelled = asyncio.Event()

    class PendingTriggerService:
        def __init__(self) -> None:
            self.delivery: asyncio.Future[None] | None = None

        def submit_completion(
            self,
            agent_id: str,
            session_id: str,
            *,
            notice_id: str,
            origin_run_id: str,
            body: str,
            project_id: str | None = None,
            execution_owner: object | None = None,
        ) -> asyncio.Future[None]:
            assert (agent_id, session_id, project_id) == (AGENT_ID, "session-a", None)
            assert notice_id.startswith("bash:")
            assert origin_run_id == RUN_ID
            assert body
            self.delivery = asyncio.get_running_loop().create_future()
            completion_submitted.set()
            return self.delivery

        def cancel_completion(
            self,
            agent_id: str,
            session_id: str,
            *,
            notice_id: str,
            project_id: str | None = None,
        ) -> bool:
            assert (agent_id, session_id, project_id) == (AGENT_ID, "session-a", None)
            assert notice_id.startswith("bash:")
            if self.delivery is not None and not self.delivery.done():
                self.delivery.cancel()
            completion_cancelled.set()
            return True

    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    bash_result = await bash_handler(
        make_context(tmp_path),
        {"command": "print('done')", "mode": "background"},
        manager,
        trigger_service=PendingTriggerService(),
    )
    process_id = bash_result["data"]["process_id"]
    await asyncio.wait_for(completion_submitted.wait(), timeout=2)

    persisted_callbacks: list[Callable[[], None]] = []
    process_result = await make_process_handler(manager)(
        _process_context(tmp_path, result_persisted_hook=persisted_callbacks.append),
        {"action": "status", "process_id": process_id},
    )

    assert process_result["ok"] is True
    assert process_result["data"]["status"] == "completed"
    assert len(persisted_callbacks) == 1

    persisted_callbacks[0]()

    notification_task = manager.get_process(process_id, AGENT_ID).completion_notification_task
    assert notification_task is not None
    await asyncio.gather(notification_task, return_exceptions=True)
    await asyncio.sleep(0)
    assert notification_task.cancelled() is True
    assert completion_cancelled.is_set() is True


def test_background_bash_statuses_folds_handoffs_manual_results_and_completion_notes() -> None:
    def result(call_id: str, name: str, process_id: str, status: str) -> ChatMessage:
        data = {"process_id": process_id, "status": status}
        if status == "running":
            data["delivery"] = "automatic"
        return ChatMessage.tool(
            tool_call_id=call_id, name=name, content=json.dumps(tool_success(data))
        )

    messages = [
        result("bash-one", "bash", "process-one", "running"),
        result("foreground", "bash", "foreground", "completed"),
        ChatMessage.note(
            "Automatic completion delivery\n\n"
            "### Bash process — completed\n"
            "Process ID: process-one\n"
            "Command: npm test"
        ),
        result("bash-two", "bash", "process-two", "running"),
        result("process-two", "process", "process-two", "killed"),
        ChatMessage.note(
            "### Bash process — aborted by user\nProcess ID: process-three\nCommand: dev server"
        ),
    ]

    assert background_bash_statuses(messages) == {
        "process-one": "completed",
        "process-two": "killed",
        "process-three": "cancelled",
    }
