"""Live authority of the vBot CLI processes started by a restricted shell call."""

from __future__ import annotations

import asyncio
import json
import secrets
import shlex
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.agents.temporary import TemporaryAgent
from core.permissions import ApprovalRequirement
from core.skills.skills import SkillRegistry, allows_vbot_cli
from core.tools._permission_preflight import authorize_accesses
from core.tools._tool_workers import run_tool_worker
from core.tools.tools import JsonObject, ToolContext

if TYPE_CHECKING:
    from core.projects._runtime_agent import RuntimeAgent


class ShellCliAccessError(ValueError):
    """The CLI cannot proceed under the current shell call's authority."""


class ShellCliGrant:
    """A shell process tree's CLI access ends when its last process exits."""

    def __init__(
        self,
        environment: JsonObject,
        context: ToolContext,
        arguments: JsonObject,
        release: Callable[[], None],
    ) -> None:
        self.environment_value = json.dumps(environment, separators=(",", ":"))
        self.context = context
        self.arguments = arguments
        self._release = release
        self._released = False
        self._pending: set[asyncio.Task[dict[str, str]]] = set()
        self._idle = asyncio.Event()
        self._idle.set()
        self.lock = asyncio.Lock()

    @property
    def reviewing(self) -> bool:
        return bool(self._pending)

    async def wait_for_reviews(self) -> None:
        await self._idle.wait()

    async def review(
        self, check: Callable[[], Coroutine[Any, Any, dict[str, str]]]
    ) -> dict[str, str]:
        if self._released:
            raise ShellCliAccessError("This shell command's CLI access has ended.")
        task = asyncio.create_task(check())
        self._pending.add(task)
        self._idle.clear()
        try:
            return await task
        except asyncio.CancelledError:
            if self._released:
                raise ShellCliAccessError("This shell command's CLI access has ended.") from None
            raise
        finally:
            self._pending.discard(task)
            if not self._pending:
                self._idle.set()

    def release(self) -> None:
        self._released = True
        self._release()
        for task in self._pending:
            task.cancel()


class ShellCliAccess:
    """Bind CLI reviews to live shell calls on the serving instance."""

    def __init__(
        self,
        *,
        resolve_agent: Callable[[ToolContext], Awaitable[RuntimeAgent]],
        resolve_skills: Callable[[str | None, str | None], SkillRegistry],
        data_dir: Path,
        server_bind: Callable[[], tuple[str, int | None]],
    ) -> None:
        self._resolve_agent = resolve_agent
        self._resolve_skills = resolve_skills
        self._data_dir = data_dir
        self._server_bind = server_bind
        self._grants: dict[str, ShellCliGrant] = {}

    async def allows(self, context: ToolContext) -> bool:
        agent = await self._resolve_agent(context)

        def granted() -> bool:
            identity = (
                context.skill_subject_id
                if context.project_id is None and not isinstance(agent, TemporaryAgent)
                else None
            )
            registry = self._resolve_skills(context.skill_project_id, identity)
            if context.skill_ceiling is not None:
                registry = registry.restricted_to(context.skill_ceiling)
            return allows_vbot_cli(registry, agent.allowed_skills)

        return await run_tool_worker(granted)

    def issue(
        self, context: ToolContext, arguments: JsonObject, *, handoff_token: str | None
    ) -> ShellCliGrant:
        host, port = self._server_bind()
        if port is None:
            raise ShellCliAccessError("The local vBot server has no active port.")
        token = secrets.token_urlsafe(32)

        def release() -> None:
            self._grants.pop(token, None)

        grant = ShellCliGrant(
            {
                "token": token,
                "data_dir": str(self._data_dir),
                "host": host,
                "port": port,
                "agent_id": context.agent_id,
                "session_id": context.session_id,
                "project_id": context.project_id,
                "update_handoff": handoff_token,
            },
            context,
            context.public_arguments or arguments,
            release,
        )
        self._grants[token] = grant
        return grant

    async def review(
        self,
        token: str,
        *,
        argv: list[str],
        reads: Sequence[tuple[str, bool]],
        rights: bool,
        profile: JsonObject | None,
        stdin: str | None,
    ) -> dict[str, str]:
        grant = self._grant(token)
        return await grant.review(
            lambda: self._review(grant, None, argv, reads, (), rights, profile, stdin)
        )

    def download_url_review(self, token: str) -> Callable[[str | None], Coroutine[Any, Any, None]]:
        """Bind one remote Skill install to its live shell and exact URL approvals."""
        grant = self._grant(token)
        context = replace(grant.context)

        async def review_url(url: str | None) -> None:
            if url is None:
                # A final check under the publication lock never asks or waits for
                # another pipeline stage's approval.
                await grant.review(lambda: self._revalidate(grant, context))
                return
            await grant.review(
                lambda: self._review(grant, context, [], (), [url], False, None, None)
            )

        return review_url

    def _grant(self, token: str) -> ShellCliGrant:
        grant = self._grants.get(token)
        if grant is None:
            raise ShellCliAccessError("This shell command's CLI access has ended.")
        return grant

    async def _revalidate(self, grant: ShellCliGrant, context: ToolContext) -> dict[str, str]:
        await grant.context.revalidate_permissions(force_refresh=True)
        if context is not grant.context:
            await context.revalidate_permissions(force_refresh=True)
        if not await self.allows(context):
            raise ShellCliAccessError("This Agent needs access to the bundled vbot-docs Skill.")
        return {}

    async def _review(
        self,
        grant: ShellCliGrant,
        context: ToolContext | None,
        argv: list[str],
        reads: Sequence[tuple[str, bool]],
        urls: Sequence[str],
        rights: bool,
        profile: JsonObject | None,
        stdin: str | None,
    ) -> dict[str, str]:
        # Two pipeline stages can enter the CLI at the same time. Each decision
        # belongs to one parsed invocation and gets its own exact path grants.
        async with grant.lock:
            await self._revalidate(grant, context or grant.context)
            if context is None:
                context = replace(grant.context)
            invocation: JsonObject = {"argv": argv}
            if stdin is not None:
                invocation["stdin"] = stdin
            if reads or urls:
                await authorize_accesses(
                    context,
                    [(path, "read", {"subtree": subtree}) for path, subtree in reads],
                    urls,
                    arguments=grant.arguments,
                    by_program=True,
                )
            if rights or profile is not None:
                shown = shlex.join(["vbot", *argv])
                if stdin is not None and profile is None:
                    shown += "\n" + stdin
                requirement = ApprovalRequirement.review(
                    json.dumps(invocation, sort_keys=True), shown=shown, permission_profile=profile
                )
                if not await context.request_approval(
                    "This command changes Agent access.", [requirement], arguments=grant.arguments
                ):
                    raise ShellCliAccessError("The access change was not approved.")
            if reads or urls or rights or profile is not None:
                await self._revalidate(grant, context)
            if not reads:
                return {}
            return await run_tool_worker(
                lambda: {path: str(context.resolve_path(path, access="read")) for path, _ in reads}
            )
