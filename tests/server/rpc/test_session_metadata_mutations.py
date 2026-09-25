"""Session RPCs preserve metadata owned by concurrent domain mutations."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.agents import AgentNotFoundError
from core.channels import ChannelConfig
from core.database import write_bootstrap_marker
from core.sessions import ChatSessionManager, SessionAddress
from server.events import ServerEventBus
from server.rpc.errors import RpcError
from server.rpc.session_methods import _link_session_to_channel, _set_session_compaction_policy

_POLICY = {
    "enabled": True,
    "trigger": {"type": "input_tokens", "tokens": 100_000},
    "strategy": {"type": "continuation"},
}


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["link_channel", "set_policy", "clear_policy"])
async def test_session_partial_mutation_preserves_concurrent_title(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    write_bootstrap_marker(tmp_path)
    sessions = ChatSessionManager(tmp_path)
    try:
        session = sessions.create("assistant")
        address = SessionAddress(None, "assistant", session.id)
        sessions.set_metadata(address, {"compaction_policy": _POLICY, "retained": "value"})
        replace = sessions.set_metadata
        mutate = sessions.mutate_metadata_with_previous
        concurrent_writes: list[SessionAddress] = []

        def concurrent_title(target: SessionAddress) -> None:
            # Reproduce another domain committing after any RPC pre-read but
            # immediately before its write. Both write APIs get the same race.
            concurrent_writes.append(target)
            mutate(target, lambda metadata: metadata.update({"title": "Concurrent title"}))

        def replace_after_title(target: SessionAddress, metadata: dict[str, Any]) -> None:
            concurrent_title(target)
            replace(target, metadata)

        def mutate_after_title(
            target: SessionAddress, mutation: Callable[[dict[str, Any]], None]
        ) -> tuple[dict[str, Any], dict[str, Any]]:
            concurrent_title(target)
            return mutate(target, mutation)

        monkeypatch.setattr(sessions, "set_metadata", replace_after_title)
        monkeypatch.setattr(sessions, "mutate_metadata_with_previous", mutate_after_title)
        config = ChannelConfig(id="channel", platform="telegram", agent_id="assistant")

        async def resolve_agent_async(*_args: object) -> SimpleNamespace:
            return SimpleNamespace(compaction_policy=None)

        state = SimpleNamespace(
            runtime=SimpleNamespace(
                chat_sessions=sessions,
                channel_service=SimpleNamespace(list_channels=lambda: [config]),
                agent_resolver=SimpleNamespace(resolve_agent_async=resolve_agent_async),
                storage=SimpleNamespace(load_compaction_settings=lambda: _POLICY),
            ),
            event_bus=ServerEventBus(),
        )
        params = {"agent_id": "assistant", "session_id": session.id}
        if operation == "link_channel":
            await _link_session_to_channel(
                state, {**params, "channel_id": "channel", "platform_conv_id": "conversation"}
            )
        else:
            await _set_session_compaction_policy(
                state, {**params, "policy": None if operation == "clear_policy" else _POLICY}
            )

        stored = sessions.get_metadata(address)
        assert concurrent_writes == [address]
        assert stored["title"] == "Concurrent title"
        assert stored["retained"] == "value"
        if operation == "link_channel":
            assert stored["source_channel_id"] == "channel"
            assert stored["compaction_policy"] == _POLICY
        elif operation == "clear_policy":
            assert "compaction_policy" not in stored
        else:
            assert stored["compaction_policy"] == _POLICY
    finally:
        sessions.close()


@pytest.mark.asyncio
async def test_session_policy_resolution_failure_does_not_persist_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.server.rpc.agent_methods_test_support import _make_state

    state, resolver, sessions = _make_state()

    def unresolved(*_args: object) -> None:
        raise AgentNotFoundError("missing Agent")

    monkeypatch.setattr(resolver, "resolve_agent", unresolved)
    with pytest.raises(RpcError) as exc_info:
        await _set_session_compaction_policy(
            state, {"agent_id": "assistant", "session_id": "s1", "policy": _POLICY}
        )

    assert isinstance(exc_info.value.__cause__, AgentNotFoundError)
    assert sessions.saved_metadata == {}
