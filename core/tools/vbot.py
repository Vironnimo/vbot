"""The built-in App Agent's local vBot CLI Tool."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

from core.agents.app_chats import app_chat_profile
from core.permissions import PermissionDenied
from core.tools.availability import TOOL_ACTIVATION_SESSION_GRANT
from core.tools.shell_environment import RunIdentity
from core.tools.tools import (
    JsonObject,
    ToolContext,
    ToolDisplay,
    ToolNotAllowedError,
    ToolRegistry,
    tool_failure,
    tool_success,
)
from core.tools.update_handoff import UpdateHandoffs, issue_for_tool_context
from core.tools.vbot_cli_runner import run_vbot_cli


def register_vbot_tool(
    registry: ToolRegistry,
    *,
    resolve_agent: Callable[[ToolContext], Awaitable[object]],
    data_dir: Path,
    server_bind: Callable[[], tuple[str, int | None]],
    update_handoffs: UpdateHandoffs,
) -> None:
    async def vbot(context: ToolContext, arguments: JsonObject) -> JsonObject:
        profile = app_chat_profile(await resolve_agent(context))
        if profile is None or "vbot" not in profile.tools:
            raise ToolNotAllowedError("The vbot Tool is available only to the App Agent.")
        await context.revalidate_permissions(force_refresh=True)
        if context.permissions.restricted:
            raise PermissionDenied(
                context.permissions.refusal(
                    "command", "vbot", "protected_location", location="vbot_cli"
                )
            )
        argv = arguments["argv"]
        if argv[:1] == ["vbot"]:
            argv = argv[1:]
        host, port = server_bind()
        if port is None:
            return tool_failure("vbot_unavailable", "The local vBot server has no active port.")
        grant = issue_for_tool_context(update_handoffs, context)
        try:
            result = await run_vbot_cli(
                argv,
                stdin=arguments.get("stdin"),
                data_dir=data_dir,
                port=port,
                host=host,
                run=RunIdentity(
                    context.agent_id,
                    context.session_id,
                    context.project_id,
                    grant.token if grant is not None else None,
                ),
            )
        finally:
            if grant is not None:
                grant.release()
        if result["exit_code"]:
            output = "\n".join(
                text.rstrip() for text in (result["stdout"], result["stderr"]) if text
            )
            return tool_failure(
                "vbot_command_failed",
                f"vbot exited with code {result['exit_code']}.\n{output}".rstrip(),
            )
        return tool_success(result)

    registry.register(
        "vbot",
        (
            "Run a vBot CLI command on this instance. "
            "Use the vbot-docs Skill or pass --help for command syntax."
        ),
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "argv": {
                    "type": "array",
                    "items": {"type": "string", "pattern": r"^[^\u0000]*$"},
                    "description": (
                        "Arguments after vbot, one string per argument, such as "
                        '["config", "get", "server.timezone"].'
                    ),
                },
                "stdin": {
                    "type": "string",
                    "description": "Text for commands that read standard input.",
                },
            },
            "required": ["argv"],
        },
        vbot,
        session_scoped=True,
        activation=TOOL_ACTIVATION_SESSION_GRANT,
        catalog_visible=False,
        parallel_safe=False,
        display=ToolDisplay(hidden_argument_keys=("stdin",)),
        result_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "exit_code": {"type": "integer"},
                "stdout": {"type": "string"},
                "stderr": {"type": "string"},
                "stdout_truncated": {"type": "boolean"},
                "stderr_truncated": {"type": "boolean"},
            },
            "required": ["exit_code", "stdout", "stderr"],
        },
    )
