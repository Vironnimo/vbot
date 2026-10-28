"""Shared fixtures and fakes for cron behavior tests."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from core.automation.cron import (
    CronService,
    CronWatch,
)
from core.calendar import CalendarService
from core.channels import ChannelNotFoundError
from core.permissions import PermissionCeiling, PermissionRefusal
from core.runs import RunStatus
from core.secrets import KeyFileProtection, SecretStore
from core.secrets._references import EMPTY_MASKER, SecretMasker
from core.sessions import AGENT_DEFAULT_PROJECT, WorkingProjectChoice


def secret_store(tmp_path: Path) -> SecretStore:
    """A Secret Store in ``tmp_path`` protected by a key file beside it; each call opens it anew."""
    data_dir = tmp_path / "secrets"
    data_dir.mkdir(parents=True, exist_ok=True)
    return SecretStore(
        data_dir,
        protections=(KeyFileProtection(directories=(tmp_path / "keys",)),),
    )


def make_service(
    tmp_path: Path,
    *,
    agent_resolver: Any = None,
    sessions: Any = None,
    tz: str | ZoneInfo | None = None,
    calendar: CalendarService | None = None,
    secret_masker: Callable[[], SecretMasker] = lambda: EMPTY_MASKER,
) -> tuple[CronService, SimpleNamespace]:
    """A CronService with a mock trigger; with ``calendar``, bound to it both ways."""
    trigger_service = SimpleNamespace(trigger_run=AsyncMock())
    service = CronService(
        cast(Any, trigger_service),
        tmp_path,
        agent_resolver=agent_resolver,
        sessions=sessions,
        tz=tz,
        calendar=calendar,
        secret_masker=secret_masker,
    )
    if calendar is not None:
        calendar.bind_event_jobs(service)
    return service, trigger_service


def scripted_runs(trigger_service: SimpleNamespace, *outcomes: tuple[RunStatus, str]) -> list[bool]:
    """Admit one Run per outcome; each reports the activity decision Runs make at finish."""
    decisions: list[bool] = []
    pending = list(outcomes)

    async def trigger_run(*_args: Any, completion_activity: Any, **_kwargs: Any) -> Any:
        status, answer = pending.pop(0)
        number = len(outcomes) - len(pending)
        result = SimpleNamespace(content=answer)

        async def wait() -> Any:
            decisions.append(completion_activity(status, result))
            if status is not RunStatus.COMPLETED:
                raise RuntimeError("model unavailable")
            return result

        return SimpleNamespace(id=f"run-{number}", session_id=f"ses-{number}", wait=wait)

    trigger_service.trigger_run.side_effect = trigger_run
    return decisions


@dataclass
class FakeChats:
    """Channels as Cron sees them: owners by Channel id, and what reached chats.

    A Channel whose id starts with ``mail-`` is an email Channel.
    """

    owners: dict[str, str] = field(default_factory=lambda: {"tg-main": "agent-one"})
    relayed: list[tuple[Any, str, str, str | None]] = field(default_factory=list)
    sent: list[tuple[str, str, str, str | None]] = field(default_factory=list)
    notes: list[tuple[str, str, str, str | None, str | None]] = field(default_factory=list)

    async def get_channel(self, channel_id: str) -> SimpleNamespace:
        if channel_id not in self.owners:
            raise ChannelNotFoundError(f"Channel not found: {channel_id}")
        platform = "email" if channel_id.startswith("mail-") else "telegram"
        return SimpleNamespace(agent_id=self.owners[channel_id], platform=platform)

    async def relay_run_to_chat(
        self, run: Any, channel_id: str, platform_target: str, *, thread_id: str | None = None
    ) -> None:
        self.relayed.append((run, channel_id, platform_target, thread_id))

    async def send(
        self, channel_id: str, message: str, platform_target: str, *, thread_id: str | None = None
    ) -> None:
        self.sent.append((channel_id, message, platform_target, thread_id))

    async def record_outbound_note(
        self,
        channel_id: str,
        platform_target: str,
        note: str,
        *,
        thread_id: str | None = None,
        skip_session_id: str | None = None,
    ) -> bool:
        self.notes.append((channel_id, platform_target, note, thread_id, skip_session_id))
        return True


@dataclass
class FakeChecks:
    """Watch checks that answer from a script: an output, or an error to raise."""

    answers: list[str | Exception] = field(default_factory=list)
    calls: list[tuple[str, ...]] = field(default_factory=list)
    # The permission ceiling of each check, and the refusal ``permission_refusal`` gives.
    ceilings: list[PermissionCeiling | None] = field(default_factory=list)
    refusal: PermissionRefusal | None = None
    targets: list[tuple[str | None, WorkingProjectChoice]] = field(default_factory=list)

    async def run_command(
        self,
        command: str,
        *,
        agent_id: str,
        project_id: str | None,
        session_id: str | None = None,
        working_project_id: WorkingProjectChoice = AGENT_DEFAULT_PROJECT,
        ceiling: PermissionCeiling | None = None,
    ) -> str:
        self.calls.append(("command", command, agent_id, project_id or ""))
        self.ceilings.append(ceiling)
        self.targets.append((session_id, working_project_id))
        return self._answer()

    async def fetch_url(self, url: str, *, ceiling: PermissionCeiling | None = None) -> str:
        self.calls.append(("url", url))
        self.ceilings.append(ceiling)
        return self._answer()

    async def permission_refusal(
        self,
        watch: CronWatch,
        *,
        agent_id: str,
        project_id: str | None,
        session_id: str | None = None,
        working_project_id: WorkingProjectChoice = AGENT_DEFAULT_PROJECT,
        ceiling: PermissionCeiling | None,
    ) -> PermissionRefusal | None:
        return self.refusal if ceiling is not None else None

    def _answer(self) -> str:
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer
