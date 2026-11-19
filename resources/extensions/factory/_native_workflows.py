"""Native task authority around the unchanged Workflow execution owner."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import replace
from typing import Any, cast

from core.agents.temporary import TemporaryAgentConfig
from core.tools.availability import ToolAccess
from core.utils.ids import new_id

from . import _workflows, agent_text
from ._native_agent_text import native_input, native_workflow_instructions
from .store import FactoryError

type Json = dict[str, Any]


class NativeWorkflows:
    def __init__(self, native: Any) -> None:
        self.native = native

    async def linked_card(self, task: Json) -> Json | None:
        if not task.get("workflow_card_id"):
            return None
        try:
            return cast(
                Json, await self.native.service.records().get_card(task["workflow_card_id"])
            )
        except FactoryError as error:
            if error.code != "card_not_found":
                raise
        patch: Json = {"workflow_card_id": ""}
        if task["state"] not in {"done", "cancelled", "paused"}:
            patch.update(state="blocked", reason="workflow_missing", epoch=task["epoch"] + 1)
            if isinstance(task.get("submission"), dict):
                patch["submission"] = {**task["submission"], "epoch": patch["epoch"]}
        previous = deepcopy(task)
        saved = await self.native.store.change_task(
            task["id"], patch, actor="factory", event="workflow_missing"
        )
        task.update(saved)
        await self.native.changed("tasks", saved, previous)
        return None

    async def _policy(self, task: Json) -> str:
        factory = await self.native.store.get_factory(task["factory_id"])
        member = await self.native.store.get_member(task["assignee_id"])
        value = {
            "factory": {key: factory.get(key) for key in ("config", "remit", "leader_member_id")},
            "member": {
                key: member.get(key) for key in ("config", "role", "instructions", "agent_proof")
            },
        }
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

    async def start(self, task: Json, factory: Json) -> None:
        service = self.native.service
        # The Card's persisted provenance is also the recovery key for a crash
        # after Card creation but before the native association commits.
        card = next(
            (
                item
                for item in await service.records().list_cards()
                if item["runtime"].get("native_task_id") == task["id"]
                and item["runtime"].get("native_epoch") == task["epoch"]
                and item["workflow_id"] == task["workflow_id"]
            ),
            None,
        )
        if card is None:
            try:
                workflow = await service.records().get_workflow(task["workflow_id"])
                if not workflow["enabled"] or workflow["project_id"] not in {
                    "",
                    factory["project_id"],
                }:
                    raise FactoryError(
                        "workflow_unavailable",
                        "The selected Workflow is unavailable for this Factory Project.",
                    )
            except FactoryError:
                task = await self.native.store.change_task(
                    task["id"],
                    {"state": "blocked", "reason": "workflow_unavailable"},
                    actor="factory",
                    event="workflow_unavailable",
                )
                await self.native._publish("tasks", task)
                return
            card = await service._new_card(
                workflow,
                project_id=factory["project_id"],
                title=task["title"],
                description=task["objective"],
                origin="workflow",
                runtime={
                    "native_task_id": task["id"],
                    "native_epoch": task["epoch"],
                    "native_policy": await self._policy(task),
                },
            )
        task = await self.native.store.change_task(
            task["id"],
            {"workflow_card_id": card["id"], "state": "blocked", "reason": "workflow"},
            actor="factory",
            event="workflow_started",
        )
        await self.native._publish("tasks", task)
        if card["state"] in {"done", "discarded"}:
            await self.native.workflow_finished(card)

    async def scope(self, card: Json) -> tuple[Json, Json] | None:
        root = card
        seen: set[str] = set()
        while root["id"] not in seen:
            seen.add(root["id"])
            if task_id := root["runtime"].get("native_task_id"):
                return await self.native.store.get_task(task_id), root
            if not root.get("parent_id"):
                return None
            try:
                root = await self.native.service.records().get_card(root["parent_id"])
            except FactoryError as error:
                if error.code != "card_not_found":
                    raise
                return None
        return None

    async def permitted(self, card: Json) -> bool:
        scope = await self.scope(card)
        if scope is None:
            return True
        task, root = scope
        if not task.get("assignee_id") or task.get("workflow_card_id") != root["id"]:
            return False
        factory = await self.native.store.get_factory(task["factory_id"])
        member = await self.native.store.get_member(task["assignee_id"])
        return bool(
            factory["state"] == "active"
            and member["state"] == "active"
            and (
                (task["state"] == "blocked" and task["reason"] == "workflow")
                or (
                    root["state"] == "done"
                    and card["id"] != root["id"]
                    and task["state"] in {"ready", "working", "blocked", "review"}
                )
            )
            and task["workflow_card_id"] == root["id"]
            and task["epoch"] == root["runtime"].get("native_epoch")
            and card["project_id"] in {"", factory["project_id"]}
            and card["runtime"].get("workflow", {}).get("project_id", "")
            in {"", factory["project_id"]}
        )

    async def admissible(self, card: Json, limit: int) -> bool:
        scope = await self.scope(card)
        if scope is None:
            return True
        if not await self.permitted(card):
            return False
        task, _ = scope
        factory = await self.native.store.get_factory(task["factory_id"])
        member = await self.native.store.get_member(task["assignee_id"])
        return bool(await self.native._capacity(factory, member, limit))

    async def definition_permitted(self, card: Json, workflow: Json) -> bool:
        scope = await self.scope(card)
        if scope is None:
            return True
        task, _ = scope
        factory = await self.native.store.get_factory(task["factory_id"])
        if workflow["project_id"] in {"", factory["project_id"]}:
            return True
        task = await self.native.store.change_task(
            task["id"],
            {"state": "blocked", "reason": "workflow_unavailable"},
            actor="factory",
            event="workflow_unavailable",
        )
        await self.native._publish("tasks", task)
        await self.halt(card)
        return False

    async def config(
        self, card: Json, config: TemporaryAgentConfig, grants: set[str] | frozenset[str]
    ) -> TemporaryAgentConfig:
        scope = await self.scope(card)
        if scope is None:
            return config
        factory = await self.native.store.get_factory(scope[0]["factory_id"])
        member = await self.native.store.get_member(scope[0]["assignee_id"])
        boundary = factory.get("config", {})
        local = member.get("config", {})
        allowed = boundary.get("allowed_tools")
        if "allowed_tools" in local:
            allowed = (
                local["allowed_tools"]
                if allowed is None
                else list(set(allowed) & set(local["allowed_tools"]))
            )
        denied = (
            set(config.tool_access.denied)
            | set(boundary.get("denied_tools", []))
            | set(local.get("denied_tools", []))
        )
        skills = boundary.get("allowed_skills", ["*"])
        local_skills = local.get("allowed_skills", ["*"])
        skills = (
            local_skills
            if "*" in skills
            else skills
            if "*" in local_skills
            else [skill for skill in skills if skill in local_skills]
        )
        return replace(
            config,
            tool_access=ToolAccess(
                mode="all" if allowed is None else "selected",
                allowed=tuple(sorted(set(allowed or []) | set(grants))),
                denied=tuple(sorted(denied)),
                fixed=allowed is not None,
            ),
            allowed_skills=skills,
            permission_ceiling=boundary.get("permission_ceiling"),
            permission_ceiling_profile=boundary.get("permission_ceiling_profile"),
            instructions=config.instructions
            + "\n\n"
            + native_workflow_instructions(factory, member),
        )

    async def model_permitted(self, card: Json, model: str | None) -> bool:
        scope = await self.scope(card)
        if scope is None or model is None:
            return True
        task, _ = scope
        factory = await self.native.store.get_factory(task["factory_id"])
        allowed = factory.get("config", {}).get("allowed_models")
        if allowed is None or self.native._model_allowed(model, allowed):
            return True
        task = await self.native.store.change_task(
            task["id"],
            {"state": "blocked", "reason": "model_unavailable"},
            actor="factory",
            event="model_unavailable",
        )
        await self.native._publish("tasks", task)
        await self.halt(card)
        return False

    async def tool_permitted(self, card: Json, tool_name: str) -> bool:
        from ._native_policy import tool_permitted

        scope = await self.scope(card)
        if scope is None:
            return True
        factory = await self.native.store.get_factory(scope[0]["factory_id"])
        member = await self.native.store.get_member(scope[0]["assignee_id"])
        return await tool_permitted(
            self.native.service,
            [factory.get("config", {}), member.get("config", {})],
            tool_name,
            {"factory_submit_work", "factory_report_blocked", "factory_request_changes"},
        )

    async def halt(self, card: Json) -> None:
        owner = self.native.service.execution
        runtime = deepcopy(card["runtime"])
        cancel = set()
        for key, state in runtime.get("steps", {}).items():
            if state.get("status") == "running":
                owner._halt(state, "native_paused")
                cancel.add(key)
        if not runtime.get("steps"):
            runtime["blocker"] = {"reason": "native_paused", "note": ""}
        if cancel or runtime.get("blocker"):
            await owner._commit(card, runtime, event="native_paused", cancel=cancel)

    async def changed(self, task: Json, previous: Json | None) -> None:
        card_id = task.get("workflow_card_id") or (previous or {}).get("workflow_card_id")
        if not card_id:
            return
        service = self.native.service
        if task.get("workflow_card_id"):
            card = await self.linked_card(task)
            if card is None:
                return
        else:
            try:
                card = await service.records().get_card(card_id)
            except FactoryError as error:
                if error.code != "card_not_found":
                    raise
                return
        ended = task["state"] in {"done", "cancelled"}
        replaced = (
            previous is not None
            and task["epoch"] != previous["epoch"]
            and task["state"] != "paused"
        )
        if ended or replaced:
            for item in await service.records().list_cards():
                scope = await self.scope(item)
                if (
                    scope
                    and scope[1]["id"] == card["id"]
                    and item["state"] not in {"done", "discarded"}
                ):
                    await service.execution.discard(item)
            if replaced and task.get("workflow_card_id"):
                saved = await self.native.store.change_task(
                    task["id"], {"workflow_card_id": ""}, actor="factory", event="workflow_detached"
                )
                await self.native._publish("tasks", saved)
            return
        if task["state"] == "paused":
            for item in await service.records().list_cards():
                scope = await self.scope(item)
                if (
                    scope
                    and scope[1]["id"] == card["id"]
                    and item["state"] not in {"done", "discarded"}
                ):
                    await self.halt(item)

    async def resume(self, task: Json) -> bool:
        if not task.get("workflow_card_id"):
            return False
        service = self.native.service
        card = await self.linked_card(task)
        if card is None:
            return True
        root_done = card["state"] == "done"
        result_version = card["runtime"].get("native_result_version", card["revision"])
        if card["state"] == "discarded" or (
            root_done
            and task["runtime"].get("workflow_results", {}).get(card["id"], 0) < result_version
        ):
            if task["state"] == "ready" and card["state"] == "discarded":
                saved = await self.native.store.change_task(
                    task["id"], {"workflow_card_id": ""}, actor="factory", event="workflow_retried"
                )
                task.update(saved)
            elif (
                card["state"] == "done"
                and task["runtime"].get("workflow_results", {}).get(card["id"], 0) < result_version
                and task["state"] in {"ready", "blocked"}
            ):
                runtime = {**card["runtime"], "native_epoch": task["epoch"]}
                card = await service.records().change(
                    card["id"],
                    {"runtime": runtime},
                    actor="factory",
                    event="workflow_result_recovered",
                )
                saved = await self.native.store.change_task(
                    task["id"],
                    {"state": "blocked", "reason": "workflow"},
                    actor="factory",
                    event="workflow_result_ready",
                )
                await self.native.workflow_finished(card)
                task.update(saved)
                return True
            return False
        factory = await self.native.store.get_factory(task["factory_id"])
        if factory["state"] != "active":
            return True
        linked = []
        for item in await service.records().list_cards():
            scope = await self.scope(item)
            if scope and scope[1]["id"] == card["id"]:
                linked.append(item)
        paused = any(
            item["runtime"].get("blocker", {}).get("reason") == "native_paused"
            or any(
                state.get("reason") == "native_paused"
                for state in item["runtime"].get("steps", {}).values()
            )
            for item in linked
        )
        policy = await self._policy(task)
        policy_changed = card["runtime"].get("native_policy") != policy
        if (
            (task["state"] != "ready" or root_done)
            and not paused
            and not policy_changed
            and card["runtime"].get("native_epoch") == task["epoch"]
        ):
            return not root_done
        explicit_resume = task["state"] == "ready"
        if task["state"] == "ready" and not root_done:
            task = await self.native.store.change_task(
                task["id"],
                {"state": "blocked", "reason": "workflow"},
                actor="factory",
                event="workflow_resumed",
            )
            await self.native._publish("tasks", task)
        runtime = deepcopy(card["runtime"])
        runtime["native_epoch"] = task["epoch"]
        runtime["native_policy"] = policy
        card = await service.records().change(
            card["id"], {"runtime": runtime}, actor="factory", event="native_resumed"
        )
        for item in linked:
            item = await service.records().get_card(item["id"])
            if policy_changed:
                step_runtime = deepcopy(item["runtime"])
                for state in step_runtime.get("steps", {}).values():
                    if state.get("participant"):
                        state["native_policy_dirty"] = True
                item = await service.records().change(
                    item["id"],
                    {"runtime": step_runtime},
                    actor="factory",
                    event="native_policy_changed",
                )
            if item["runtime"].get("blocker") and (
                explicit_resume or item["runtime"]["blocker"].get("reason") == "native_paused"
            ):
                await service.execution.retry(item)
                continue
            for step_id, state in item["runtime"].get("steps", {}).items():
                if state.get("reason") == "native_paused" or (
                    explicit_resume
                    and state.get("reason")
                    in {"run_failed", "restart", "budget_exhausted", "upgraded", "child_failed"}
                ):
                    current = await service.records().get_card(item["id"])
                    if policy_changed:
                        await self._refresh_participant(current, step_id, task)
                    else:
                        await service.execution.retry(current, step_id)
        return not root_done

    async def refresh_pending(self, card: Json, step_id: str) -> bool:
        scope = await self.scope(card)
        if scope is None:
            return False
        state = card["runtime"]["steps"][step_id]
        participant = state["pending"]["participant"]
        policy = await self._policy(scope[0])
        policies = state.get("native_participant_policies", {})
        if policies.get(participant) != policy and (
            participant in policies or participant in state.get("participants", {})
        ):
            await self._refresh_participant(card, step_id, scope[0])
            return True
        if policies.get(participant) != policy:
            runtime = deepcopy(card["runtime"])
            runtime["steps"][step_id].setdefault("native_participant_policies", {})[participant] = (
                policy
            )
            saved = await self.native.service.records().change(
                card["id"], {"runtime": runtime}, actor="factory", event="native_policy_prepared"
            )
            card.update(saved)
        return False

    async def _refresh_participant(self, card: Json, step_id: str, task: Json) -> None:
        owner = self.native.service.execution
        runtime = deepcopy(card["runtime"])
        state = runtime["steps"][step_id]
        state.pop("native_policy_dirty", None)
        step = _workflows.step(runtime["workflow"], step_id)
        previous = state.get("participants", {}).get(state.get("participant"), {})
        state["native_policy_generation"] = task["epoch"]
        participant = owner._participant(state, step)
        history = await owner._history(card, [])
        earlier = owner.latest(
            history["outputs"],
            runtime["workflow"],
            _workflows.ancestors(runtime["workflow"], step_id),
        )
        content = agent_text.step_input(
            card,
            step,
            comments=history["comments"],
            earlier=earlier,
            findings=[
                item
                for item in history["outputs"]
                if item["kind"] == "changes" and item["step"] == step_id
            ],
            note={"kind": "keep_working", "text": ""},
            trigger=history.get("trigger"),
        )
        content += "\n\n" + native_input(
            "workflow_resume",
            task,
            previous_session=previous,
            updates=[state["pending"]["content"]] if state.get("pending") else [],
        )
        state.update(
            status="running",
            epoch=new_id("fence"),
            pending={
                "id": new_id("fci"),
                "participant": participant,
                "kind": "initial",
                "content": content,
                "cancel_previous": True,
            },
        )
        for key in ("reason", "question", "note"):
            state.pop(key, None)
        await owner._commit(card, runtime, event="retried", actor="factory", cancel={step_id})

    async def fence(self, factory_id: str, *, archive: bool = False) -> None:
        service = self.native.service
        for card in await service.records().list_cards():
            scope = await self.scope(card)
            if scope is None or scope[0]["factory_id"] != factory_id:
                continue
            if not await self.permitted(card):
                await self.halt(card)
            if archive:
                current = await service.records().get_card(card["id"])
                if current["state"] not in {"done", "discarded"}:
                    await service.execution.discard(current)
                for group_id in service.execution.group_ids(card):
                    service.spawn(self._archive(group_id))

    async def _archive(self, group_id: str) -> None:
        await self.native.groups.close_group(group_id)
        await self.native.groups.archive_group(group_id)
