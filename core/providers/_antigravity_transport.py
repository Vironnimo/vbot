"""One isolated, subscription-only Antigravity CLI turn over its public NDJSON interface."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shlex
import site
import subprocess
from collections.abc import AsyncGenerator, AsyncIterator
from pathlib import Path
from typing import Any

from core.providers._antigravity_native import SUBSCRIPTION_SETTINGS, subscription_environment
from core.providers._native_agent_wire import native_tool_names
from core.providers.adapter import TOOL_CALL_REJECTION_FIELD, build_tool_call_rejection
from core.providers.errors import (
    ProviderAuthError,
    ProviderError,
    ProviderTimeoutError,
    ProviderUsageLimitError,
)
from core.storage import TemporaryFileManager
from core.utils.file_status import is_file_strict
from core.utils.processes import (
    create_subprocess_exec,
    guarded_process_launch,
    kill_process_tree_async,
    subprocess_creation_flags,
    wait_for_exit,
)
from core.utils.python_process import python_executable
from core.utils.workers import BoundedWorkerPool, settle_before_cancelling

_FILES = BoundedWorkerPool(name="antigravity-workspaces", max_workers=4)
_BRIDGE = Path(__file__).with_name("_antigravity_bridge.py").resolve()
_AGENT = "vbot"
_START_SECONDS = 45
_TOOL_PREFIX = "mcp_vbot_"


def _invalid(message: str) -> ProviderError:
    return ProviderError(f"Antigravity CLI {message}.", retryable=False)


def _response_error(reason: object) -> ProviderError:
    # Google can include prompt text and paths in diagnostics. Classify, never relay raw output.
    detail = str(reason).casefold()
    if "quota" in detail or "resource_exhausted" in detail:
        return ProviderUsageLimitError("The Antigravity subscription limit was reached.")
    if "unauthenticated" in detail or "log in" in detail or "not logged" in detail:
        return ProviderAuthError("Antigravity CLI requires its Google account login.")
    return _invalid("could not complete this Model request")


def _json_file(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8", newline="\n")


def _configuration(
    root: Path,
    system_prompt: str,
    tools: list[dict[str, Any]],
    python: Path,
    replay_steps: tuple[str, ...] = (),
) -> tuple[Path, Path]:
    home, work = root / "home", root / "workspace"
    config = work / ".agents"
    agent_dir = config / "agents" / _AGENT
    agent_dir.mkdir(parents=True)
    # agy walks up to the Git root for hooks and AGENTS.md. Some installations
    # keep their data directory inside a checkout. Give this disposable workspace
    # its own empty repository boundary, independently of the Git executable.
    git = work / ".git"
    (git / "refs").mkdir(parents=True)
    (git / "objects").mkdir()
    (git / "HEAD").write_bytes(b"ref: refs/heads/main\n")
    google_dir = home / ".gemini" / "config"
    google_dir.mkdir(parents=True)

    # agy's private profile contains only our inventory. Eager registration exposes
    # each real schema natively and avoids teaching the model a lookup protocol.
    if tools:
        mcp_argv = ["-I", "-B", str(_BRIDGE), "mcp", str(root)]
        if site.ENABLE_USER_SITE:
            mcp_argv.append(site.getusersitepackages())
        server = {
            "command": str(python),
            "args": mcp_argv,
            "tools": {tool["name"]: {"eager": True} for tool in tools},
        }
        _json_file(root / "tools.json", tools)
        _json_file(google_dir / "mcp_config.json", {"mcpServers": {"vbot": server}})

    agent_file = (
        "---\nname: vbot\ndescription: vBot\nmainAgent: true\nsubagent: false\n"
        "excludeDefaultComponents: true\ninheritMcp: true\n---\n# System Prompt\n" + system_prompt
    )
    (agent_dir / "agent.md").write_text(agent_file, encoding="utf-8", newline="\n")

    # The native hook command is shell-interpreted. Only fixed names enter its shell
    # source. The launch environment puts the vBot interpreter's directory first;
    # the hook wrapper itself lives at a fixed relative path in the private workspace.
    if os.name == "nt":
        interpreter = python.name
        if not interpreter.replace(".", "").isalnum():
            raise _invalid("requires a Python executable with a shell-safe basename")
    else:
        interpreter = shlex.quote(python.name)
    # agy starts each hook in the directory containing its hooks.json.
    bridge_copy = config / "vbot_hook.py"
    bridge_copy.write_bytes(_BRIDGE.read_bytes())
    prefix = f"{interpreter} -I -S -B vbot_hook.py"
    before = "before-tools" if tools else "before"
    if replay_steps:
        _json_file(root / "replay.json", replay_steps)
    replay_option = " replay" if replay_steps else ""
    hooks = {
        "PreInvocation": [
            {"type": "command", "command": f"{prefix} {before} ../..{replay_option}"}
        ],
        "PreToolUse": [
            {"matcher": "*", "hooks": [{"type": "command", "command": f"{prefix} guard ../.."}]}
        ],
    }
    _json_file(config / "hooks.json", {"vbot": hooks})
    settings = {
        **SUBSCRIPTION_SETTINGS,
        "toolPermission": "strict",
        "allowNonWorkspaceAccess": False,
        "permissions": {
            "deny": [
                "read_file(*)",
                "write_file(*)",
                "read_url(*)",
                "execute_url(*)",
                "download_url(*)",
                "command(*)",
                "mcp(*)",
            ],
        },
        "trustedWorkspaces": [str(work)],
    }
    _json_file(root / "settings.json", settings)
    return home, work


def _confirmed_call(root: Path, step: dict[str, Any], names: dict[str, str]) -> dict[str, Any]:
    index = step.get("step_index")
    if type(index) is not int or index < 0:
        raise _invalid("returned a Tool step without an index")
    try:
        receipt = json.loads((root / f"call-{index}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise _invalid("did not confirm the pre-execution Tool denial") from exc
    name = step.get("tool_name")
    info = step.get("tool_info")
    if (
        not isinstance(info, dict)
        or not isinstance(name, str)
        or not name
        or not isinstance(receipt, dict)
        or receipt.get("name") != name
        or not isinstance(receipt.get("id"), str)
        or not receipt["id"]
        or step.get("state") != "ERROR"
        or not isinstance(info.get("error"), dict)
        or "denied by pre-tool hook" not in str(info["error"].get("message", "")).lower()
    ):
        raise _invalid("returned an unconfirmed Tool call")
    # The hook also receives the harness-only toolSummary field. The public tool_info
    # carries precisely the model's arguments, including fields unknown to vBot.
    parameters = info.get("parameters", {})
    call: dict[str, Any] = {
        "type": "tool_call_delta",
        "id": receipt["id"],
        "name_delta": names.get(name, name),
        "arguments_delta": json.dumps(parameters, ensure_ascii=False),
    }
    if name not in names:
        next_action = (
            "Use a tool from the vbot MCP server or answer directly."
            if names
            else "Answer directly."
        )
        call[TOOL_CALL_REJECTION_FIELD] = build_tool_call_rejection(
            code="tool_unavailable",
            message=f"This tool was blocked before execution. {next_action}",
            name=name,
            arguments=parameters,
        )
    return call


def _token_counts(value: object) -> dict[str, int]:
    usage: dict[str, int] = {}
    if not isinstance(value, dict):
        return usage
    for native, canonical in (
        ("input_tokens", "input_tokens"),
        ("output_tokens", "output_tokens"),
        ("thinking_tokens", "reasoning_tokens"),
        ("cache_read_tokens", "cache_read_tokens"),
    ):
        count = value.get(native)
        if type(count) is int and count >= 0:
            usage[canonical] = count
    if "input_tokens" in usage:
        usage["input_tokens"] += usage.get("cache_read_tokens", 0)
        # Native requests always carry a prompt. A zero after including cache
        # is unavailable as a measurement, including inside last_request_usage.
        if usage["input_tokens"] == 0:
            del usage["input_tokens"]
    return usage


async def _event(reader: asyncio.StreamReader) -> tuple[str, dict[str, Any]]:
    try:
        line = await reader.readline()
        if not line:
            raise _invalid("ended before completing its response")
        message = json.loads(line)
        if not isinstance(message, dict):
            raise ValueError
        tag = message["event"]
        body = message[tag]
        if not isinstance(tag, str) or not isinstance(body, dict):
            raise ValueError
    except (ValueError, TypeError, KeyError) as exc:
        raise _invalid("returned an invalid stream event") from exc
    return tag, body


async def _read_turn(
    process: asyncio.subprocess.Process,
    root: Path,
    model: str,
    prompt: str,
    names: dict[str, str],
    replay_count: int = 0,
) -> AsyncIterator[dict[str, Any]]:
    assert process.stdin is not None and process.stdout is not None
    try:
        async with asyncio.timeout(_START_SECONDS):
            tag, init = await _event(process.stdout)
    except TimeoutError as exc:
        raise ProviderTimeoutError("Antigravity CLI did not initialize in time.") from exc
    if (
        tag != "init"
        or init.get("agent") != _AGENT
        or init.get("cwd") != str(root / "workspace")
        or init.get("permission_mode") != "strict"
        or init.get("model") != model
    ):
        raise _invalid("did not initialize the requested Model with the isolated vBot agent")
    packet = {"event": "user", "message": {"content": prompt}}
    process.stdin.write(json.dumps(packet, ensure_ascii=False).encode("utf-8") + b"\n")
    await process.stdin.drain()
    checked_guard = False
    content: list[str] = []
    calls: dict[int, dict[str, Any]] = {}
    pending: set[int] = set()
    agent_usage: dict[str, int] | None = None
    had_checkpoint = False
    input_steps: set[int] = set()
    while True:
        tag, body = await _event(process.stdout)
        if tag == "result" and body.get("status") != "SUCCESS":
            raise _response_error(body.get("error"))
        step_type = body.get("step_type") if tag == "step_update" else None
        if replay_count and step_type == "user_input" and body.get("state") == "DONE":
            index = body.get("step_index")
            if type(index) is int and index >= 0:
                input_steps.add(index)
        if step_type == "checkpoint":
            had_checkpoint = True
        if step_type in {"user_input", "checkpoint", "error_message"}:
            continue
        if not checked_guard:
            checked_guard = await _FILES.run(is_file_strict, root / "invocation")
            if not checked_guard:
                if names and not await _FILES.run(is_file_strict, root / "listed"):
                    raise _invalid(
                        "did not load vBot's Tool inventory from its local Python process"
                    )
                raise _invalid("did not run vBot's pre-invocation hook")
        # agy acknowledges the initial stdin message and each userMessage
        # injected by the hook as separate completed user_input steps.
        if (
            replay_count
            and (tag == "result" or step_type in {"agent_response", "tool"})
            and len(input_steps) != 1 + replay_count
        ):
            raise _invalid("did not acknowledge the complete vBot conversation")
        if tag == "result":
            if pending:
                raise _invalid("finished without a pre-execution denial for every Tool")
            response = body.get("response")
            if isinstance(response, str):
                accumulated = "".join(content)
                if response.startswith(accumulated):
                    remaining = response[len(accumulated) :]
                    if remaining:
                        yield {"type": "content_delta", "text": remaining}
            if len({call["id"] for call in calls.values()}) != len(calls):
                raise _invalid("returned duplicate Tool call IDs")
            # The CLI's result accounts for the entire native turn, including a
            # separate checkpoint request. Only the agent response anchors Context.
            total = _token_counts(body.get("usage"))
            if total != agent_usage or had_checkpoint:
                usage: dict[str, Any] = {"type": "usage", "reported_cost_usd": 0.0, **total}
                if had_checkpoint or (
                    agent_usage
                    and any(total.get(key, value) != value for key, value in agent_usage.items())
                ):
                    usage["last_request_usage"] = agent_usage or {}
                yield usage
            for call_index in sorted(calls):
                yield calls[call_index]
            yield {"type": "finish", "reason": "tool_calls" if calls else "stop"}
            return
        if tag != "step_update":
            raise _invalid("reported an unexpected stream event")
        if step_type == "agent_response":
            text = body.get("text_delta")
            if isinstance(text, str) and text:
                content.append(text)
                yield {"type": "content_delta", "text": text}
            if body.get("state") == "DONE" and (counts := _token_counts(body.get("usage"))):
                agent_usage = counts
                yield {"type": "usage", "reported_cost_usd": 0.0, **counts}
        elif step_type == "tool":
            index = body.get("step_index")
            if type(index) is not int or index < 0:
                raise _invalid("returned a Tool step without an index")
            if body.get("state") == "ACTIVE":
                pending.add(index)
            else:
                calls[index] = await _FILES.run(_confirmed_call, root, body, names)
                pending.discard(index)
        elif step_type not in {"error_message", "system_message"}:
            raise _invalid("reported an unexpected agent step")


async def stream_turn(
    executable: str | Path,
    *,
    model: str,
    system_prompt: str,
    prompt: str,
    tools: list[dict[str, Any]],
    temporary_files: TemporaryFileManager,
    replay_steps: tuple[str, ...] = (),
) -> AsyncGenerator[dict[str, Any]]:
    """Run the official CLI as an isolated one-turn child, always reaping its tree."""
    stack = contextlib.ExitStack()
    process: asyncio.subprocess.Process | None = None
    python = Path(python_executable())
    short_names = native_tool_names(tools, 64 - len(_TOOL_PREFIX))
    names = {_TOOL_PREFIX + short: original for original, short in short_names.items()}

    def prepare() -> tuple[Path, Path, Path]:
        root = stack.enter_context(temporary_files.provider_directory())
        manifest = [{**tool, "name": short_names[tool["name"]]} for tool in tools]
        home, work = _configuration(root, system_prompt, manifest, python, replay_steps)
        return root, home, work

    async def start(root: Path, home: Path, work: Path) -> None:
        nonlocal process
        env = subscription_environment(home)
        env["GIT_CEILING_DIRECTORIES"] = str(root)
        env["PATH"] = str(python.parent) + os.pathsep + env.get("PATH", "")
        command = [
            str(executable),
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--model",
            model,
            "--agent",
            _AGENT,
            "--settings",
            str(root / "settings.json"),
            "--disable-slash-commands",
            "--log-file",
            str(root / "native.log"),
        ]
        launch = guarded_process_launch(command, env=env)
        process = await create_subprocess_exec(
            *launch.argv,
            cwd=work,
            env=env,
            pass_fds=launch.pass_fds,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=os.name != "nt",
            limit=16 * 1024 * 1024,
            creationflags=subprocess_creation_flags(),
        )

    async def stop() -> None:
        try:
            if process is not None and process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    await kill_process_tree_async(process)
                await wait_for_exit(process)
        finally:
            await _FILES.run(stack.close)

    try:
        root, home, work = await settle_before_cancelling(_FILES.run(prepare))
        await settle_before_cancelling(start(root, home, work))
        assert process is not None
        async for delta in _read_turn(process, root, model, prompt, names, len(replay_steps)):
            yield delta
    except (OSError, UnicodeError) as exc:
        raise _invalid("could not open or maintain its isolated local session") from exc
    finally:
        await settle_before_cancelling(stop())
