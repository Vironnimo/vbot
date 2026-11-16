"""Native budgets retain all epochs and cover descendants at admission."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from typing import Any

import pytest

from resources.extensions.factory._native_limits import NativeLimits

type Json = dict[str, Any]
pytestmark = pytest.mark.asyncio


class Native:
    def __init__(self) -> None:
        self.tasks: list[Json] = [
            {"id": "parent", "limits": {"runs": 3}},
            {"id": "child", "parent_id": "parent", "limits": {}},
        ]
        self.executions: list[Json] = [
            {
                "id": "old",
                "factory_id": "factory",
                "kind": "task",
                "scope_id": "parent",
                "group_id": "old-group",
                "runtime": {"runs": ["old-run"], "tool_calls": 1},
            },
            {
                "id": "current",
                "factory_id": "factory",
                "kind": "task",
                "scope_id": "child",
                "group_id": "child-group",
                "runtime": {"runs": ["child-run"], "tool_calls": 1},
            },
        ]
        self.factory: Json = {"limits": {}}
        self.topics: Json = {"topic": {"task_id": "child"}}
        self.reports: Json = {}
        self.blocks: list[Json] = []
        self.reads: list[str] = []
        self.cards: list[Json] = []
        self.store = self
        self.service = SimpleNamespace(
            host=SimpleNamespace(execution_usage=self.usage),
            records=lambda: self,
            execution=SimpleNamespace(group_ids=lambda card: [card["group_id"]]),
        )

    async def list_cards(self) -> list[Json]:
        return deepcopy(self.cards)

    async def get_task(self, task_id: str) -> Json:
        return deepcopy(next(item for item in self.tasks if item["id"] == task_id))

    async def change_task(self, task_id: str, patch: Json, **kwargs: Any) -> Json:
        task = next(item for item in self.tasks if item["id"] == task_id)
        task.update(deepcopy(patch))
        return deepcopy(task)

    async def _publish(self, resource: str, record: Json) -> None:
        assert resource == "tasks"

    async def all_tasks(self, factory_id: str) -> list[Json]:
        return deepcopy(self.tasks)

    async def all_executions(self, factory_id: str) -> list[Json]:
        return deepcopy(self.executions)

    async def get_factory(self, factory_id: str) -> Json:
        return deepcopy(self.factory)

    async def get_topic(self, topic_id: str) -> Json:
        return deepcopy(self.topics[topic_id])

    async def change_execution(self, execution_id: str, patch: Json, *, actor: str) -> Json:
        item = next(item for item in self.executions if item["id"] == execution_id)
        item.update(deepcopy(patch))
        return deepcopy(item)

    async def usage(self, group: str, runs: list[str] | None) -> Json:
        assert runs is None  # includes auxiliary and owned descendant requests
        self.reads.append(group)
        return deepcopy(self.reports.get(group, {}))

    async def block(self, execution: Json, reason: str, detail: Json) -> None:
        assert reason == "budget_exhausted"
        self.blocks.append(detail)


async def test_native_budget_receipts_survive_reconstruction_and_assignment_epochs() -> None:
    native = Native()
    limits = NativeLimits(native)
    execution = deepcopy(native.executions[-1])
    await limits.observe_run(execution, "child-run")
    await limits.observe_run(execution, "descendant-run")
    await limits.count_tool(execution, "exact-tool-receipt")
    limits = NativeLimits(native)
    execution = deepcopy(native.executions[-1])
    await limits.count_tool(execution, "exact-tool-receipt")
    assert execution["runtime"]["tool_calls"] == 2
    assert execution["runtime"]["runs"] == ["child-run", "descendant-run"]
    assert await limits.check(execution, check_runs=True)
    assert native.blocks[-1] == {
        "scope": "task:parent",
        "metric": "runs",
        "used": 3,
        "limit": 3,
        "reason": "limit_reached",
    }


@pytest.mark.parametrize(
    ("metric", "report", "reason"),
    [
        ("tokens", {"calls": 1, "input_tokens": 7, "output_tokens": 3}, "limit_reached"),
        ("tokens", {"calls": 1, "unreported_calls": 1}, "usage_unavailable"),
        ("cost_usd", {"calls": 1, "cost_usd": 10}, "limit_reached"),
        ("cost_usd", {"calls": 1, "cost_usd": None, "unpriced_calls": 1}, "usage_unavailable"),
    ],
)
async def test_native_factory_resource_limits_include_unrelated_and_topic_work(
    metric: str, report: Json, reason: str
) -> None:
    native = Native()
    native.tasks[0]["limits"] = {}
    native.factory["limits"] = {metric: 10}
    native.executions.append(
        {
            "id": "topic-execution",
            "factory_id": "factory",
            "kind": "topic",
            "scope_id": "topic",
            "group_id": "topic-group",
            "runtime": {},
        }
    )
    native.reports["old-group"] = report
    limits = NativeLimits(native)
    assert await limits.check(native.executions[-1])
    assert native.blocks[-1]["scope"] == "factory"
    assert native.blocks[-1]["reason"] == reason
    assert set(native.reads) == {"old-group", "child-group", "topic-group"}


async def test_native_tool_reservation_checks_ancestor_cap_without_resource_reads() -> None:
    native = Native()
    native.tasks[0]["limits"] = {"tool_calls": 2, "tokens": 1}
    limits = NativeLimits(native)
    assert await limits.check(native.executions[-1], resources=False)
    assert native.blocks[-1]["metric"] == "tool_calls"
    assert native.reads == []


async def test_linked_workflow_descendants_charge_task_and_factory_budgets() -> None:
    native = Native()
    native.tasks[0]["limits"] = {"tool_calls": 4, "tokens": 20}
    for task in native.tasks:
        task["factory_id"] = "factory"
    native.cards = [
        {
            "id": "workflow",
            "group_id": "workflow-group",
            "runtime": {
                "native_task_id": "child",
                "budget": {"runs": ["wf-run"], "tool_calls": 1},
            },
        },
        {
            "id": "nested",
            "group_id": "nested-group",
            "parent_id": "workflow",
            "runtime": {
                "budget": {"runs": ["nested-run"], "tool_calls": 1},
            },
        },
    ]
    native.reports["nested-group"] = {"calls": 1, "input_tokens": 15, "output_tokens": 5}
    limits = NativeLimits(native)
    status = await limits.status(native.executions[-1])
    assert status["task:parent"]["usage"]["tool_calls"] == 4
    assert status["task:parent"]["usage"]["runs"] == 4
    assert status["task:parent"]["usage"]["tokens"] == 20
    assert await limits.workflow_check(native.cards[-1], resources=False)
    assert native.tasks[-1]["reason"] == "budget_exhausted"
    assert native.tasks[-1]["limit_hit"]["scope"] == "task:parent"
