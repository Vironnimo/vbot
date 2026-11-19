"""Operate the serving vBot instance through its CLI."""

from __future__ import annotations

import json
import shlex
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from core.agents.app_chats import app_chat_profile
from core.agents.temporary import TemporaryAgent
from core.permissions import ApprovalRequirement
from core.skills.skills import SkillRegistry, grants_vbot_tool
from core.tools._permission_preflight import authorize_paths
from core.tools._tool_workers import run_tool_worker
from core.tools.availability import TOOL_ACTIVATION_SESSION_GRANT, resolve_tool_access
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
from core.tools.vbot_cli_runner import VBotAccessReviewRequiredError, run_vbot_cli

if TYPE_CHECKING:
    from core.projects._runtime_agent import RuntimeAgent


def _reviewed_files(context: ToolContext, reads: Sequence[tuple[str, bool]]) -> dict[str, str]:
    return {
        path: str(context.resolve_path(path, access="read"))
        for path, subtree in reads
        if not subtree
    }


def register_vbot_tool(
    registry: ToolRegistry,
    *,
    resolve_agent: Callable[[ToolContext], Awaitable[RuntimeAgent]],
    resolve_skills: Callable[[str | None, str | None], SkillRegistry],
    data_dir: Path,
    server_bind: Callable[[], tuple[str, int | None]],
    update_handoffs: UpdateHandoffs,
) -> None:
    def skill_granted(context: ToolContext, agent: RuntimeAgent) -> bool:
        identity = (
            context.skill_subject_id
            if context.project_id is None and not isinstance(agent, TemporaryAgent)
            else None
        )
        skills = resolve_skills(context.skill_project_id, identity)
        if context.skill_ceiling is not None:
            skills = skills.restricted_to(context.skill_ceiling)
        return grants_vbot_tool(skills, agent.allowed_skills)

    async def check_authority(context: ToolContext) -> None:
        await context.revalidate_permissions(force_refresh=True)
        agent = await resolve_agent(context)
        access = resolve_tool_access(
            agent.tool_access,
            registry.list_tools(["vbot"]),
            agent.memory_prompt_mode,
            session_tool_grants=context.session_tool_grants,
        )
        if "vbot" not in access.session_tool_grants:
            raise ToolNotAllowedError("The vbot Tool is unavailable to this Agent.")
        if app_chat_profile(agent) is not None and not context.permissions.restricted:
            return
        if not await run_tool_worker(skill_granted, context, agent):
            raise ToolNotAllowedError("This Agent needs access to the bundled vbot-docs Skill.")

    async def vbot(context: ToolContext, arguments: JsonObject) -> JsonObject:
        await check_authority(context)
        arguments = {**arguments, "argv": list(arguments["argv"])}
        argv = arguments["argv"]
        if argv[:1] == ["vbot"]:
            argv = argv[1:]
        host, port = server_bind()
        if port is None:
            return tool_failure("vbot_unavailable", "The local vBot server has no active port.")
        grant = issue_for_tool_context(update_handoffs, context)

        async def execute(
            *, check_agent_access: bool, reviewed_inputs: JsonObject | None = None
        ) -> JsonObject:
            return await run_vbot_cli(
                argv,
                stdin=arguments.get("stdin"),
                data_dir=data_dir,
                port=port,
                host=host,
                check_agent_access=check_agent_access,
                reviewed_inputs=reviewed_inputs,
                run=RunIdentity(
                    context.agent_id,
                    context.session_id,
                    context.project_id,
                    grant.token if grant is not None else None,
                ),
            )

        try:
            try:
                result = await execute(check_agent_access=context.permissions.restricted)
            except VBotAccessReviewRequiredError as review:
                public = context.public_arguments or arguments
                if review.reads:
                    await authorize_paths(
                        context,
                        [(path, "read", {"subtree": tree}) for path, tree in review.reads],
                        arguments=public,
                    )
                if review.rights:
                    target = json.dumps(public, ensure_ascii=False, sort_keys=True)
                    shown = public["argv"]
                    command = shown if shown[:1] == ["vbot"] else ["vbot", *shown]
                    requirement = ApprovalRequirement.review(
                        target, shown=shlex.join(command), permission_profile=review.profile
                    )
                    if not await context.request_approval(
                        "This command changes Agent access.",
                        [requirement],
                        arguments=public,
                    ):
                        return tool_failure(
                            "permission_denied", "The access change was not approved."
                        )
                reviewed_files = (
                    await run_tool_worker(_reviewed_files, context, review.reads)
                    if review.reads
                    else {}
                )
                await check_authority(context)
                result = await execute(
                    check_agent_access=False,
                    reviewed_inputs={"files": reviewed_files, "profile": review.profile},
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
                    "items": {"type": "string", "pattern": r"^[^\x00]*$"},
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
