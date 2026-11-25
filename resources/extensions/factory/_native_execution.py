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

    async def _all(self, method: str, *args: Any, **kwargs: Any) -> list[Json]:
        result: list[Json] = []
        after = ""
        while True:
            page = await getattr(self.store, method)(*args, after=after, limit=100, **kwargs)
            result.extend(page)
            if len(page) < 100:
                return result
            after = page[-1]["id"]

    async def all_executions(self, factory_id: str | None = None) -> list[Json]:
        return await self._all("list_executions", factory_id)

    async def group_ids(self) -> set[str]:
        return {item["group_id"] for item in await self.all_executions()}

    async def running(self) -> int:
        total, _, _ = await self.store.active_execution_counts()
        return int(total)

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
        scope = await self.store.get_topic(execution["scope_id"])
        return factory, member, scope

    async def _valid(self, execution: Json) -> bool:
        return await self._current_scope(execution) is not None

    async def _current_scope(self, execution: Json) -> tuple[Json, Json, Json] | None:
        if self.quiescing or execution["state"] in {"closed", "cancelled", "failed", "draining"}:
            return None
        if execution["kind"] != "topic":
            return None
        factory, member, scope = await self._scope(execution)
        if factory["state"] != "active" or member["state"] != "active":
            return None
        if execution["runtime"].get("member_revision") != member["revision"]:
            return None
        if execution["runtime"].get("factory_revision") != factory["revision"]:
            return None
        if scope["state"] != "open":
            return None
        return factory, member, scope

    async def actor(self, context: ToolContext) -> Json:
        owner = context.execution_owner
        if owner is None or owner.extension != "factory":
            raise FactoryError(
                "participant_required", "Use this Tool in a Factory task or discussion Session."
            )
        execution = await self._find(owner.group_id)
        runtime = execution["runtime"]
        address = runtime.get("address", {})
        current = await self._current_scope(execution)
        if (
            current is None
            or execution["state"] != "running"
            or owner.epoch != runtime.get("group_epoch")
            or owner.participant_id != execution["participant_id"]
            or owner.generation_id != runtime.get("generation_id")
            or context.agent_id != address.get("agent_id")
            or context.session_id != address.get("session_id")
            or context.run_id != runtime.get("run_id")
        ):
            raise FactoryError("stale_run", "This Factory execution is no longer current.")
        factory, member, scope = current
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
        if resource not in {"members", "factories", "topics"}:
            self.service.request_schedule()
            return
        factory_id = record["id"] if resource == "factories" else record["factory_id"]
        executions = await self.all_executions(factory_id)
        for execution in executions:
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
                    )
                )
        if resource in {"factories", "members"}:
            job_triggers = getattr(self.service, "job_triggers", None)
            if job_triggers is not None:
                await job_triggers.sync()
            job_execution = getattr(self.service, "job_execution", None)
            if job_execution is not None and (
                (resource == "factories" and record["state"] == "archived")
                or (resource == "members" and record["state"] == "retired")
            ):
                for run in await self.service.records().jobs.list_active_runs():
                    if run["factory_id"] == factory_id and (
                        resource == "factories" or run["member_id"] == record["id"]
                    ):
                        await job_execution.cancel_run(run["id"])
            badge = getattr(self.service, "_badge", None)
            if badge is not None:
                await badge()
        self.service.request_schedule()

    async def block(self, execution: Json, reason: str, detail: Json | None = None) -> None:
        runtime = {**execution["runtime"], "failure": reason, "limit_hit": detail}
        if execution["kind"] == "topic":
            topic = await self.store.get_topic(execution["scope_id"])
            if topic.get("direct_member_id"):
                posts = await self.store.recent_posts(topic["id"], limit=1)
                runtime["failed_sequence"] = posts[-1]["sequence"] if posts else 0
                await self._publish("topics", topic)
        saved = await self.store.change_execution(
            execution["id"],
            {
                "state": "draining",
                "runtime": runtime,
            },
            actor="factory",
        )
        self.service.spawn(self.sessions._close(saved, final_state="failed"))
        self.service.request_schedule()

    async def check_member_profile(self, execution: Json, member: Json) -> bool:
        (profile,) = await self.service.native_management.identities.member_profiles([member])
        if profile["profile_available"]:
            return True
        await self.block(execution, "member_profile_unavailable")
        return False

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
        executions = await self.all_executions()
        for execution in executions:
            if execution["state"] == "admitting" and await self._valid(execution):
                self.sessions._launch_later(execution)
        await self._schedule_topics(limit)

    async def _capacity(self, factory: Json, member: Json, limit: int) -> bool:
        total, factory_active, member_active = await self.store.active_execution_counts(
            factory["id"], member["id"]
        )
        jobs_running = await self.service.job_execution.running_count()
        return bool(
            total + jobs_running < limit
            and factory_active < factory.get("capacity", limit)
            and member_active < member.get("capacity", 1)
        )

    @staticmethod
    def _model_allowed(model: str, allowed: list[str]) -> bool:
        return model in allowed or parse_bare_model(model) in allowed

    async def reconcile(self) -> None:
        self.quiescing = False
        async with self.service.lock:
            from ._native_recovery import recover_archives

            await recover_archives(self)
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
                member = await self.store.get_member(execution["member_id"])
                if not await self.check_member_profile(execution, member):
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

    async def _schedule_topics(self, limit: int) -> None:
        await self.discussions.schedule(limit)
