"""Repair persisted native intentions independently of post-commit publication.

Management record commits precede service callbacks. Recovery derives current
authority from those records, so an interruption in that seam cannot strand
working tasks or retain archived-scope canonical groups.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

type Json = dict[str, Any]


async def repair_tasks(native: Any) -> None:
    executions = await native.all_executions()
    for task in await native.all_tasks():
        if task["state"] in {"done", "cancelled"}:
            continue
        related = [
            item
            for item in executions
            if item["kind"] in {"task", "review"} and item["scope_id"] == task["id"]
        ]
        current = [item for item in related if item["epoch"] == task["epoch"]]
        factory = await native.store.get_factory(task["factory_id"])
        stale = False
        for execution in current:
            member = await native.store.get_member(execution["member_id"])
            if (
                execution["runtime"].get("factory_revision") != factory["revision"]
                or execution["runtime"].get("member_revision") != member["revision"]
            ):
                stale = True
                break
        patch: Json = {}
        if (
            factory["state"] == "active"
            and task["state"] == "paused"
            and task["reason"] == "factory_paused"
        ):
            patch.update(state="ready", reason="")
        if stale:
            patch["epoch"] = task["epoch"] + 1
            if isinstance(task.get("submission"), dict):
                patch["submission"] = {**task["submission"], "epoch": patch["epoch"]}
            if task["state"] == "working":
                patch.update(
                    state="ready" if factory["state"] == "active" else "paused",
                    reason="" if factory["state"] == "active" else "factory_paused",
                )
        elif task["state"] == "working" and not current:
            # A task agreement/assignment edit committed its new epoch before
            # changed() could turn the previous working projection into ready.
            patch.update(state="ready", reason="")
        elif task["state"] == "working" and all(
            item["state"] in {"idle", "closed", "failed", "cancelled"} for item in current
        ):
            failure = next(
                (item["runtime"].get("failure") for item in current if item["state"] == "failed"),
                None,
            )
            patch.update(state="blocked", reason=failure or "input")
        if (
            task["state"] == "review"
            and not (task.get("submission") or {}).get("reviewer_approved")
            and any(
                item["kind"] == "review"
                and item["state"] == "idle"
                and not item["runtime"].get("pending")
                for item in current
            )
        ):
            patch.update(state="blocked", reason="review_input")
        if (
            task.get("reviewer_id")
            and task["state"] == "review"
            and (await native.store.get_member(task["reviewer_id"]))["state"] == "retired"
        ):
            patch.update(state="blocked", reason="reviewer_retired")
        if (
            task.get("assignee_id")
            and task["state"] not in {"paused", "review"}
            and (await native.store.get_member(task["assignee_id"]))["state"] == "retired"
        ):
            patch.update(state="blocked", reason="member_retired")
        if patch:
            runtime = deepcopy(task["runtime"])
            wakes = runtime.setdefault("wakes", [])
            transferred = runtime.setdefault("transferred_inputs", [])
            latest_epoch = max((item["epoch"] for item in related), default=0)
            for execution in related:
                pending = execution["runtime"].get("pending")
                if (
                    execution["kind"] == "task"
                    and execution["epoch"] == latest_epoch
                    and pending
                    and pending["id"] not in transferred
                    and not any(item["id"] == pending["id"] for item in wakes)
                ):
                    wakes.append({"id": pending["id"], "content": pending["content"]})
                    transferred.append(pending["id"])
            patch["runtime"] = runtime
            task = await native.store.change_task(
                task["id"], patch, actor="factory", event="scope_recovered"
            )
            await native._publish("tasks", task)


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
            native.service.spawn(native._close(execution, archive=True))
    for factory in factories.values():
        if factory["state"] == "archived":
            await native.workflows.fence(factory["id"], archive=True)


async def recover_results(native: Any) -> None:
    for task in await native.all_tasks():
        if task["state"] == "done" and task.get("parent_id"):
            await native._task_done(task)
        if (
            task.get("workflow_card_id")
            and task["state"] == "blocked"
            and task["reason"] == "workflow"
        ):
            card = await native.workflows.linked_card(task)
            if card is not None and card["state"] in {"done", "discarded"}:
                await native.workflow_finished(card)
    for card in await native.service.records().list_cards():
        scope = await native.workflows.scope(card)
        stale = scope is not None and (
            (
                scope[0].get("workflow_card_id") != scope[1]["id"]
                and (
                    scope[0]["epoch"] != scope[1]["runtime"].get("native_epoch")
                    or scope[0]["workflow_id"] != scope[1]["workflow_id"]
                )
            )
            or scope[0]["state"] in {"done", "cancelled"}
        )
        if stale and card["state"] not in {"done", "discarded"}:
            await native.service.execution.discard(card)
