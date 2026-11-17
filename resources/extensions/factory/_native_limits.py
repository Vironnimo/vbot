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

type Json = dict[str, Any]


class NativeLimits:
    def __init__(self, native: Any) -> None:
        self.native = native

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
        cards = await self.native.service.records().list_cards()
        task_id = self._card_task(card, {item["id"]: item for item in cards})
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
        tasks = {task["id"]: task for task in await self.native.all_tasks(factory_id)}
        executions = await self.native.all_executions(factory_id)
        # Include a newly created or updated record supplied by admission even if
        # a caller's enumeration snapshot predates the durable reservation.
        executions = [item for item in executions if item["id"] != execution["id"]]
        executions.append(execution)
        task_id = await self._task_id(execution)
        scopes: list[tuple[str, Json, set[str] | None]] = []
        visited: set[str] = set()
        while task_id and task_id not in visited:
            visited.add(task_id)
            task = tasks[task_id]
            descendants = {task_id}
            while True:
                expanded = descendants | {
                    item["id"] for item in tasks.values() if item.get("parent_id") in descendants
                }
                if expanded == descendants:
                    break
                descendants = expanded
            scopes.append((f"task:{task_id}", task.get("limits", {}), descendants))
            task_id = task.get("parent_id", "")
        scopes.append(("factory", factory.get("limits", {}), None))
        result: Json = {}
        reports: dict[str, Json] = {}
        task_ids = {item["id"]: await self._task_id(item) for item in executions}
        cards = await self.native.service.records().list_cards()
        card_index = {item["id"]: item for item in cards}
        card_tasks = {item["id"]: self._card_task(item, card_index) for item in cards}
        for scope, limits, scope_tasks in scopes:
            selected = [
                item
                for item in executions
                if scope_tasks is None or task_ids[item["id"]] in scope_tasks
            ]
            runs: set[str] = set()
            calls = 0
            groups: set[str] = set()
            for item in selected:
                runtime = item.get("runtime", {})
                runs.update(runtime.get("runs", []))
                calls += runtime.get("tool_calls", 0)
                if item.get("group_id"):
                    groups.add(item["group_id"])
            for card in cards:
                linked_task = card_tasks[card["id"]]
                if linked_task not in tasks or (
                    scope_tasks is not None and linked_task not in scope_tasks
                ):
                    continue
                budget = card.get("runtime", {}).get("budget", {})
                runs.update(budget.get("runs", []))
                calls += budget.get("tool_calls", 0)
                groups.update(self.native.service.execution.group_ids(card))
            report: Json = {}
            if resources and (report_all or limits.get("tokens") or limits.get("cost_usd")):
                for group in groups:
                    if group not in reports:
                        reports[group] = await self.native.service.host.execution_usage(group, None)
                    for key, value in reports[group].items():
                        if isinstance(value, (int, float)) and not isinstance(value, bool):
                            report[key] = report.get(key, 0) + value
                    if reports[group].get("calls") and reports[group].get("cost_usd") is None:
                        # reached() uses unpriced_calls for partially priced
                        # reports, so unknown cost cannot disappear in a sum.
                        report["unpriced_calls"] = max(1, report.get("unpriced_calls", 0))
            result[scope] = {
                "limits": limits,
                "usage": _limits.consumption(
                    {"budget": {"runs": list(runs), "tool_calls": calls}}, "card", "", report
                ),
            }
        return result

    @staticmethod
    def _card_task(card: Json, cards: dict[str, Json]) -> str:
        seen: set[str] = set()
        while card["id"] not in seen:
            seen.add(card["id"])
            task_id = card.get("runtime", {}).get("native_task_id", "")
            if task_id:
                return cast(str, task_id)
            parent = cards.get(card.get("parent_id", ""))
            if parent is None:
                break
            card = parent
        return ""

    async def _task_id(self, execution: Json) -> str:
        if execution["kind"] in {"task", "review"}:
            return cast(str, execution["scope_id"])
        topic = await self.native.store.get_topic(execution["scope_id"])
        return cast(str, topic.get("task_id", ""))
