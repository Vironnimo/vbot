"""Shell Tool registration, foreground execution, the command environment, and spawning."""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import core.tools._bash_environment as bash_environment
import core.tools._bash_results as bash_results
import core.tools.bash as bash_module
from core.tools.bash import (
    BASH_SUBAGENT_TOOL_DESCRIPTION,
    BASH_SUBAGENT_TOOL_PARAMETERS,
    BASH_TOOL_DESCRIPTION,
    BASH_TOOL_PARAMETERS,
    bash_handler,
    project_bash_tool_definitions,
    register_bash_tool,
)
from core.tools.process_manager import ProcessManager
from core.tools.tools import (
    JsonObject,
    ToolCall,
    ToolContext,
    ToolExecutionConfig,
    ToolExecutor,
    ToolRegistry,
    tool_success,
)
from core.utils.processes import subprocess_creation_flags
from tests.core.tools.bash_test_support import (
    AGENT_ID,
    RUN_ID,
    make_context,
    make_spool_manager,
    python_command,
)
from tests.core.tools.bash_test_support import manager as manager
from tests.core.tools.bash_test_support import shell_env_cache as shell_env_cache

# --- Registration ----------------------------------------------------------


def test_register_bash_tool() -> None:
    registry = ToolRegistry()
    manager = ProcessManager(sweep_interval_seconds=3600)

    register_bash_tool(registry, manager)

    tool = registry.get("bash")
    assert tool.description == BASH_TOOL_DESCRIPTION
    assert tool.description
    assert tool.parameters == BASH_TOOL_PARAMETERS
    assert "oneOf" not in tool.parameters
    assert "additionalProperties" not in tool.parameters
    assert set(tool.parameters["properties"]) == {
        "mode",
        "command",
        "description",
        "workdir",
        "timeout",
        "env_keys",
    }
    assert tool.parameters["required"] == ["command"]
    properties = tool.parameters["properties"]
    assert properties["description"]["type"] == "string"
    assert "maxLength" not in tool.parameters["properties"]["description"]
    assert tool.parameters["properties"]["mode"]["enum"] == [
        "foreground",
        "background",
    ]
    env_keys = properties["env_keys"]
    assert env_keys["type"] == "array"
    assert env_keys["items"] == {"type": "string", "minLength": 1}
    assert env_keys["minItems"] == 1
    assert env_keys["uniqueItems"] is True
    assert all(
        isinstance(property_schema.get("description"), str) and property_schema["description"]
        for property_schema in properties.values()
    )
    display = registry.display_for_call(
        "bash",
        {
            "description": "Run the frontend tests",
            "command": "npm test -- --run",
            "mode": "foreground",
        },
    )
    assert display["primary"][0]["value"] == "Run the frontend tests"
    assert display["primary"][0]["kind"] == "description"
    assert tool.parallel_safe is True


def test_subagent_projection_exposes_only_non_handoff_bash_modes() -> None:
    definitions: list[JsonObject] = [
        {
            "name": "bash",
            "description": BASH_TOOL_DESCRIPTION,
            "parameters": BASH_TOOL_PARAMETERS,
        },
        *(
            {"name": name, "description": "Dedicated Tool.", "parameters": {"type": "object"}}
            for name in ("read", "search_files", "apply_patch")
        ),
    ]

    assert project_bash_tool_definitions(definitions, nesting_depth=0) is definitions

    projected = project_bash_tool_definitions(definitions, nesting_depth=1)
    bash_definition = projected[0]

    assert bash_definition["description"] == BASH_SUBAGENT_TOOL_DESCRIPTION
    assert bash_definition["description"]
    assert bash_definition["parameters"] == BASH_SUBAGENT_TOOL_PARAMETERS
    parameters = bash_definition["parameters"]
    assert "oneOf" not in parameters
    assert "additionalProperties" not in parameters
    assert parameters["required"] == ["command"]
    assert "mode" not in parameters["properties"]
    assert projected[1] is definitions[1]
    assert definitions[0]["description"] == BASH_TOOL_DESCRIPTION
    assert definitions[0]["parameters"] == BASH_TOOL_PARAMETERS


USUAL_POINTER = "For reading, searching and editing files use read, search_files and apply_patch. "


