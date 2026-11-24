"""Repair persisted native intentions independently of post-commit publication."""

from __future__ import annotations

from typing import Any

type Json = dict[str, Any]


async def recover_archives(native: Any) -> None:
    factories = {item["id"]: item for item in await native._all("list_factories")}
    for execution in await native.all_executions():
        member = await native.store.get_member(execution["member_id"])
        if (
            factories[execution["factory_id"]]["state"] == "archived"
            or member["state"] == "retired"
        ):
            if execution["state"] != "closed":
                await native.store.change_execution(
                    execution["id"], {"state": "draining"}, actor="factory"
                )
            native.service.spawn(native.sessions._close(execution, archive=True))
