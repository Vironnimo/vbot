"""Workflow budgets: settled usage, hard admission and receipt-backed threshold notices."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

from core.extensions import PreparedSessionDelivery, SessionRequestContext
from core.utils.ids import new_id

from . import _limits, _workflows, agent_text

type Json = dict[str, Any]

if TYPE_CHECKING:
    from ._execution import FactoryExecution


def _sum_reports(reports: list[Json]) -> Json:
    """Add usage reports of several execution groups."""
    total: Json = {}
    for report in reports:
        for key, value in report.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            total[key] = total.get(key, 0) + value
    return total


class WorkflowBudgets:
    """Workflow budget admission and receipt-backed threshold notices."""

    def __init__(self, execution: FactoryExecution) -> None:
        self.execution = execution
        self.service = execution.service

    @property
    def store(self) -> Any:
        return self.service.store

    async def budget_status(
        self, card: Json, step_id: str, *, resources: bool = True, report_all: bool = False
    ) -> Json:
        runtime = card["runtime"]
        card_limits, step_limits = _limits.configured(runtime, step_id)
        result: Json = {}
        for scope, limits in (("card", card_limits), ("step", step_limits)):
            report = None
            if resources and (report_all or limits.get("tokens") or limits.get("cost_usd")):
                host = self.service.host
                if scope == "step":
                    report = await host.execution_usage(
                        _workflows.group_id(card["id"], step_id),
                        runtime["steps"][step_id].get("budget", {}).get("runs", []),
                    )
                else:
                    report = _sum_reports(
                        [
                            await host.execution_usage(group_id, None)
                            for group_id in self.execution.group_ids(card)
                        ]
                    )
            result[scope] = {
                "limits": limits,
                "usage": _limits.consumption(runtime, scope, step_id, report),
            }
        return result

    async def _check_budget(
        self, card: Json, step_id: str, *, check_runs: bool, resources: bool = True
    ) -> bool:
        if (
            native := getattr(self.service, "native", None)
        ) and not await native.workflows.permitted(card):
            await native.workflows.halt(card)
            return True
        if (native := getattr(self.service, "native", None)) and await native.limits.workflow_check(
            card, check_runs=check_runs, resources=resources
        ):
            runtime = self.execution._runtime(card)
            for state in runtime.get("steps", {}).values():
                if state.get("status") == "running":
                    self.execution._halt(state, "budget_exhausted")
            await self.execution._commit(card, runtime, event="budget_exhausted")
            return True
        status = await self.budget_status(card, step_id, resources=resources)
        for scope, item in status.items():
            limits = (
                item["limits"] if resources else {"tool_calls": item["limits"].get("tool_calls", 0)}
            )
            hit = _limits.reached(limits, item["usage"], scope, check_runs=check_runs)
            if hit is not None:
                runtime = self.execution._runtime(card)
                state = runtime["steps"][step_id]
                state.update(limit_hit=hit, note=_limits.message(hit))
                self.execution._halt(state, "budget_exhausted")
                await self.execution._commit(card, runtime, event="budget_exhausted", detail=hit)
                return True
        return False

    async def _prepare_budget_notice(
        self, card: Json, runtime: Json, step_id: str, context: SessionRequestContext
    ) -> Json | None:
        recipient = f"{context.agent_id}/{context.session_id}"
        notice = runtime.setdefault("budget_notices", {}).setdefault(recipient, {"seen": []})
        warnings = _limits.warnings(
            runtime,
            step_id,
            await self.budget_status({**card, "runtime": runtime}, step_id),
            notice["seen"],
        )
        if not warnings:
            return None
        return {
            "id": new_id("fnotice"),
            "recipient": recipient,
            "content": agent_text.budget_warning(warnings),
            "keys": [warning["key"] for warning in warnings],
            "revision": str(card["revision"]),
        }

    @staticmethod
    def _acknowledge_budget_notice(runtime: Json, pending: Json) -> None:
        notice = runtime.setdefault("budget_notices", {}).setdefault(
            pending["recipient"], {"seen": []}
        )
        notice["seen"] = list(dict.fromkeys([*notice["seen"], *pending["keys"]]))
        notice.pop("pending", None)

    async def _budget_notice(
        self, card: Json, step_id: str, context: SessionRequestContext
    ) -> PreparedSessionDelivery | None:
        runtime = self.execution._runtime(card)
        recipient = f"{context.agent_id}/{context.session_id}"
        notice = runtime.setdefault("budget_notices", {}).setdefault(recipient, {"seen": []})
        pending = notice.get("pending")
        if pending is None:
            pending = await self._prepare_budget_notice(card, runtime, step_id, context)
            if pending is None:
                return None
            notice["pending"] = pending
            await self.store.change(card["id"], {"runtime": runtime}, actor="factory", event=None)
        content = pending["content"]
        return PreparedSessionDelivery(
            pending["id"],
            hashlib.sha256(content.encode("utf-8")).hexdigest(),
            (content,),
            pending["revision"],
            "factory_budget_notice",
        )