@pytest.mark.parametrize(
    ("offered", "nesting_depth", "pointer"),
    [
        (
            ("read", "search_files", "apply_patch", "web_fetch"),
            0,
            "For reading, searching and editing files use read, search_files and apply_patch; "
            "for web pages use web_fetch. ",
        ),
        (("search_files",), 0, "For searching files use search_files. "),
        (("read", "apply_patch"), 0, "For reading and editing files use read and apply_patch. "),
        (("web_fetch",), 0, "For web pages use web_fetch. "),
        ((), 0, ""),
        (("read", "web_fetch"), 1, "For reading files use read; for web pages use web_fetch. "),
        ((), 1, ""),
    ],
)
def test_description_points_only_to_the_dedicated_tools_offered(
    offered: tuple[str, ...], nesting_depth: int, pointer: str
) -> None:
    bash_definition = {
        "name": "bash",
        "description": BASH_TOOL_DESCRIPTION,
        "parameters": BASH_TOOL_PARAMETERS,
    }
    definitions: list[JsonObject] = [
        bash_definition,
        *({"name": name, "description": "Dedicated Tool."} for name in offered),
        {"name": "web_search", "description": "Search the web."},
    ]

    projected = project_bash_tool_definitions(definitions, nesting_depth=nesting_depth)

    base = BASH_SUBAGENT_TOOL_DESCRIPTION if nesting_depth else BASH_TOOL_DESCRIPTION
    assert USUAL_POINTER in base
    assert projected[0]["description"] == base.replace(USUAL_POINTER, pointer)
    assert projected[1:] == definitions[1:]
    assert bash_definition["description"] == BASH_TOOL_DESCRIPTION


@pytest.mark.asyncio
async def test_two_bash_calls_can_run_concurrently_by_default(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active_count = 0
    max_active_count = 0
    both_started = asyncio.Event()

    async def fake_bash_handler(
        context: ToolContext,
        arguments: dict[str, Any],
        process_manager: ProcessManager,
        *,
        trigger_service: Any | None = None,
        credential_resolver: Callable[[str], str] | None = None,
        update_handoffs: Any | None = None,
    ) -> dict[str, Any]:
        nonlocal active_count, max_active_count
        assert process_manager is manager
        assert trigger_service is None
        assert credential_resolver is None
        assert update_handoffs is None
        assert arguments["command"].startswith("download-")
        active_count += 1
        max_active_count = max(max_active_count, active_count)
        if max_active_count == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=1)
        active_count -= 1
        return tool_success({"status": "completed", "call_id": context.tool_call_id})

    monkeypatch.setattr(bash_module, "bash_handler", fake_bash_handler)
    registry = ToolRegistry()
    register_bash_tool(registry, manager)
    executor = ToolExecutor(registry, per_run_limit=2, global_limit=2)

    results = await executor.execute_many(
        [
            ToolCall(
                id="download-1",
                name="bash",
                arguments={"command": "download-one", "mode": "foreground"},
            ),
            ToolCall(
                id="download-2",
                name="bash",
                arguments={"command": "download-two", "mode": "foreground"},
            ),
        ],
        ToolExecutionConfig(
            agent_id=AGENT_ID,
            session_id="session-a",
            run_id=RUN_ID,
            workspace=tmp_path,
            vbot_root=tmp_path,
            data_root=tmp_path,
            allowed_tools=["bash"],
        ),
    )

    assert max_active_count == 2
    assert [result["data"]["call_id"] for result in results] == [
        "download-1",
        "download-2",
    ]


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0.0, "<1s"),
        (0.4, "<1s"),
        (0.6, "1s"),
        (45.2, "45s"),
        (845.0, "14m 5s"),
        (3723.0, "1h 2m 3s"),
    ],
)
def test_format_elapsed_duration_renders_compact_durations(seconds: float, expected: str) -> None:
    """Elapsed abort times render as compact h/m/s strings."""
    assert bash_results._format_elapsed_duration(seconds) == expected


