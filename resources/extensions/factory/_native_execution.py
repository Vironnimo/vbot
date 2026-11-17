"""Native Factory admission over canonical identity-backed owned Sessions.

All domain transitions share FactoryExtension.lock. Durable execution records
hold exact input intentions; the host alone owns Runs, receipts and draining.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, cast

from core.chat import parse_bare_model
from core.tools import ToolContext
from core.utils.ids import new_id

from ._native_sessions import NativeSessions
from .store import FactoryError

type Json = dict[str, Any]


_TERMINAL = frozenset({"done", "cancelled"})


class NativeFactoryExecution:
    """Native scope coordination, scheduling and durable work intentions."""

    def __init__(self, service: Any) -> None:
        self.service = service
        self.quiescing = False
        self.sessions = NativeSessions(self)

    @property
    def store(self) -> Any:
        return self.service.records().native

    @property
    def groups(self) -> Any:
        return self.service.host.temporary_agents

    @staticmethod
    def owns(group_id: str) -> bool:
        return group_id.startswith("native.")

    @staticmethod
    def topic_epoch(factory: Json, member: Json) -> int:
        return int((factory["revision"] << 32) + member["revision"])

    @property
    def discussions(self) -> Any:
        if not hasattr(self, "_discussions"):
            from ._native_discussions import NativeDiscussions

            self._discussions = NativeDiscussions(self)
        return self._discussions

    @property
    def workflows(self) -> Any:
        if not hasattr(self, "_workflows"):
            from ._native_workflows import NativeWorkflows

            self._workflows = NativeWorkflows(self)
        return self._workflows

    async def _all(self, method: str, *args: Any, **kwargs: Any) -> list[Json]:
        result: list[Json] = []
        after = ""
        while True:
            page = await getattr(self.store, method)(*args, after=after, limit=100, **kwargs)
            result.extend(page)
            if len(page) < 100:
                return result
            after = page[-1]["id"]

    async def all_tasks(self, factory_id: str | None = None) -> list[Json]:
        return await self._all("list_tasks", factory_id)

    async def all_executions(self, factory_id: str | None = None) -> list[Json]:
        return await self._all("list_executions", factory_id)

    async def group_ids(self) -> set[str]:
        return {item["group_id"] for item in await self.all_executions()}

    async def running(self) -> int:
        return sum(
            item["state"] in {"admitting", "running", "draining"}
            for item in await self.all_executions()
        )

    async def _find(self, group_id: str) -> Json:
        # The opaque execution id is the sole native group suffix, never a
        # caller-selected member or task id.
        if not self.owns(group_id):
            raise FactoryError("stale_run", "This Factory execution is no longer current.")
        execution: Json = await self.store.get_execution(group_id.removeprefix("native."))
        if execution["group_id"] != group_id:
            raise FactoryError("stale_run", "This Factory execution is no longer current.")
        return execution

    async def _scope(self, execution: Json) -> tuple[Json, Json, Json]:
        factory = await self.store.get_factory(execution["factory_id"])
        member = await self.store.get_member(execution["member_id"])
        scope = await getattr(
            self.store, "get_task" if execution["kind"] in {"task", "review"} else "get_topic"
        )(execution["scope_id"])
        return factory, member, scope

    async def _valid(self, execution: Json) -> bool:
        if self.quiescing or execution["state"] in {"closed", "cancelled", "failed", "draining"}:
            return False
        factory, member, scope = await self._scope(execution)
        if factory["state"] != "active" or member["state"] != "active":
            return False
        if execution["runtime"].get("member_revision") != member["revision"]:
            return False
        if execution["runtime"].get("factory_revision") != factory["revision"]:
            return False
        if execution["kind"] in {"task", "review"}:
            return bool(
                scope["epoch"] == execution["epoch"]
                and scope["state"] == ("review" if execution["kind"] == "review" else "working")
                and (
                    scope.get("reviewer_id")
                    if execution["kind"] == "review"
                    else scope["assignee_id"]
                )
                == member["id"]
            )
        if scope["state"] != "open":
            return False
        if scope.get("task_id"):
            task = await self.store.get_task(scope["task_id"])
            if task["state"] in {"paused", "cancelled"}:
                return False
        return True

    async def actor(self, context: ToolContext) -> Json:
        owner = context.execution_owner
        if owner is None or owner.extension != "factory":
            raise FactoryError(
                "participant_required", "Use this Tool in a Factory task or discussion Session."
            )
        execution = await self._find(owner.group_id)
        runtime = execution["runtime"]
        address = runtime.get("address", {})
        if (
            not await self._valid(execution)
            or execution["state"] != "running"
            or owner.epoch != runtime.get("group_epoch")
            or owner.participant_id != execution["participant_id"]
            or owner.generation_id != runtime.get("generation_id")
            or context.agent_id != address.get("agent_id")
            or context.session_id != address.get("session_id")
            or context.run_id != runtime.get("run_id")
        ):
            raise FactoryError("stale_run", "This Factory execution is no longer current.")
        factory, member, scope = await self._scope(execution)
        return {
            "factory": factory,
            "member": member,
            "task": scope if execution["kind"] in {"task", "review"} else {},
            "topic": scope if execution["kind"] == "topic" else {},
            "epoch": execution["epoch"],
            "execution": execution,
        }

    async def _publish(self, resource: str, record: Json) -> None:
        host = self.service.host
        if host is not None and host.publish_change is not None:
            host.publish_change(resource, [record["id"]], record["revision"])

    async def changed(self, resource: str, record: Json, previous: Json | None = None) -> None:
        """Reconcile domain mutations while their caller holds the service lock."""
        await self._publish(resource, record)
        if (
            resource == "tasks"
            and previous is not None
            and record["epoch"] != previous["epoch"]
            and record["state"] == "working"
        ):
            record = await self.store.change_task(
                record["id"],
                {"state": "ready", "reason": ""},
                actor="factory",
                event="scope_changed",
            )
            await self._publish(resource, record)
        if previous is not None and resource in {"factories", "members"}:
            factory_id = record["id"] if resource == "factories" else record["factory_id"]
            for task in await self.all_tasks(factory_id):
                if (
                    resource == "members"
                    and task["assignee_id"] != record["id"]
                    and task.get("reviewer_id") != record["id"]
                ):
                    continue
                if task["state"] in _TERMINAL:
                    continue
                patch: Json = {"epoch": task["epoch"] + 1}
                submission = task.get("submission")
                if task["state"] == "review" and isinstance(submission, dict):
                    patch["submission"] = {**submission, "epoch": patch["epoch"]}
                task_runtime = deepcopy(task["runtime"])
                wakes = task_runtime.setdefault("wakes", [])
                transferred = task_runtime.setdefault("transferred_inputs", [])
                for execution in await self.all_executions(factory_id):
                    pending = execution["runtime"].get("pending")
                    if (
                        execution["kind"] == "task"
                        and execution["scope_id"] == task["id"]
                        and execution["epoch"] == task["epoch"]
                        and pending
                        and pending["id"] not in transferred
                        and not any(wake["id"] == pending["id"] for wake in wakes)
                    ):
                        wakes.append({"id": pending["id"], "content": pending["content"]})
                        transferred.append(pending["id"])
                patch["runtime"] = task_runtime
                if (
                    resource == "members"
                    and record["state"] == "retired"
                    and task["state"] == "review"
                    and task.get("reviewer_id") == record["id"]
                ):
                    patch.update(state="blocked", reason="reviewer_retired")
                if (
                    resource == "factories"
                    and record["state"] == "active"
                    and task["state"] == "paused"
                    and task["reason"] == "factory_paused"
                ):
                    patch.update(state="ready", reason="")
                elif task["state"] == "working":
                    patch.update(
                        state="ready" if record["state"] == "active" else "paused",
                        reason="" if record["state"] == "active" else "factory_paused",
                    )
                if patch:
                    task = await self.store.change_task(
                        task["id"], patch, actor="factory", event="scope_changed"
                    )
                    await self._publish("tasks", task)
        if resource in {"tasks", "members", "factories", "topics"}:
            for execution in await self.all_executions(
                record.get("factory_id") or (record["id"] if resource == "factories" else None)
            ):
                archive = (resource == "factories" and record["state"] == "archived") or (
                    resource == "members"
                    and record["state"] == "retired"
                    and execution["member_id"] == record["id"]
                )
                if archive or (
                    execution["state"]
                    not in {
                        "closed",
                        "cancelled",
                        "failed",
                        "draining",
                    }
                    and not await self._valid(execution)
                ):
                    await self.store.change_execution(
                        execution["id"], {"state": "draining"}, actor="factory"
                    )
                    self.service.spawn(
                        self.sessions._close(
                            execution,
                            archive=archive,
                            wait=resource == "tasks" and record["state"] in {"done", "review"},
                        )
                    )
        if (
            resource == "tasks"
            and record["state"] == "done"
            and (previous or {}).get("state") != "done"
        ):
            await self._task_done(record)
        if resource == "tasks":
            await self.workflows.changed(record, previous)
        if resource in {"factories", "members"}:
            await self.workflows.fence(
                record["id"] if resource == "factories" else record["factory_id"],
                archive=resource == "factories" and record["state"] == "archived",
            )
        self.service.request_schedule()

    async def block(self, execution: Json, reason: str, detail: Json | None = None) -> None:
        saved = await self.store.change_execution(
            execution["id"],
            {
                "state": "draining",
                "runtime": {**execution["runtime"], "failure": reason, "limit_hit": detail},
            },
            actor="factory",
        )
        if execution["kind"] in {"task", "review"}:
            task = await self.store.get_task(execution["scope_id"])
            if task["epoch"] == execution["epoch"] and task["state"] not in _TERMINAL | {"paused"}:
                task = await self.store.change_task(
                    task["id"],
                    {"state": "blocked", "reason": reason},
                    actor="factory",
                    event=reason,
                )
                await self._publish("tasks", task)
        self.service.spawn(self.sessions._close(saved, final_state="failed"))
        self.service.request_schedule()

    async def _prepare(self, kind: str, scope: Json, member: Json, content: str) -> Json:
        factory = await self.store.get_factory(scope["factory_id"])
        epoch = scope["epoch"] if kind in {"task", "review"} else self.topic_epoch(factory, member)
        existing = [
            item
            for item in await self.all_executions(scope["factory_id"])
            if (item["kind"], item["scope_id"], item["member_id"], item["epoch"])
            == (kind, scope["id"], member["id"], epoch)
        ]
        execution: Json
        if existing:
            execution = existing[0]
            if execution["state"] in {"running", "admitting"}:
                return execution
            if execution["state"] == "failed" and scope.get("state") != "ready":
                return execution
            runtime = deepcopy(execution["runtime"])
            runtime["pending"] = {"id": new_id("fin"), "kind": "continuation", "content": content}
            return cast(
                Json,
                await self.store.change_execution(
                    execution["id"], {"state": "admitting", "runtime": runtime}, actor="factory"
                ),
            )
        # Claim the record id first; the persisted group address is derived from
        # it in the same intention update before any canonical Session exists.
        execution = await self.store.create_execution(
            scope["factory_id"],
            kind,
            scope["id"],
            member["id"],
            epoch,
            group_id="",
            participant_id=member["id"],
            runtime={
                "member_revision": member["revision"],
                "factory_revision": (await self.store.get_factory(scope["factory_id"]))["revision"],
                "pending": {
                    "id": (scope.get("runtime", {}).get("wakes") or [{}])[0].get(
                        "id", new_id("fin")
                    ),
                    "kind": "initial",
                    "content": content,
                    "native_updates": [
                        wake["content"] for wake in scope.get("runtime", {}).get("wakes", [])[:1]
                    ],
                },
                "runs": [],
                "tool_calls": 0,
            },
            actor="factory",
            state="admitting",
        )
        return execution

    async def schedule(self, limit: int) -> None:
        """Called only by the single Factory scheduler, under its lock."""
        if self.quiescing:
            return
        from ._native_agent_text import native_input
        from ._native_recovery import recover_results, repair_tasks

        await repair_tasks(self)
        await recover_results(self)
        await self.discussions.wake_origins()
        executions = await self.all_executions()
        for execution in executions:
            if execution["state"] == "admitting" and await self._valid(execution):
                self.sessions._launch_later(execution)
        for task in sorted(
            await self.all_tasks(),
            key=lambda item: (-item["priority"], item["created_at"], item["id"]),
        ):
            task = await self._dispatch_wake(task, limit)
            dependencies_done = all(
                [
                    (await self.store.get_task(key))["state"] == "done"
                    for key in task["dependencies"]
                ]
            )
            if (
                task["state"] == "blocked"
                and task["reason"] == "dependencies"
                and dependencies_done
            ):
                task = await self.store.change_task(
                    task["id"],
                    {"state": "ready", "reason": ""},
                    actor="factory",
                    event="dependencies_ready",
                )
                await self._publish("tasks", task)
            if task["state"] not in {"ready", "review"}:
                if task.get("workflow_card_id") and task["state"] in {"working", "blocked"}:
                    await self.workflows.resume(task)
                continue
            factory = await self.store.get_factory(task["factory_id"])
            member_id = (
                task.get("reviewer_id") if task["state"] == "review" else task["assignee_id"]
            )
            if task["state"] == "review" and (task.get("submission") or {}).get(
                "reviewer_approved"
            ):
                continue
            if factory["state"] != "active" or not member_id:
                continue
            member = await self.store.get_member(member_id)
            if member["state"] != "active":
                continue
            if task["state"] == "ready" and await self.workflows.resume(task):
                continue
            if task["state"] == "ready" and not dependencies_done:
                task = await self.store.change_task(
                    task["id"],
                    {"state": "blocked", "reason": "dependencies"},
                    actor="factory",
                    event="dependencies_waiting",
                )
                await self._publish("tasks", task)
                continue
            kind = "review" if task["state"] == "review" else "task"
            if any(
                item["scope_id"] == task["id"]
                and item["kind"] == kind
                and item["epoch"] == task["epoch"]
                and item["state"]
                in (
                    {"running", "admitting", "idle"}
                    if kind == "review"
                    else {"running", "admitting"}
                )
                for item in executions
            ):
                continue
            if not await self._capacity(factory, member, limit):
                continue
            if kind == "task" and task.get("workflow_id") and not task.get("workflow_card_id"):
                await self.workflows.start(task, factory)
                continue
            execution = await self._prepare(
                kind,
                task,
                member,
                native_input(
                    kind,
                    task,
                    factory=factory,
                    member=member,
                    updates=[wake["content"] for wake in task["runtime"].get("wakes", [])[:1]],
                ),
            )
            if kind == "task":
                runtime = deepcopy(task["runtime"])
                runtime["wakes"] = runtime.get("wakes", [])[1:]
                task = await self.store.change_task(
                    task["id"],
                    {"state": "working", "reason": "", "runtime": runtime},
                    actor="factory",
                    event="admission_prepared",
                )
                await self._publish("tasks", task)
            self.sessions._launch_later(execution)
            executions = await self.all_executions()
        await self._schedule_topics(limit)

    async def _capacity(self, factory: Json, member: Json, limit: int) -> bool:
        active = [
            item
            for item in await self.all_executions()
            if item["state"] in {"running", "admitting", "draining"}
        ]
        cards = await self.service.records().list_cards()
        factory_workflows = member_workflows = 0
        for card in cards:
            if not self.service.execution.running(card):
                continue
            scope = await self.workflows.scope(card)
            if scope is not None and scope[0]["factory_id"] == factory["id"]:
                factory_workflows += 1
                member_workflows += scope[0]["assignee_id"] == member["id"]
        return bool(
            len(active) + sum(self.service.execution.running(card) for card in cards) < limit
            and sum(item["factory_id"] == factory["id"] for item in active) + factory_workflows
            < factory.get("capacity", limit)
            and sum(item["member_id"] == member["id"] for item in active) + member_workflows
            < member.get("capacity", 1)
        )

    @property
    def limits(self) -> Any:
        if not hasattr(self, "_limits"):
            from ._native_limits import NativeLimits

            self._limits = NativeLimits(self)
        return self._limits

    @staticmethod
    def _model_allowed(model: str, allowed: list[str]) -> bool:
        return model in allowed or parse_bare_model(model) in allowed

    async def reconcile(self) -> None:
        self.quiescing = False
        async with self.service.lock:
            from ._native_recovery import recover_archives, recover_results, repair_tasks

            await repair_tasks(self)
            await recover_archives(self)
            await recover_results(self)
            for execution in await self.all_executions():
                if execution["state"] == "draining":
                    self.service.spawn(
                        self.sessions._close(
                            execution,
                            final_state="failed"
                            if execution["runtime"].get("failure")
                            else "closed",
                        )
                    )
                    continue
                if execution["state"] not in {"running", "admitting"}:
                    continue
                if not await self._valid(execution):
                    await self.store.change_execution(
                        execution["id"], {"state": "draining"}, actor="factory"
                    )
                    self.service.spawn(self.sessions._close(execution))
                    continue
                await self.sessions._acknowledge(execution)
                execution = await self.store.get_execution(execution["id"])
                run_id = execution["runtime"].get("run_id")
                if run_id:
                    inspected = (await self.groups.owned_runs(execution["group_id"], [run_id])).get(
                        run_id
                    )
                    if (
                        inspected is not None
                        and inspected.run is not None
                        and inspected.run.status.value == "running"
                    ):
                        continue
                    if execution["runtime"].get("pending"):
                        self.sessions._launch_later(execution)
                    else:
                        await self.sessions._finished(
                            execution,
                            "success"
                            if inspected is not None
                            and inspected.record.terminal_status == "completed"
                            else "error",
                        )
                elif execution["runtime"].get("pending"):
                    self.sessions._launch_later(execution)

    async def _task_done(self, task: Json) -> None:
        from ._native_agent_text import native_input

        if task.get("parent_id"):
            parent = await self.store.get_task(task["parent_id"])
            marks = parent["runtime"].get("child_results", {})
            if (
                parent["state"] not in _TERMINAL | {"paused", "review", "backlog"}
                and marks.get(task["id"], 0) < task["revision"]
            ):
                await self._wake_task(
                    parent,
                    native_input("child_result", task, parent=parent),
                    runtime_patch={"child_results": {**marks, task["id"]: task["revision"]}},
                )

    async def _wake_task(
        self, task: Json, content: str, *, runtime_patch: Json | None = None
    ) -> None:
        if task["state"] in _TERMINAL | {"paused", "review", "backlog"}:
            return
        if task["state"] == "blocked" and task["reason"] not in {
            "input",
            "peer",
            "children",
            "workflow",
        }:
            return
        runtime = deepcopy(task["runtime"])
        runtime.update(runtime_patch or {})
        runtime.setdefault("wakes", []).append({"id": new_id("fin"), "content": content})
        # The read marker and durable wake commit in this one task mutation.
        # Dispatch can crash at any later point: the exact receipt stays stable.
        task = await self.store.change_task(
            task["id"],
            {
                "state": "working" if task["state"] == "working" else "ready",
                "reason": "",
                "runtime": runtime,
            },
            actor="factory",
            event="wake_prepared",
        )
        await self._publish("tasks", task)
        self.service.request_schedule()

    async def _dispatch_wake(self, task: Json, limit: int) -> Json:
        wakes = task["runtime"].get("wakes", [])
        if not wakes or task["state"] not in {"ready", "working"}:
            return task
        current = next(
            (
                item
                for item in await self.all_executions(task["factory_id"])
                if item["kind"] == "task"
                and item["scope_id"] == task["id"]
                and item["epoch"] == task["epoch"]
            ),
            None,
        )
        if current is None:
            return task
        if current["state"] != "running":
            factory = await self.store.get_factory(task["factory_id"])
            member = await self.store.get_member(task["assignee_id"])
            if (
                factory["state"] != "active"
                or member["state"] != "active"
                or not await self._capacity(factory, member, limit)
            ):
                return task
        pending = current["runtime"].get("pending")
        wake = wakes[0]
        if pending and pending["id"] != wake["id"]:
            return task
        runtime = deepcopy(current["runtime"])
        runtime["pending"] = {**wake, "kind": "continuation"}
        current = await self.store.change_execution(
            current["id"],
            {
                "runtime": runtime,
                "state": "running" if current["state"] == "running" else "admitting",
            },
            actor="factory",
        )
        task_runtime = deepcopy(task["runtime"])
        task_runtime["wakes"] = wakes[1:]
        task = await self.store.change_task(
            task["id"],
            {"state": "working", "reason": "", "runtime": task_runtime},
            actor="factory",
            event="wake_dispatched",
        )
        if current["state"] == "admitting":
            self.sessions._launch_later(current)
        return task

    async def workflow_finished(self, card: Json, *, new_result: bool = False) -> None:
        from ._native_agent_text import native_input

        task_id = card["runtime"].get("native_task_id")
        if not task_id:
            return
        if card["state"] == "done" and (
            new_result or not card["runtime"].get("native_result_version")
        ):
            card = await self.service.records().change(
                card["id"],
                {"runtime": {**card["runtime"], "native_result_version": card["revision"]}},
                actor="factory",
                event="workflow_result_recorded",
            )
        task = await self.store.get_task(task_id)
        if (
            task["workflow_card_id"] == card["id"]
            and task["epoch"] == card["runtime"].get("native_epoch")
            and task["state"] == "blocked"
            and task["reason"] == "workflow"
        ):
            if card["state"] == "done":
                marks = task["runtime"].get("workflow_results", {})
                version = card["runtime"]["native_result_version"]
                if marks.get(card["id"], 0) < version:
                    await self._wake_task(
                        task,
                        native_input("workflow_result", task, card=card),
                        runtime_patch={"workflow_results": {**marks, card["id"]: version}},
                    )
            else:
                task = await self.store.change_task(
                    task_id, {"reason": "workflow_failed"}, actor="factory", event="workflow_failed"
                )
                await self._publish("tasks", task)

    async def _schedule_topics(self, limit: int) -> None:
        await self.discussions.schedule(limit)
