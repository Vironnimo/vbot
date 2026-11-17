"""Native Factory budgets over durable executions and canonical Model Usage.

All methods run under the Factory service lock. Counters belong to executions,
not assignment epochs: aggregating every retained execution preserves spending
through reassignments, retries and restarts. Model limits check settled Usage;
an already admitted request can overshoot its remaining allocation.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, cast

from . import _limits
from ._budget_index import FactoryBudgetIndex

type Json = dict[str, Any]


class NativeLimits:
    def __init__(self, native: Any) -> None:
        self.native = native
        self.index = FactoryBudgetIndex(native.service)

    async def observe_run(self, execution: Json, run_id: str) -> None:
        runtime = deepcopy(execution.get("runtime", {}))
        runs = runtime.setdefault("runs", [])
        if run_id in runs:
            return
        runs.append(run_id)
        await self._save(execution, runtime)

    async def count_tool(self, execution: Json, tool_call_id: str) -> None:
        runtime = deepcopy(execution.get("runtime", {}))
        receipts = runtime.setdefault("tool_receipts", [])
        if tool_call_id in receipts:
            return
        receipts.append(tool_call_id)
        runtime["tool_calls"] = runtime.get("tool_calls", 0) + 1
        await self._save(execution, runtime)

    async def _save(self, execution: Json, runtime: Json) -> None:
        saved = await self.native.store.change_execution(
            execution["id"], {"runtime": runtime}, actor="factory"
        )
        execution.update(saved)

    async def check(
        self, execution: Json, *, check_runs: bool = False, resources: bool = True
    ) -> bool:
        """Return true after blocking admission on an exhausted/unknown budget."""
        for scope, item in (await self.status(execution, resources=resources)).items():
            limits = item["limits"]
            if not resources:
                limits = {"tool_calls": limits.get("tool_calls", 0)}
            hit = _limits.reached(limits, item["usage"], scope, check_runs=check_runs)
            if hit is not None:
                await self.native.block(execution, "budget_exhausted", hit)
                return True
        return False

    async def workflow_check(
        self,
        card: Json,
        *,
        check_runs: bool = False,
        resources: bool = True,
        run_id: str | None = None,
        tool_call_id: str | None = None,
    ) -> bool:
        """Check native allocations before the Workflow owner reserves its call.

        The Workflow owner retains Run/Tool counters; this helper never creates
        another reservation or resets a Workflow's own limits.
        """
        await self.index.sync(resources=False)
        task_id = self.index.card_task(card["id"])
        if not task_id:
            return False
        task = await self.native.store.get_task(task_id)
        if run_id and run_id in card.get("runtime", {}).get("budget", {}).get("runs", []):
            check_runs = False
        for scope, item in (
            await self.status_for_task(task, resources=resources, report_all=False)
        ).items():
            limits = (
                item["limits"] if resources else {"tool_calls": item["limits"].get("tool_calls", 0)}
            )
            hit = _limits.reached(limits, item["usage"], scope, check_runs=check_runs)
            if hit is not None:
                task = await self.native.store.change_task(
                    task_id,
                    {"state": "blocked", "reason": "budget_exhausted", "limit_hit": hit},
                    actor="factory",
                    event="budget_exhausted",
                )
                await self.native._publish("tasks", task)
                return True
        return False

    async def status_for_task(
        self, task: Json, *, resources: bool = True, report_all: bool = True
    ) -> Json:
        """Project a task's live consumption without changing persisted history."""
        return await self.status(
            {
                "id": "",
                "kind": "task",
                "scope_id": task["id"],
                "factory_id": task["factory_id"],
                "runtime": {},
            },
            resources=resources,
            report_all=report_all,
        )

    async def status(
        self, execution: Json, *, resources: bool = True, report_all: bool = False
    ) -> Json:
        factory_id = execution["factory_id"]
        factory = await self.native.store.get_factory(factory_id)
        ancestors: dict[str, Json] = {}
        task_id = await self._task_id(execution)
        while task_id and task_id not in ancestors:
            task = await self.native.store.get_task(task_id)
            ancestors[task_id] = task
            task_id = task.get("parent_id", "")
        metrics = {"runs", "tool_calls", "tokens", "cost_usd"} if resources else {"tool_calls"}
        if not report_all and not any(
            item.get("limits", {}).get(metric)
            for item in [factory, *ancestors.values()]
            for metric in metrics
        ):
            return {}
        read_usage = resources and (
            report_all
            or any(
                item.get("limits", {}).get(metric)
                for item in [factory, *ancestors.values()]
                for metric in ("tokens", "cost_usd")
            )
        )
        await self.index.sync(resources=read_usage)
        scopes = [(f"task:{key}", task.get("limits", {})) for key, task in ancestors.items()]
        scopes.append(("factory", factory.get("limits", {})))
        return {
            scope: {
                "limits": limits,
                "usage": self.index.usage(
                    f"factory:{factory_id}" if scope == "factory" else scope,
                    resources=read_usage,
                ),
            }
            for scope, limits in scopes
        }

    async def _task_id(self, execution: Json) -> str:
        if execution["kind"] in {"task", "review"}:
            return cast(str, execution["scope_id"])
        topic = await self.native.store.get_topic(execution["scope_id"])
        return cast(str, topic.get("task_id", ""))