# --- Foreground execution --------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("depth", "script", "exit_code", "stream", "hint"),
    [
        # The default mode runs in the foreground with stdin at EOF.
        (0, "import sys\nassert sys.stdin.read() == ''\nprint('hello')\n", 0, "stdout", None),
        # A failing exit code is still a successful Tool Result. The hint judges
        # the import failure against the command's working directory.
        (
            1,
            "import sys\nprint('bad', file=sys.stderr, flush=True)\nimport shop\n",
            1,
            "stderr",
            "Run it as a module from the working directory instead: `python -m tests.test_cart`.",
        ),
    ],
    ids=["success", "subagent-failure"],
)
async def test_foreground_command_finishes_inline_with_its_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    depth: int,
    script: str,
    exit_code: int,
    stream: str,
    hint: str | None,
) -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    cancel_callbacks: list[Callable[[], None]] = []
    watcher_calls: list[Any] = []

    async def emit_hook(event_type: str, payload: dict[str, Any]) -> None:
        events.append((event_type, payload))

    monkeypatch.setattr(
        bash_module, "_shell_argv", lambda command: [sys.executable, *command.split()[1:]]
    )
    monkeypatch.setattr(
        bash_module,
        "_maybe_spawn_completion_watcher",
        lambda *args, **kwargs: watcher_calls.append(args),
    )
    project = tmp_path / "project"
    (project / "shop").mkdir(parents=True)
    (project / "tests").mkdir()
    (project / "tests" / "test_cart.py").write_text(script, encoding="utf-8")
    context = make_context(
        tmp_path,
        emit_hook=emit_hook,
        nesting_depth=depth,
        cancel_registration_hook=cancel_callbacks.append,
        cancel_check_hook=lambda: False,
    )
    spool_manager = make_spool_manager(tmp_path)
    try:
        result = await bash_handler(
            context,
            {"command": "python tests/test_cart.py", "workdir": str(project)},
            spool_manager,
        )

        assert result["ok"] is True
        data = result["data"]
        assert data["status"] == "completed"
        assert data["exit_code"] == exit_code
        output = data["output"].replace("\r\n", "\n")
        if exit_code == 0:
            assert output == "hello\n"
        else:
            assert output.startswith("bad\n")
        for field in ("mode", "stdout", "stderr", "truncated", "log_file"):
            assert field not in data
        if hint is None:
            assert "hint" not in data
        else:
            assert data["hint"].endswith(hint)
        (process,) = spool_manager.list_processes(AGENT_ID)
        assert {event for event, _payload in events} == {f"tool_call_{stream}"}
        assert all(
            payload["tool_call_id"] == "call-a" and payload["process_id"] == process.process_id
            for _event, payload in events
        )
        assert "".join(payload["data"] for _event, payload in events).replace("\r\n", "\n") == (
            output
        )
        # The user-cancel callback was registered but never fired.
        assert len(cancel_callbacks) == 1
        assert process.cancelled_by_user is False
        assert watcher_calls == []
    finally:
        await spool_manager.aclose()


@pytest.mark.asyncio
async def test_shell_pipeline_and_script_owned_input_remain_available(manager, tmp_path):
    child = (
        "import sys\nvalue = 0\n"
        "for line in sys.stdin:\n"
        "    value += int(line)\n"
        "    print(value, flush=True)\n"
    )
    script = tmp_path / "input test.py"
    script.write_text(
        "import subprocess, sys\n"
        "assert sys.stdin.read().strip() == 'pipeline-input'\n"
        f"child = subprocess.Popen([sys.executable, '-u', '-c', {child!r}], "
        "stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, "
        f"creationflags={subprocess_creation_flags()})\n"
        "try:\n"
        "    child.stdin.write('3\\n'); child.stdin.flush()\n"
        "    observed = int(child.stdout.readline())\n"
        "    assert observed == 3\n"
        "    child.stdin.write(str(10 - observed) + '\\n'); child.stdin.flush()\n"
        "    assert child.stdout.readline().strip() == '10'\n"
        "    child.stdin.close()\n"
        "    assert child.wait(timeout=3) == 0\n"
        "    print('child-dialog-ok')\n"
        "finally:\n"
        "    if child.poll() is None:\n"
        "        child.kill(); child.wait()\n",
        encoding="utf-8",
    )
    if sys.platform == "win32":
        executable = sys.executable.replace("'", "''")
        command = f"'pipeline-input' | & '{executable}' 'input test.py'"
    else:
        import shlex

        command = f"printf '%s' 'pipeline-input' | {shlex.quote(sys.executable)} 'input test.py'"
    result = await asyncio.wait_for(
        bash_handler(make_context(tmp_path), {"command": command, "timeout": 8}, manager), 10
    )
    assert result["ok"] is True
    assert result["data"]["exit_code"] == 0
    assert result["data"]["output"].strip() == "child-dialog-ok"


