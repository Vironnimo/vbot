"""Automation RPC method registry."""

from __future__ import annotations

from typing import Any

from server.rpc.dispatcher import RpcMethodHandler


def method_handlers(delegates: Any) -> dict[str, RpcMethodHandler]:
    """Return automation RPC handlers from the delegates facade."""

    return {
        "cron.create": delegates._cron_create,
        "cron.list": delegates._cron_list,
        "cron.update": delegates._cron_update,
        "cron.delete": delegates._cron_delete,
        "cron.enable": delegates._cron_enable,
        "cron.disable": delegates._cron_disable,
    }
