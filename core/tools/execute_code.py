"""Run an Agent's Python program with its existing process and Tool authority."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import tempfile
from collections.abc import Callable, Container
from functools import cache
from pathlib import Path
from typing import Any

from core.permissions import CommandChecker, PermissionDenied
from core.sessions import SessionAddress
from core.tools._argument_repair import load_json_value
from core.tools._code_process import CodeExecutionResult, run_python
from core.tools._tool_display import display_notice, display_text
from core.tools.arguments import required_string
from core.tools.availability import TOOL_ACTIVATION_FOLLOWS
from core.tools.call_syntax import SpellingAliases, normalize_call_arguments
from core.tools.contracts import JsonObject, ToolContract, compile_tool_contract
from core.tools.model_names import SHELL_MODEL_NAME
from core.tools.scratch_areas import ScratchAreas, ScratchFolder, ScratchUnavailableError
from core.tools.shell_environment import RunIdentity, command_environment
from core.tools.tools import (
    ToolContext,
    ToolDisplay,
    ToolRegistry,
    run_tool_worker,
    tool_failure,
    tool_success,
)

EXECUTE_CODE_DESCRIPTION = "Run Python to combine Tool calls and process their results."
EXECUTE_CODE_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "code": {
            "type": "string",
            "description": (
                "Python source. Import tools with `from vbot_tools import tools`, then call "
                "any of your Tools as `tools.<name>(<parameter>=<value>)`. Calls return the "
                "result's data as Python objects; failures raise vbot_tools.ToolError. For "
                "local files and commands, use Python's open() and subprocess. MCP results "
                "retain content blocks and structuredContent. Each execution starts fresh in "
                "the working directory and stops after 300 seconds. Print the results you need."
            ),
        },
    },
    "required": ["code"],
}

_ALIASES = SpellingAliases({"code": ("python", "script", "source", "command")})


@cache
def _contract() -> ToolContract:
    return compile_tool_contract(
        name="execute_code", input_schema=EXECUTE_CODE_PARAMETERS, require_closed_input=False
    )


def _arguments(arguments: Any) -> Any:
    if isinstance(arguments, str):
        try:
            decoded = load_json_value(arguments)
        except json.JSONDecodeError:
            decoded = None
        # Accept freeform Python as well as the usual JSON argument object.
        arguments = decoded if isinstance(decoded, dict) else {"code": arguments}
    return normalize_call_arguments(_contract(), arguments, field_aliases=_ALIASES)


def _details(arguments: JsonObject, result: JsonObject | None) -> list[JsonObject]:
    blocks = [display_text("input", source="arguments", path=["code"])]
    if result is not None and result.get("ok"):
        data = result.get("data", {})
        if note := data.get("note"):
            level = "warning" if data.get("status") in {"completed", "stopped"} else "error"
            blocks.append(display_notice(level, str(note)))
        blocks.append(display_text("output", source="result", path=["data", "output"]))
        if data.get("stderr"):
            blocks.append(display_text("response", source="result", path=["data", "stderr"]))
    elif result is not None and (error := result.get("error")):
        blocks.append(display_notice("error", str(error["message"])))
    return blocks


class _ExecuteCode:
    def __init__(
        self,
        checker: CommandChecker,
        *,
        credential_names: Callable[[], Container[str]],
        scratch_areas: ScratchAreas | None,
    ) -> None:
        self._checker = checker
        self._credential_names = credential_names
        self._scratch_areas = scratch_areas

    async def __call__(self, context: ToolContext, arguments: JsonObject) -> JsonObject:
        code = required_string(arguments.get("code"), field_name="code", strip=False)
        if context.programmatic:
            return tool_failure(
                "code_recursion",
                "Nothing was run: execute_code cannot call itself. "
                "Continue in this Python program.",
            )
        if not context.can_call("bash"):
            return tool_failure(
                "tool_not_allowed",
                "Nothing was run: Python execution needs the same access as "
                f"{SHELL_MODEL_NAME}, which this Run does not allow.",
            )
        if not context.tool_calls_available:
            return tool_failure(
                "code_unavailable",
                "Nothing was run: execute_code needs an active conversation Run.",
            )
        cwd = context.resolve_path(context.effective_cwd, access="start_dir")
        scratch = await self._scratch(context)
        environment = await run_tool_worker(self._environment, context, scratch)
        refusal = await self._checker.check_argv(
            context.permissions,
            [sys.executable, "-c", code],
            cwd=cwd,
            env=environment,
            home=Path.home(),
        )
        if refusal is not None:
            raise PermissionDenied(refusal)
        context.record_worked_paths(cwd)
        output_path = await run_tool_worker(_output_path, scratch)
        result: CodeExecutionResult | None = None
        try:
            result = await self._run(context, code, cwd, environment, output_path)
        finally:
            if output_path is not None and not (
                result is not None and (result.output_truncated or result.stderr_truncated)
            ):
                # Only shortened output names its capture file; drop the others.
                await run_tool_worker(output_path.unlink, missing_ok=True)
        if result is None:
            return tool_failure(
                "cancelled_by_user",
                "Nothing was run: the user stopped this Python execution before it started.",
            )
        data: JsonObject = {
            "status": result.status,
            "output": result.output,
            "tool_calls": result.tool_calls,
        }
        if result.exit_code is not None:
            data["exit_code"] = result.exit_code
        if result.stderr:
            data["stderr"] = result.stderr
        if result.tool_failures:
            data["tool_failures"] = result.tool_failures
        if result.tool_failure_count:
            data["tool_failure_count"] = result.tool_failure_count
        if result.output_truncated or result.stderr_truncated:
            data["output_truncated"] = result.output_truncated
            data["stderr_truncated"] = result.stderr_truncated
            data["note"] = (
                "Output was shortened. The program already ran; do not repeat its effects."
            )
            if result.output_path is not None:
                data["output_file"] = str(result.output_path).replace(os.sep, "/")
                data["note"] += " Read output_file for the captured output."
        if result.log_truncated:
            data["log_truncated"] = True
            data["note"] = str(data.get("note", "")) + (
                " The captured output file reached its size limit and is incomplete."
            )
        if not result.ran:
            data["note"] = (
                "Nothing was run: Python could not compile the program. Fix the error in stderr."
            )
        elif result.status != "completed":
            stopped = {
                "timed_out": "Python reached its 300-second time limit. ",
                "stopped": "The user stopped this Python execution. ",
            }.get(result.status, "The Python program failed. ")
            data["note"] = (
                stopped + "Earlier Tool calls and Python changes "
                "remain applied. Inspect their outcomes before repeating any changes."
                + (" " + str(data["note"]) if "note" in data else "")
            )
        return tool_success(data)

    async def _run(
        self,
        context: ToolContext,
        code: str,
        cwd: Path,
        environment: dict[str, str],
        output_path: Path | None,
    ) -> CodeExecutionResult | None:
        """Run the program; ``None`` when the user stopped it before it started."""
        current = asyncio.current_task()
        stop = asyncio.Event()

        def cancel() -> None:
            # A user stop ends only the program and still reports its output;
            # cancelling the Run cancels this call like any other.
            if context.is_cancelled():
                if current is not None and not current.done():
                    current.cancel()
            else:
                stop.set()

        context.on_cancel(cancel)
        if context.is_cancelled():
            raise asyncio.CancelledError
        if context.was_cancelled_by_user():
            return None
        return await run_python(
            code,
            cwd=cwd,
            environment=environment,
            invoke=context.call_tool,
            output_path=output_path,
            stop=stop,
        )

    def _environment(self, context: ToolContext, scratch: ScratchFolder | None) -> dict[str, str]:
        restricted = context.permissions.restricted
        run = (
            None
            if restricted
            else RunIdentity(context.agent_id, context.session_id, context.project_id)
        )
        return command_environment(
            run,
            scratch=scratch,
            confined=restricted,
            credential_names=self._credential_names() if restricted else (),
        ).complete()

    async def _scratch(self, context: ToolContext) -> ScratchFolder | None:
        if self._scratch_areas is not None:
            with contextlib.suppress(ScratchUnavailableError):
                return await self._scratch_areas.folder_for(
                    SessionAddress(context.project_id, context.agent_id, context.session_id)
                )
        return None


def _output_path(scratch: ScratchFolder | None) -> Path | None:
    if scratch is None:
        return None
    descriptor, name = tempfile.mkstemp(prefix="code-", suffix=".log", dir=scratch.tmp)
    os.close(descriptor)
    return Path(name)


def register_execute_code_tool(
    registry: ToolRegistry,
    *,
    command_checker: CommandChecker,
    credential_names: Callable[[], Container[str]],
    scratch_areas: ScratchAreas | None = None,
) -> None:
    registry.register(
        "execute_code",
        EXECUTE_CODE_DESCRIPTION,
        EXECUTE_CODE_PARAMETERS,
        _ExecuteCode(
            command_checker, credential_names=credential_names, scratch_areas=scratch_areas
        ),
        summary="Combine Tool calls with Python.",
        family="execution",
        activation=TOOL_ACTIVATION_FOLLOWS,
        activation_source="bash",
        parallel_safe=False,
        execution_slot_required=False,
        open_input_schema=True,
        argument_normalizer=_arguments,
        result_schema={"type": "object"},
        display=ToolDisplay(detail_builder=_details),
    )
