"""Log and prompt RPC method registry."""

from __future__ import annotations

from typing import Any, cast

from server.rpc.dispatcher import RpcMethodHandler


def method_handlers(delegates: Any) -> dict[str, RpcMethodHandler]:
    """Return log and prompt RPC handlers from the delegates facade."""

    def list_prompts(state: Any, _params: dict[str, Any]) -> dict[str, Any]:
        return cast(dict[str, Any], delegates._list_prompts(state))

    return {
        "log.list": delegates._list_logs,
        "log.read": delegates._read_log,
        "prompt.list": list_prompts,
        "prompt.update": delegates._update_prompt,
        "prompt.reset": delegates._reset_prompt,
        "prompt.preview": delegates._preview_prompt,
    }
