"""The secrets Tool through production dispatch: values reach the store, never the Agent."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from core.chat import ReplySurface
from core.providers.adapter import tool_result_text
from core.runs import RunKind
from core.secrets.agent_secrets import AgentSecrets
from core.secrets.requests import SecretRequests
from core.tools.secrets import register_secrets_tool
from core.tools.tools import ToolContext, ToolRegistry
from tests.core.secrets.secrets_test_support import data_directory, open_store
from tests.core.tools.tools_test_support import dispatch_as_executor

pytestmark = pytest.mark.asyncio

PASSWORD = "hunter2-very-secret"


class _Tool:
    def __init__(self, tmp_path: Path, *, timeout: float = 60.0) -> None:
        clock = lambda: datetime(2026, 10, 21, 12, tzinfo=UTC)  # noqa: E731
        self.secrets = AgentSecrets(open_store(tmp_path, data_directory(tmp_path)), clock=clock)
        self.secrets.reload()
        self.requests = SecretRequests(self.secrets, clock=clock, timeout=timeout)
        self.registry = ToolRegistry()
        self.registry.set_secrets(self.secrets)
        register_secrets_tool(self.registry, self.secrets, self.requests)
        self.tmp_path = tmp_path

    async def call(
        self, arguments: dict[str, object], *, surface: ReplySurface | None = None
    ) -> tuple[bool, str]:
        context = ToolContext(
            agent_id="main",
            session_id="s1",
            run_id="run-1",
            tool_call_id="call",
            tool_name="secrets",
            tool_call_index=0,
            workspace=self.tmp_path,
            vbot_root=self.tmp_path,
            data_root=self.tmp_path,
            run_kind=RunKind.USER,
            reply_surface=surface or ReplySurface(kind="webui"),
        )
        result = await dispatch_as_executor(self.registry, context, arguments)
        return bool(result["ok"]), str(tool_result_text(json.dumps(result)))

    async def pending(self) -> dict[str, object]:
        for _ in range(100):
            if listed := self.requests.list():
                return listed[0]
            await asyncio.sleep(0)
        raise AssertionError("no request appeared")


async def test_a_requested_secret_reaches_the_agent_only_as_a_reference(tmp_path: Path) -> None:
    tool = _Tool(tmp_path)

    call = asyncio.create_task(
        tool.call(
            {"action": "request", "name": "github", "kind": "password", "reason": "push to vbot"}
        )
    )
    pending = await tool.pending()
    assert pending["name"] == "GITHUB" and pending["kind"] == "login"
    assert pending["description"] == "push to vbot"
    await tool.requests.answer(str(pending["id"]), {"username": "julian", "password": PASSWORD})
    ok, text = await call

    assert ok and PASSWORD not in text
    assert "username julian" in text and "{{secret:GITHUB.password}}" in text
    assert tool.secrets.substitute("main", "s1", "{{secret:GITHUB.password}}", "terminal")[0] == (
        PASSWORD
    )
    assert tool.requests.list() == []
    ok, listed = await tool.call({"action": "list"})
    assert ok and "- GITHUB (login, this session)" in listed and PASSWORD not in listed


async def test_a_request_ends_declined_or_expired_without_a_secret(tmp_path: Path) -> None:
    tool = _Tool(tmp_path)
    call = asyncio.create_task(tool.call({"action": "request", "name": "API"}))
    tool.requests.decline(str((await tool.pending())["id"]))
    ok, declined = await call

    expiring = _Tool(tmp_path / "other", timeout=0.01)
    ok_expired, expired = await expiring.call({"action": "request", "name": "API"})

    assert ok and "declined" in declined
    assert ok_expired and "/secret API <value>" in expired
    assert tool.secrets.all() == [] and expiring.requests.list() == []


async def test_a_channel_user_gets_the_command_instead_of_a_form(tmp_path: Path) -> None:
    tool = _Tool(tmp_path)
    telegram = ReplySurface(
        kind="channel",
        platform="telegram",
        platform_display_name="Telegram",
        channel_id="chat-1",
        conversation_kind="direct",
    )

    ok, text = await tool.call(
        {"action": "request", "name": "CODE", "lifetime": "once"}, surface=telegram
    )

    assert ok and "/secret CODE --once <value>" in text
    assert tool.requests.list() == []


async def test_generate_stores_a_password_the_agent_never_sees(tmp_path: Path) -> None:
    tool = _Tool(tmp_path)

    ok, text = await tool.call(
        {"action": "generate", "name": "SHOP", "username": "julian@example.com", "symbols": ""}
    )
    again_ok, again = await tool.call({"action": "generate", "name": "shop"})

    (secret,) = tool.secrets.usable("main", "s1")
    password = tool.secrets.substitute("main", "s1", "{{secret:SHOP.password}}", "terminal")[0]
    assert ok and password not in text and "{{secret:SHOP.password}}" in text
    assert len(password) == 24 and password.isalnum()
    assert secret.lifetime == "permanent" and secret.agents == ("main",)
    assert not again_ok and "already exists" in again


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({"action": "remove"}, 'Unknown action "remove". Use one of: list, request, generate.'),
        ({"action": "request"}, "request needs name"),
        ({"action": "request", "name": "X1", "lifetime": "weekly"}, 'Unknown lifetime "weekly"'),
        ({"action": "generate", "name": "X1", "length": 4}, "from 8 to 128"),
    ],
)
async def test_invalid_calls_are_refused_with_what_to_do(
    tmp_path: Path, arguments: dict[str, object], expected: str
) -> None:
    tool = _Tool(tmp_path)

    ok, text = await tool.call(arguments)

    assert not ok and expected in text
    assert tool.secrets.all() == []


async def test_the_prompt_block_lists_only_the_agents_own_secrets_without_values(
    tmp_path: Path,
) -> None:
    from types import SimpleNamespace

    from core.tools.tools import ToolPromptBlockRegistry

    tool = _Tool(tmp_path)
    blocks = ToolPromptBlockRegistry()
    register_secrets_tool(ToolRegistry(), tool.secrets, tool.requests, blocks)
    (definition,) = blocks.block_definitions()
    tool.secrets.create(
        name="github",
        kind="login",
        values={"username": "julian", "password": PASSWORD},
        actor="user",
        agents=["main"],
    )
    tool.secrets.create(name="other", kind="token", values={"value": "x" * 20}, actor="user")
    tool.secrets.create(
        name="session_key",
        kind="token",
        values={"value": "y" * 20},
        actor="user",
        lifetime="session",
        session_id="s1",
    )

    def render(agent_id: str, *, subagent_session: bool = False) -> Any:
        context = SimpleNamespace(
            agent=SimpleNamespace(id=agent_id), subagent_session=subagent_session
        )
        return definition.render(context)

    rendered = render("main")
    empty = render("helper")

    assert [key for key, _ in rendered.catalog.entries] == ["GITHUB"]
    assert "username julian" in rendered.text and "{{secret:GITHUB.password}}" in rendered.text
    assert PASSWORD not in rendered.text
    assert empty == ""
    # A Sub-Agent Session of the same Agent has only the Secrets passed to it.
    assert render("main", subagent_session=True) == ""
