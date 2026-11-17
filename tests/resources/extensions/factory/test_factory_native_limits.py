"""Native budgets retain all epochs and cover descendants at admission."""

from __future__ import annotations

import asyncio
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
        self._feed: dict[tuple[str, str], Json] = {}
        self._revision = 0
        self.service = SimpleNamespace(
            host=SimpleNamespace(execution_usage_changes=self.usage_changes),
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

    async def budget_changes(self, after: int = 0, **kwargs: Any) -> Json:
        rows: dict[tuple[str, str], Json] = {}
        for task in self.tasks:
            rows[("task", task["id"])] = {
                "id": task["id"],
                "factory_id": task.get("factory_id", "factory"),
                "parent_id": task.get("parent_id", ""),
            }
        for key, topic in self.topics.items():
            rows[("topic", key)] = {"id": key, "factory_id": "factory", **topic}
        for item in self.executions:
            rows[("execution", item["id"])] = {
                **item,
                "execution_kind": item["kind"],
                "runs": item["runtime"].get("runs", []),
                "tool_calls": item["runtime"].get("tool_calls", 0),
            }
        for item in self.cards:
            rows[("card", item["id"])] = {
                **item,
                "task_id": item["runtime"].get("native_task_id", ""),
                **item["runtime"].get("budget", {}),
                "steps": [],
            }
        changes = []
        for record_key in self._feed.keys() | rows.keys():
            if not after or rows.get(record_key) != self._feed.get(record_key):
                self._revision += 1
                changes.append(
                    {"kind": record_key[0], "id": record_key[1], "value": rows.get(record_key)}
                )
        self._feed = deepcopy(rows)
        return {
            "ledger_id": "domain",
            "revision": self._revision,
            "more": False,
            "records": changes,
        }

    async def usage_changes(self, after: int = 0, **kwargs: Any) -> Json:
        self.reads.append("changes")
        return {
            "ledger_id": "usage",
            "revision": after + 1,
            "more": False,
            "groups": [
                {
                    "group_id": key,
                    "usage": {
                        **report,
                        "cost_nano_usd": round((report.get("cost_usd") or 0) * 1e9),
                    },
                }
                for key, report in self.reports.items()
            ],
        }

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
    assert native.reads == ["changes"]


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


async def test_unlimited_admission_reads_only_its_ancestor_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native = Native()
    native.tasks[0]["limits"] = {}

    async def unexpected_scan(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Uncapped admission enumerated the Factory")

    monkeypatch.setattr(native, "all_tasks", unexpected_scan)
    monkeypatch.setattr(native, "all_executions", unexpected_scan)
    monkeypatch.setattr(native, "list_cards", unexpected_scan)
    limits = NativeLimits(native)
    assert not await limits.check(native.executions[-1])
    assert not await limits.check(native.executions[-1], resources=False)
    assert native.reads == []


async def test_incremental_totals_follow_corrections_reparenting_and_reconstruction() -> None:
    native = Native()
    native.factory["limits"] = {"cost_usd": 100}
    native.reports["child-group"] = {
        "calls": 1,
        "input_tokens": 10,
        "output_tokens": 2,
        "cost_nano_usd": 400_000_000,
        "cost_usd": 0.4,
    }
    limits = NativeLimits(native)
    execution = native.executions[-1]
    first = await limits.status(execution, report_all=True)
    assert first["task:parent"]["usage"]["cost_usd"] == 0.4
    native.reports["child-group"].update(input_tokens=20, cost_usd=0.1)
    corrected = await limits.status(execution, report_all=True)
    assert corrected["task:parent"]["usage"]["tokens"] == 22
    assert corrected["factory"]["usage"]["cost_usd"] == 0.1
    assert await limits.status(execution, report_all=True) == corrected
    native.tasks[-1]["parent_id"] = ""
    moved = await limits.status(execution, report_all=True)
    assert "task:parent" not in moved
    parent = await limits.status(native.executions[0], report_all=True)
    assert parent["task:parent"]["usage"]["tool_calls"] == 1
    assert parent["task:parent"]["usage"]["cost_usd"] == 0
    assert moved["factory"]["usage"] == parent["factory"]["usage"]
    assert await NativeLimits(native).status(execution, report_all=True) == moved
    feed, usage_feed = native.budget_changes, native.usage_changes

    async def restored_feed(after: int = 0, **kwargs: Any) -> Json:
        page = await feed(0 if kwargs.get("ledger_id") != "restored-domain" else after)
        return {**page, "ledger_id": "restored-domain"}

    async def restored_usage(after: int = 0, **kwargs: Any) -> Json:
        return {**await usage_feed(after), "ledger_id": "restored-usage"}

    native.service.records = lambda: SimpleNamespace(budget_changes=restored_feed)
    native.service.host.execution_usage_changes = restored_usage
    assert await limits.status(execution, report_all=True) == moved


async def test_cancelled_projection_page_does_not_skip_unapplied_changes() -> None:
    native = Native()
    snapshot = (await native.budget_changes())["records"]
    entered = asyncio.Event()
    block = True

    async def feed(after: int = 0, **kwargs: Any) -> Json:
        if after == 0:
            return {"ledger_id": "paged", "revision": 1, "more": True, "records": snapshot[:2]}
        entered.set()
        if block:
            await asyncio.Event().wait()
        return {"ledger_id": "paged", "revision": 2, "more": False, "records": snapshot[2:]}

    native.service.records = lambda: SimpleNamespace(budget_changes=feed)
    limits = NativeLimits(native)
    interrupted = asyncio.create_task(limits.status(native.executions[-1]))
    await entered.wait()
    interrupted.cancel()
    with pytest.raises(asyncio.CancelledError):
        await interrupted
    block = False
    status = await limits.status(native.executions[-1])
    assert status["task:parent"]["usage"]["runs"] == 2
    assert status["factory"]["usage"]["tool_calls"] == 2
