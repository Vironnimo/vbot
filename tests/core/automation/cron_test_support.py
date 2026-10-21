"""Shared fixtures and fakes for cron behavior tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from core.automation.cron import (
    CronService,
)
from core.calendar import CalendarService
from core.channels import ChannelNotFoundError
from core.runs import RunStatus
from core.secrets import KeyFileProtection, SecretStore


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
    """Channels as Cron sees them: owners by Channel id, and what reached chats."""

    owners: dict[str, str] = field(default_factory=lambda: {"tg-main": "agent-one"})
    relayed: list[tuple[Any, str, str, str | None]] = field(default_factory=list)
    sent: list[tuple[str, str, str, str | None]] = field(default_factory=list)
    notes: list[tuple[str, str, str, str | None, str | None]] = field(default_factory=list)

    async def get_channel(self, channel_id: str) -> SimpleNamespace:
        if channel_id not in self.owners:
            raise ChannelNotFoundError(f"Channel not found: {channel_id}")
        return SimpleNamespace(agent_id=self.owners[channel_id])

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

    async def run_command(self, command: str, *, agent_id: str, project_id: str | None) -> str:
        self.calls.append(("command", command, agent_id, project_id or ""))
        return self._answer()

    async def fetch_url(self, url: str) -> str:
        self.calls.append(("url", url))
        return self._answer()

    def _answer(self) -> str:
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer
