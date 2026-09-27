"""A registered channel_send Tool with a recording Channel service, for channel_send tests.

Calls run through production dispatch as a Provider cycle makes them: with the calling
Agent's Definition Profile contract while the profile offers the Tool, else with the
Tool's own contract, and a refused call becomes an ``invalid_arguments`` result.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

from core.channels.adapter import RouteFacts
from core.providers.adapter import tool_result_text
from core.tools.channel import CHANNEL_SEND_TOOL_NAME, register_channel_send_tool
from core.tools.tools import (
    ToolContext,
    ToolDefinitionProfileContext,
    ToolRegistry,
    is_tool_result_envelope,
    tool_failure,
)

MAX_ATTACHMENT_BYTES = 1000
OUTBOUND_SESSION = RouteFacts(agent_id="agent-1", session_id="ch-tg-main-111")


class _NullAsyncContext:
    """Stand-in for the per-Session write lock of a mocked Session manager."""

    async def __aenter__(self) -> _NullAsyncContext:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None


def make_chat_sessions(metadata: dict[str, Any] | None = None) -> Mock:
    """Return a mock ``ChatSessionManager`` whose calling Session holds ``metadata``.

    ``run_async`` runs its function inline and ``get_metadata_async`` delegates to
    ``get_metadata``, so tests configure and assert the synchronous mocks.
    """
    chat_sessions = Mock()
    chat_sessions.write_lock.return_value = _NullAsyncContext()
    chat_sessions.get_metadata.return_value = {} if metadata is None else metadata

    def run_inline(function: Any, *arguments: Any, **keyword_arguments: Any) -> Any:
        return function(*arguments, **keyword_arguments)

    chat_sessions.run_async = AsyncMock(side_effect=run_inline)
    chat_sessions.get_metadata_async = AsyncMock(
        side_effect=lambda address: chat_sessions.get_metadata(address)
    )
    return chat_sessions


def make_context(workspace: Path) -> ToolContext:
    return ToolContext(
        agent_id="agent-1",
        session_id="session-1",
        run_id="run-1",
        tool_call_id="call-1",
        tool_name=CHANNEL_SEND_TOOL_NAME,
        tool_call_index=0,
        workspace=workspace,
        vbot_root=workspace.parent,
        data_root=workspace.parent / "data",
    )


def make_channel_config(
    *,
    channel_id: str = "tg-main",
    agent_id: str = "agent-1",
    platform: str = "telegram",
    enabled: bool = True,
    allowed_chat_ids: list[int] | list[str] | None = None,
) -> Mock:
    return Mock(
        id=channel_id,
        agent_id=agent_id,
        platform=platform,
        enabled=enabled,
        allowed_chat_ids=allowed_chat_ids or [],
    )


@dataclass
class ChannelSend:
    registry: ToolRegistry
    service: Any
    sessions: Any
    workspace: Path

    def definition(self, agent_id: str = "agent-1") -> dict[str, Any] | None:
        """The definition a Provider cycle of ``agent_id`` offers, or None when hidden."""
        definitions = self.registry.provider_definitions(
            [CHANNEL_SEND_TOOL_NAME],
            profile_context=ToolDefinitionProfileContext(agent_id=agent_id),
        )
        return definitions[0] if definitions else None

    async def dispatch(self, arguments: Any, **context: Any) -> dict[str, Any]:
        """Run one call; ``context`` overrides fields of the calling ``ToolContext``."""
        tool_context = replace(make_context(self.workspace), **context)
        definition = self.definition(tool_context.agent_id)
        contracts = self.registry.contracts_for_provider_definitions(
            [] if definition is None else [definition]
        )
        tool_context = replace(tool_context, input_contract=contracts.get(CHANNEL_SEND_TOOL_NAME))
        try:
            return await self.registry.dispatch(tool_context, arguments, [CHANNEL_SEND_TOOL_NAME])
        except ValueError as error:
            return tool_failure("invalid_arguments", str(error))

    def call(self, arguments: Any, **context: Any) -> dict[str, Any]:
        return asyncio.run(self.dispatch(arguments, **context))

    def sent(self) -> list[tuple[Any, ...]]:
        """Each ``ChannelService.send`` as (channel, message, chat, keyword arguments)."""
        return [
            (call.args[0], call.args[1], call.args[2], call.kwargs)
            for call in self.service.send.await_args_list
        ]


def channel_send(
    tmp_path: Path,
    *configs: Mock,
    reply_target: dict[str, Any] | None = None,
    service: Any = None,
    sessions: Any = None,
    max_attachment_size_bytes: int = MAX_ATTACHMENT_BYTES,
) -> ChannelSend:
    """Register channel_send for ``configs`` (default: tg-main allowing chat 111).

    ``reply_target`` is the calling Session's last Reply Target. The default Channel
    service records sends and resolves every outbound note to ``OUTBOUND_SESSION``.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    if service is None:
        service = Mock()
        service.send = AsyncMock()
        service.ensure_outbound_session = AsyncMock(return_value=OUTBOUND_SESSION)
        service.list_channels.return_value = list(configs) or [
            make_channel_config(allowed_chat_ids=[111])
        ]
    if sessions is None:
        sessions = make_chat_sessions(
            {} if reply_target is None else {"last_reply_target": reply_target}
        )
    registry = ToolRegistry()
    register_channel_send_tool(
        registry, service, sessions, max_attachment_size_bytes=max_attachment_size_bytes
    )
    return ChannelSend(registry, service, sessions, workspace)


def options(**values: Any) -> dict[str, Any]:
    """The keyword arguments of a ``ChannelService.send`` call."""
    return {"files": None, "thread_id": None, "buttons": None, **values}


def model_text(envelope: dict[str, Any]) -> str:
    """The result text the Model reads."""
    return str(tool_result_text(json.dumps(envelope, ensure_ascii=False)))


def delivered(envelope: dict[str, Any]) -> dict[str, Any]:
    """Assert a successful send result and return its data."""
    assert is_tool_result_envelope(envelope) is True
    assert envelope["ok"] is True, envelope
    assert envelope["error"] is None
    assert envelope["artifacts"] == []
    data = envelope["data"]
    assert isinstance(data, dict)
    return data


def refused(envelope: dict[str, Any], code: str = "invalid_arguments") -> str:
    """Assert a failure result with ``code`` and return its message."""
    assert envelope == tool_failure(code, envelope["error"]["message"])
    message = envelope["error"]["message"]
    assert isinstance(message, str)
    return message