# --- Command environment ---------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("grant_source", ["agent", "skill"])
async def test_granted_env_key_is_resolved_into_only_the_spawned_process(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    grant_source: str,
) -> None:
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    context = make_context(
        tmp_path,
        tool_settings=(
            {"bash": {"allowed_env": ["TEST_API_TOKEN"]}} if grant_source == "agent" else None
        ),
        skill_env_keys=("TEST_API_TOKEN",) if grant_source == "skill" else (),
    )
    resolved: list[str] = []

    def resolve_credential(key: str) -> str:
        resolved.append(key)
        return "hidden-token"

    result = await bash_handler(
        context,
        {
            "command": "import os; print(os.environ['TEST_API_TOKEN'])",
            "mode": "foreground",
            "env_keys": ["TEST_API_TOKEN"],
        },
        manager,
        credential_resolver=resolve_credential,
    )

    assert result["ok"] is True
    assert result["data"]["output"].strip() == "hidden-token"
    assert resolved == ["TEST_API_TOKEN"]
    assert bash_environment._cached_shell_env == {"PATH": "original-path"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("project_id", "expected_project"),
    [
        ("vbot", "vbot"),
        # A Run outside a project removes the host's project context.
        (None, "missing"),
    ],
)
async def test_the_command_sees_the_run_it_belongs_to(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    project_id: str | None,
    expected_project: str,
) -> None:
    monkeypatch.setattr(
        bash_environment,
        "_cached_shell_env",
        {"PATH": "original-path", "VBOT_RUN_PROJECT_ID": "host-value"},
    )
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)

    result = await bash_handler(
        make_context(tmp_path, project_id=project_id),
        {
            "command": (
                "import os; print(os.environ['VBOT_RUN_AGENT_ID']); "
                "print(os.environ['VBOT_RUN_SESSION_ID']); "
                "print(os.environ.get('VBOT_RUN_PROJECT_ID', 'missing'))"
            ),
        },
        manager,
    )

    assert result["data"]["output"].replace("\r\n", "\n") == (
        f"agent-a\nsession-a\n{expected_project}\n"
    )


# --- Spawning --------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("shell", "recovers", "message"),
    [
        ("missing-vbot-shell", False, "The shell 'missing-vbot-shell' was not found on this host"),
        ("pwsh", False, "requires PowerShell 7 (pwsh) on Windows; install it or add it to PATH"),
        (sys.executable, True, None),
    ],
    ids=["missing-shell", "missing-pwsh", "recovers"],
)
async def test_a_missing_shell_refreshes_the_environment_once_before_failing(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    shell: str,
    recovers: bool,
    message: str | None,
) -> None:
    probes: list[bool] = []
    spawn_paths: list[str] = []
    original_spawn = manager.spawn

    async def probe() -> dict[str, str]:
        probes.append(True)
        return {"PATH": "refreshed-path"}

    async def spawn(*args: Any, env: dict[str, str], **kwargs: Any) -> str:
        spawn_paths.append(env["PATH"])
        if len(spawn_paths) == 1 or not recovers:
            raise FileNotFoundError(f"no such file: {shell}")
        return await original_spawn(*args, env=env, **kwargs)

    monkeypatch.setattr(bash_environment, "_probe_shell_env", probe)
    monkeypatch.setattr(bash_module, "_shell_argv", lambda command: [shell, "-c", command])
    monkeypatch.setattr(manager, "spawn", spawn)

    result = await bash_handler(make_context(tmp_path), {"command": "print('recovered')"}, manager)

    assert spawn_paths == ["original-path", "refreshed-path"]
    assert probes == [True]
    if recovers:
        assert result["ok"] is True
        assert result["data"]["output"].strip() == "recovered"
    else:
        assert result["error"]["code"] == "process_spawn_failed"
        assert result["error"]["message"].startswith(
            f"failed to start process: no such file: {shell}"
        )
        assert message in result["error"]["message"]


@pytest.mark.asyncio
async def test_commands_spawn_in_a_windowless_process_group(
    manager: ProcessManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[bool, int]] = []

    def creation_flags(*, new_process_group: bool = False, platform_name: str = os.name) -> int:
        flags = subprocess_creation_flags(
            new_process_group=new_process_group, platform_name=platform_name
        )
        calls.append((new_process_group, flags))
        return flags

    monkeypatch.setattr("core.tools.process_manager.subprocess_creation_flags", creation_flags)
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)

    result = await bash_handler(make_context(tmp_path), {"command": "print('done')"}, manager)

    assert result["ok"] is True
    assert calls == [(True, subprocess_creation_flags(new_process_group=True))]
