"""Agent and session RPC method registry."""

from __future__ import annotations

from typing import Any, cast

from server.rpc.dispatcher import RpcMethodHandler


def method_handlers(delegates: Any) -> dict[str, RpcMethodHandler]:
    """Return agent and session RPC handlers from the delegates facade."""

    def list_agents(state: Any, _params: dict[str, Any]) -> dict[str, Any]:
        return cast(dict[str, Any], delegates._list_agents(state))

    return {
        "agent.list": list_agents,
        "agent.get": delegates._get_agent,
        "agent.create": delegates._create_agent,
        "agent.update": delegates._update_agent,
        "agent.delete": delegates._delete_agent,
        "session.create": delegates._create_session,
        "session.list": delegates._list_sessions,
        "session.link_channel": delegates._link_session_to_channel,
    }
