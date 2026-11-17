"""Native Factory operations shared by operators and binding-authenticated Tools."""

from __future__ import annotations

import hashlib
from copy import deepcopy
from typing import TYPE_CHECKING, Any

from core.permissions import PermissionCeiling, PermissionRulesError
from core.tools import ToolContext, tool_failure, tool_success
from core.tools.arguments import optional_int
from core.tools.contracts import ToolContractError
from core.utils.workers import settle_before_cancelling

from . import _limits
from ._native_identities import FactoryIdentities
from ._native_values import CONFIG, FACTORY, LOCAL, MEMBER, OPERATION_SCHEMAS, TASK
from ._values import text
from .store import FactoryError

if TYPE_CHECKING:
    from ._native_store import NativeFactoryStore
    from .extension import FactoryExtension


type Json = dict[str, Any]


_READ_ACTIONS = {"list", "get"}


_PLURALS = {"factory": "factories", "member": "members", "task": "tasks", "topic": "topics"}


class NativeFactoryManagement:
    """Operator and authenticated member operations over native records."""

    def __init__(self, service: FactoryExtension) -> None:
        self.service = service

        self.identities = FactoryIdentities(service)

    @property
    def store(self) -> NativeFactoryStore:
        return self.service.records().native

    async def operation(self, name: str, arguments: Json, actor: Json | None = None) -> Json:
        if name not in OPERATION_SCHEMAS:
            raise FactoryError(
                "unknown_operation", "Choose an operation from the Factory operation catalog."
            )
        async with self.service.lock:
            return await settle_before_cancelling(self._operation(name, arguments, actor))

    async def private_tool(self, name: str, context: ToolContext, arguments: Json) -> Json:
        resource = {
            "factory_settings": "factory",
            "factory_members": "member",
            "factory_tasks": "task",
            "factory_topics": "topic",
        }[name]
        action = str(arguments.get("action", "")).strip().lower()
        operation = f"{resource}.{action}"
        try:
            async with self.service.lock:
                actor = await self.service.native.actor(context)
                args = dict(arguments)
                args.pop("action", None)
                if (resource == "member" and action == "create") or (
                    resource == "topic" and action == "post"
                ):
                    receipt = hashlib.sha256(
                        f"{context.run_id}:{context.tool_call_id}".encode()
                    ).hexdigest()
                    args["request_id" if resource == "member" else "source_receipt"] = receipt
                args["factory_id"] = actor["factory"]["id"]
                if resource == "factory":
                    args["id"] = actor["factory"]["id"]
                if action not in _READ_ACTIONS and args.get("id"):
                    getter = getattr(self.store, f"get_{resource}")
                    current = await getter(text(args, "id"))
                    args["expected_revision"] = current["revision"]
                result = await settle_before_cancelling(self._operation(operation, args, actor))
            if (
                resource == "task"
                and action in {"complete", "pause", "review"}
                and actor.get("task", {}).get("id") == args.get("id")
            ):
                context.request_turn_end()
            if resource == "topic" and action == "post":
                context.request_turn_end()
            return tool_success({"text": self._tool_result(resource, action, result), **result})
        except FactoryError as error:
            return tool_failure(
                error.code,
                f"Nothing was changed. {error} "
                f"Read the current {resource} and choose an action within your assignment.",
            )

    @staticmethod
    def _tool_result(resource: str, action: str, result: Json) -> str:
        record = result.get(resource)
        if isinstance(record, dict):
            return (
                f"{resource.title()} {record['id']}: {record.get('state', 'saved')}; "
                f"revision {record['revision']}."
            )
        return f"{len(result.get(_PLURALS[resource], []))} {resource} records."

    async def _operation(self, name: str, args: Json, actor: Json | None) -> Json:
        resource, _, action = name.partition(".")
        args = dict(args)
        for key in ("capacity", "limit", "expected_revision", "priority"):
            if key in args:
                try:
                    args[key] = optional_int(
                        args[key],
                        field_name=key,
                        minimum=None if key == "priority" else 1,
                        maximum=100 if key == "limit" else None,
                    )
                except ToolContractError as error:
                    raise FactoryError("invalid_value", str(error)) from error
        if name not in OPERATION_SCHEMAS:
            raise FactoryError(
                "unknown_operation", "Choose an action from this Tool's action list."
            )
        identity = "user" if actor is None else f"member:{actor['member']['id']}"
        previous: Json | None = None
        if action in {"list", "create", "add"}:
            factory_id = str(args.get("factory_id", ""))
            if resource != "factory":
                factory = await self.store.get_factory(text(args, "factory_id"))
            else:
                factory = None
        else:
            previous = await getattr(self.store, f"get_{resource}")(text(args, "id"))
            factory_id = previous["id"] if resource == "factory" else previous["factory_id"]
            factory = (
                previous if resource == "factory" else await self.store.get_factory(factory_id)
            )
        if actor:
            if factory_id and factory_id != actor["factory"]["id"]:
                raise FactoryError(
                    "outside_factory",
                    "This record belongs to another Factory. Use records from your Factory.",
                )
            await self._authorize(resource, action, args, actor, previous, factory)
        if action == "list":
            kwargs = {
                key: args[key]
                for key in (
                    "after",
                    "limit",
                    "project_id",
                    "state",
                    "assignee_id",
                    "parent_id",
                    "task_id",
                )
                if key in args
            }
            records = await (
                self.store.list_factories(**kwargs)
                if resource == "factory"
                else getattr(self.store, f"list_{resource}s")(factory_id, **kwargs)
            )
            filters = {
                key: args[key]
                for key in ("project_id", "state", "assignee_id", "parent_id", "task_id")
                if key in args
            }
            records = [
                item
                for item in records
                if all(item.get(key) == value for key, value in filters.items())
            ]
            result: Json = {
                _PLURALS[resource]: records,
                "next_after": records[-1]["id"] if len(records) == args.get("limit", 100) else "",
            }
            if (
                resource == "member"
                and self.service.host is not None
                and self.service.host.catalog is not None
            ):
                catalog = await self.service.host.catalog()
                result["catalog"] = {
                    key: catalog.get(key, []) for key in ("agents", "models", "skills", "tools")
                }
            return result
        if action == "get":
            assert previous is not None
            if resource == "task":
                return await self._task_detail(previous, args)
            if resource == "topic":
                posts = await self.store.list_posts(
                    previous["id"], after=int(args.get("after", 0)), limit=args.get("limit", 100)
                )
                return {
                    "topic": previous,
                    "posts": posts,
                    "next_after": posts[-1]["sequence"]
                    if len(posts) == args.get("limit", 100)
                    else 0,
                    "sessions": await self._sessions(previous),
                }
            return {resource: previous}
        if previous is not None and name != "topic.post":
            self._revision(previous, args)
        if factory and factory.get("state") == "archived":
            raise FactoryError(
                "factory_archived", "This Factory is archived. Its records remain readable."
            )
        if resource == "factory":
            saved = await self._factory(action, args, identity, previous, actor)
        elif resource == "member":
            saved = await self._member(action, args, identity, previous, actor)
        elif resource == "task":
            saved = await self._task(action, args, identity, previous, actor)
        else:
            saved = await self._topic(action, args, identity, previous, actor)
        await self.service.native.changed(_PLURALS[resource], saved, previous)
        return {resource: saved}

    @staticmethod
    def _revision(record: Json, args: Json) -> None:
        if args.get("expected_revision") != record["revision"]:
            raise FactoryError(
                "revision_conflict",
                f"{record['id']} changed. Read its current revision before applying this action.",
            )

    async def _authorize(
        self,
        resource: str,
        action: str,
        args: Json,
        actor: Json,
        previous: Json | None,
        factory: Json | None,
    ) -> None:
        member = actor["member"]
        if member.get("state", "active") != "active":
            raise FactoryError(
                "member_retired",
                "Your membership is retired; ask the user to assign an active member.",
            )
        if action in _READ_ACTIONS:
            return
        assert factory is not None
        leader = factory.get("leader_member_id") == member["id"]
        if resource in {"factory", "member"}:
            if not leader:
                raise FactoryError(
                    "leader_required",
                    (
                        "The Leader manages Factory settings and members. Propose the change in a"
                        " topic."
                    ),
                )
            if resource == "factory" and (
                action != "update"
                or any(
                    key in args
                    for key in ("state", "limits", "config", "capacity", "remit", "clear_fields")
                )
            ):
                raise FactoryError(
                    "operator_boundary",
                    (
                        "The user sets Factory lifecycle, remit, limits and configuration "
                        "boundaries. Propose that change in a topic."
                    ),
                )
        if (
            resource == "member"
            and "identity_config" in args
            and any(
                key not in {"name", "model", "thinking_effort"} for key in args["identity_config"]
            )
        ):
            raise FactoryError(
                "configuration_boundary",
                "Global specialist edits support name, model and thinking_effort. "
                "Use local member Tool/Skill selections within the Factory ceiling.",
            )
        if resource == "task":
            if previous and any(
                key in args and args[key] != previous.get(key)
                for key in ("objective", "conditions", "reviewer_id", "user_approval")
            ):
                raise FactoryError(
                    "agreement_boundary",
                    (
                        "The user sets this task's objective and completion conditions. Propose "
                        "agreement changes in a topic."
                    ),
                )
            if "limits" in args:
                raise FactoryError(
                    "operator_boundary",
                    (
                        "The user sets task resource limits; omit limits and use the inherited "
                        "allocation."
                    ),
                )
            if (
                previous
                and "parent_id" in args
                and args["parent_id"] != previous.get("parent_id", "")
            ):
                raise FactoryError(
                    "operator_boundary",
                    "The user moves existing tasks between allocations; keep this task's parent.",
                )
            task = actor.get("task") or {}
            if task and actor.get("epoch") != task.get("epoch"):
                raise FactoryError(
                    "stale_execution",
                    (
                        "This task execution was replaced. Read the current task; do not submit "
                        "this execution's result."
                    ),
                )
            if action == "create":
                parent_id = args.get("parent_id", "")
                if not leader and parent_id:
                    parent = await self.store.get_task(parent_id)
                    if parent["assignee_id"] != member["id"]:
                        raise FactoryError(
                            "outside_remit",
                            (
                                "Create children under a task you own, or leave unrelated work "
                                "unassigned"
                                " as a backlog proposal."
                            ),
                        )
                if not leader and not parent_id and args.get("assignee_id"):
                    raise FactoryError(
                        "outside_remit",
                        (
                            "Leave unrelated work unassigned as a backlog proposal; the Leader can "
                            "authorize it."
                        ),
                    )
            elif action == "review":
                if not previous or previous.get("reviewer_id") != member["id"]:
                    raise FactoryError(
                        "reviewer_required",
                        "This result needs its configured reviewer or the user.",
                    )
            elif (
                action == "complete"
                and previous
                and (
                    previous.get("assignee_id") != member["id"] or task.get("id") != previous["id"]
                )
            ):
                raise FactoryError(
                    "owner_required",
                    (
                        "The responsible task owner submits the result; contribute evidence "
                        "through its topic."
                    ),
                )
            elif not leader and previous and previous.get("assignee_id") != member["id"]:
                parent = (
                    await self.store.get_task(previous["parent_id"])
                    if previous.get("parent_id")
                    else {}
                )
                if parent.get("assignee_id") != member["id"]:
                    raise FactoryError(
                        "outside_remit",
                        (
                            "Change your own task or its children; contribute to other tasks "
                            "through "
                            "topics."
                        ),
                    )
            if (
                actor
                and action in {"update", "complete"}
                and previous
                and task.get("id") == previous["id"]
                and actor.get("epoch") != previous["epoch"]
            ):
                raise FactoryError(
                    "stale_execution",
                    "This task was reassigned. Its current owner must continue it.",
                )

    async def _factory(
        self, action: str, args: Json, identity: str, previous: Json | None, actor: Json | None
    ) -> Json:
        if action == "create":
            self._validate_permission_ceiling(args.get("config", {}))
            project_id = text(args, "project_id")
            host = self.service.host
            assert host is not None and host.catalog is not None
            catalog = await host.catalog()
            if not any(project["id"] == project_id for project in catalog["projects"]):
                raise FactoryError("project_missing", "Choose a registered Project from catalog.")
            existing = await self.store.list_factories(project_id=project_id)
            if existing:
                if args.get("request_id") and not args.get("leader_agent_id"):
                    reservation = await self.store.reserve_identity(
                        text(args, "request_id"),
                        {key: value for key, value in args.items() if key != "expected_revision"},
                    )
                    leader_member = await self.store.get_member(existing[0]["leader_member_id"])
                    if (
                        reservation.get("proof") == leader_member.get("agent_proof")
                        and reservation["agent_id"] == leader_member["agent_id"]
                    ):
                        await self.store.complete_identity_reservation(
                            text(args, "request_id"), existing[0]["id"]
                        )
                        return existing[0]
                raise FactoryError(
                    "factory_exists",
                    "This Project already has a Factory. Read it instead of creating another.",
                )
            if args.get("leader_agent_id"):
                if args.get("leader_name") or args.get("leader_model"):
                    raise FactoryError(
                        "ambiguous_leader",
                        (
                            "Choose an existing leader_agent_id or a new leader_name and "
                            "leader_model, not both."
                        ),
                    )
                leader = await self.identities._identity(text(args, "leader_agent_id"))
            else:
                leader = await self.identities._create_identity(args, leader=True)
            boundary = args.get("config", {})
            preview = await self.identities._preview_identity(leader, project_id, boundary)
            self.identities._check_model_boundary(preview["model"], boundary.get("allowed_models"))
            settings = {key: deepcopy(args[key]) for key in FACTORY if key in args}
            project = next(
                project for project in catalog["projects"] if project["id"] == project_id
            )
            settings.setdefault(
                "name", project.get("display_name") or project.get("name") or project_id
            )
            if "limits" in settings:
                settings["limits"] = _limits.normalize(settings["limits"])
            saved = await self.store.create_factory(
                project_id,
                leader_created_by_factory=not bool(args.get("leader_agent_id")),
                leader_agent_id=leader["agent_id"],
                leader_name=leader.get("name") or leader["agent_id"],
                leader_agent_proof=leader["incarnation"],
                actor=identity,
                **settings,
            )
            if not args.get("leader_agent_id"):
                await self.store.complete_identity_reservation(
                    text(args, "request_id"), saved["id"]
                )
            return saved
        assert previous is not None
        patch = {key: deepcopy(args[key]) for key in (*FACTORY, "state") if key in args}
        if action == "archive":
            patch["state"] = "archived"
        clear_fields = args.get("clear_fields", [])
        if not isinstance(clear_fields, list) or any(
            field not in CONFIG["properties"] for field in clear_fields
        ):
            raise FactoryError(
                "invalid_field", "clear_fields must name known Factory configuration fields."
            )
        if "config" in patch or clear_fields:
            boundary = {
                key: value
                for key, value in previous.get("config", {}).items()
                if key not in clear_fields
            }
            boundary.update(patch.get("config", {}))
            self._validate_permission_ceiling(boundary)
            after = ""
            while True:
                members = await self.store.list_members(previous["id"], state="active", after=after)
                for member in members:
                    info = await self.identities._identity(member["agent_id"])
                    await self.identities._validate_effective_model(
                        info, previous["project_id"], member.get("config", {}), boundary
                    )
                if len(members) < 100:
                    break
                after = members[-1]["id"]
        if "limits" in patch:
            patch["limits"] = _limits.normalize(patch["limits"])
        return await self.store.update_factory(
            previous["id"],
            patch,
            actor=identity,
            expected_revision=previous["revision"],
            clear_config_fields=clear_fields,
        )

    @staticmethod
    def _validate_permission_ceiling(config: Json) -> None:
        if "permission_ceiling" in config:
            try:
                PermissionCeiling.from_ceiling_dict(config["permission_ceiling"])
            except PermissionRulesError as error:
                raise FactoryError(
                    "invalid_config",
                    f"Invalid permission_ceiling: {error} "
                    "Supply a valid Permission Rules ceiling or clear the override.",
                ) from error

    async def _member(
        self, action: str, args: Json, identity: str, previous: Json | None, actor: Json | None
    ) -> Json:
        if action in {"add", "create"}:
            await self._member_config(args, actor)
            info = (
                await self.identities._create_identity(args)
                if action == "create"
                else await self.identities._identity(text(args, "agent_id"))
            )
            if action == "create":
                for member in await self.store.list_members(text(args, "factory_id")):
                    if (
                        member["agent_id"] == info["agent_id"]
                        and member.get("agent_proof") == info["incarnation"]
                    ):
                        await self.store.complete_identity_reservation(
                            text(args, "request_id"), member["id"]
                        )
                        return member
            await self._validate_member_model(args, info)
            saved = await self.store.add_member(
                text(args, "factory_id"),
                info["agent_id"],
                agent_proof=info["incarnation"],
                created_by_factory=action == "create",
                name=info.get("name") or info["agent_id"],
                actor=identity,
                **{
                    key: deepcopy(args[key])
                    for key in MEMBER
                    if key in args
                    and key
                    not in {
                        "model",
                        "thinking_effort",
                        "allowed_skills",
                        "allowed_tools",
                        "denied_tools",
                    }
                },
            )
            if action == "create":
                await self.store.complete_identity_reservation(
                    text(args, "request_id"), saved["id"]
                )
            return saved
        assert previous is not None
        clear_fields = args.get("clear_fields", [])
        if not isinstance(clear_fields, list) or any(field not in LOCAL for field in clear_fields):
            raise FactoryError(
                "invalid_field",
                "clear_fields supports model, thinking_effort, allowed_skills, "
                "allowed_tools and denied_tools.",
            )
        effective_previous = {
            **previous,
            "config": {
                key: value
                for key, value in previous.get("config", {}).items()
                if key not in clear_fields
            },
        }
        await self._member_config(args, actor)
        if action == "update":
            await self._validate_member_model(
                args,
                {
                    **await self.identities._identity(previous["agent_id"]),
                    **args.get("identity_config", {}),
                },
                effective_previous,
            )
        patch = {
            key: deepcopy(args[key])
            for key in MEMBER
            if key in args
            and key
            not in {"model", "thinking_effort", "allowed_skills", "allowed_tools", "denied_tools"}
        }
        if "config" in args:
            patch["config"] = {**effective_previous.get("config", {}), **args["config"]}
        if action == "retire":
            factory = await self.store.get_factory(previous["factory_id"])
            if factory["leader_member_id"] == previous["id"]:
                raise FactoryError(
                    "leader_required",
                    "Replace the Factory's Leader before retiring this membership.",
                )
            patch["state"] = "retired"
        if "identity_config" in args:
            if not previous.get("created_by_factory"):
                raise FactoryError(
                    "shared_identity",
                    (
                        "This existing Identity Agent is shared. Edit its global configuration "
                        "explicitly in Agents settings."
                    ),
                )
            host = self.service.host
            assert host is not None and host.identity_agents is not None
            await host.identity_agents.update(
                previous["agent_id"],
                expected_incarnation=previous["agent_proof"],
                **args["identity_config"],
            )
        return await self.store.update_member(
            previous["id"],
            patch,
            actor=identity,
            expected_revision=previous["revision"],
            clear_config_fields=clear_fields,
        )

    async def _member_config(self, args: Json, actor: Json | None) -> None:
        config = deepcopy(args.get("config", {}))
        for key in ("model", "thinking_effort", "allowed_skills", "allowed_tools", "denied_tools"):
            if key in args:
                config[key] = deepcopy(args[key])
        if not isinstance(config, dict):
            raise FactoryError(
                "invalid_config",
                "Supply named Model, thinking_effort, allowed_skills or tools settings.",
            )
        if actor:
            boundary = actor["factory"].get("config", {})
            allowed_models = boundary.get("allowed_models")
            if config.get("model"):
                self.identities._check_model_boundary(config["model"], allowed_models)
            allowed_skills = boundary.get("allowed_skills", ["*"])
            if "*" not in allowed_skills and any(
                skill not in allowed_skills for skill in config.get("allowed_skills", [])
            ):
                raise FactoryError(
                    "configuration_boundary",
                    "Choose Skills allowed by the Factory's configuration boundary.",
                )
            allowed_tools = boundary.get("allowed_tools")
            if allowed_tools is not None and any(
                tool not in allowed_tools for tool in config.get("allowed_tools", [])
            ):
                raise FactoryError(
                    "configuration_boundary",
                    "Choose Tools allowed by the Factory's configuration boundary.",
                )
            if any(
                key
                not in {
                    "model",
                    "thinking_effort",
                    "allowed_skills",
                    "allowed_tools",
                    "denied_tools",
                }
                for key in config
            ):
                raise FactoryError(
                    "configuration_boundary",
                    (
                        "Member settings support model, thinking_effort, allowed_skills, "
                        "allowed_tools and denied_tools."
                    ),
                )
        if config or "config" in args:
            args["config"] = config

    async def _validate_member_model(
        self, args: Json, info: Json, previous: Json | None = None
    ) -> None:
        host = self.service.host
        assert host is not None
        local = {**(previous or {}).get("config", {}), **args.get("config", {})}
        factory_id = args.get("factory_id") or (previous or {}).get("factory_id")
        factory = await self.store.get_factory(str(factory_id))
        await self.identities._validate_effective_model(
            info, factory["project_id"], local, factory.get("config", {})
        )

    async def _task(
        self, action: str, args: Json, identity: str, previous: Json | None, actor: Json | None
    ) -> Json:
        if action in {"create", "update"} and args.get("workflow_id"):
            workflow = await self.service.records().get_workflow(text(args, "workflow_id"))
            factory_id = args.get("factory_id") or (previous or {})["factory_id"]
            factory = await self.store.get_factory(factory_id)
            if not workflow["enabled"]:
                raise FactoryError(
                    "workflow_not_found",
                    "Choose an enabled Workflow or omit workflow_id for native work.",
                )
            if workflow["project_id"] and workflow["project_id"] != factory["project_id"]:
                raise FactoryError(
                    "project_mismatch",
                    f"Workflow {workflow['name']!r} always works in Project "
                    f"{workflow['project_id']!r}. Choose a Workflow for this Factory's "
                    "Project or a global Workflow.",
                )
        if action == "create":
            fields = {
                key: deepcopy(args[key])
                for key in TASK
                if key in args and key not in {"title", "objective"}
            }
            if "limits" in fields:
                fields["limits"] = _limits.normalize(fields["limits"])
            return await self.store.create_task(
                text(args, "factory_id"),
                text(args, "title", maximum=200),
                text(args, "objective"),
                actor=identity,
                **fields,
            )
        assert previous is not None
        patch: Json = {}
        result = None
        if action == "update":
            patch = {key: deepcopy(args[key]) for key in TASK if key in args}
        elif action == "assign":
            patch = {
                "assignee_id": str(args.get("assignee_id", "")),
                "state": "ready" if args.get("assignee_id") else "backlog",
                "reason": "",
            }
        elif action in {"pause", "cancel", "reopen", "resume"}:
            state = {
                "pause": "paused",
                "cancel": "cancelled",
                "reopen": "ready" if previous.get("assignee_id") else "backlog",
                "resume": "ready" if previous.get("assignee_id") else "backlog",
            }[action]
            if action == "resume" and previous["state"] not in {"paused", "blocked"}:
                raise FactoryError(
                    "wrong_state",
                    "Resume applies to paused or blocked tasks. Read the current task state.",
                )
            patch = {"state": state, "reason": ""}
            if action == "resume" and isinstance(previous.get("submission"), dict):
                patch.update(
                    state="review",
                    reason="review",
                    epoch=previous["epoch"] + 1,
                    submission={**previous["submission"], "epoch": previous["epoch"] + 1},
                )
        elif action == "complete":
            return await self.store.submit_task(
                previous["id"],
                text(args, "outcome"),
                text(args, "evidence", maximum=100000),
                actor=identity,
                expected_revision=previous["revision"],
                expected_epoch=previous["epoch"] if actor else None,
            )
        elif action == "review":
            return await self.store.review_task(
                previous["id"],
                args.get("approve") is True,
                actor=identity,
                comment=args.get("findings", ""),
                expected_revision=previous["revision"],
                expected_epoch=previous["epoch"] if actor else None,
                reviewer_id=actor["member"]["id"] if actor else "",
                user=actor is None,
            )
        if "limits" in patch:
            patch["limits"] = _limits.normalize(patch["limits"])
        if action in {"assign", "pause", "cancel", "reopen"}:
            patch["epoch"] = previous["epoch"] + 1
        return await self.store.change_task(
            previous["id"],
            patch,
            actor=identity,
            event=action,
            expected_revision=previous["revision"],
            expected_epoch=previous["epoch"]
            if actor and actor.get("task", {}).get("id") == previous["id"]
            else None,
            result=result,
        )

    async def _topic(
        self, action: str, args: Json, identity: str, previous: Json | None, actor: Json | None
    ) -> Json:
        if action == "create":
            members = list(args.get("member_ids", []))
            if actor and actor["member"]["id"] not in members:
                members.append(actor["member"]["id"])
            topic = await self.store.create_topic(
                text(args, "factory_id"),
                text(args, "title", maximum=200),
                task_id=args.get("task_id", ""),
                participants=members,
                actor=identity,
                initial_text=args.get("text", ""),
            )
            return await self.store.get_topic(topic["id"])
        assert previous is not None
        if action == "invite":
            return await self.store.invite_topic(
                previous["id"],
                args.get("member_ids", []),
                actor=identity,
                expected_revision=previous["revision"],
                text=args.get("text", ""),
            )
        if action == "post":
            await self.store.add_post(
                previous["id"],
                text(args, "text"),
                author=identity,
                actor=identity,
                recipients=args.get("recipients", []),
                receipt=args.get("source_receipt", ""),
            )
            return await self.store.get_topic(previous["id"])
        return await self.store.update_topic(
            previous["id"],
            {"state": "closed"},
            actor=identity,
            expected_revision=previous["revision"],
        )

    async def _task_detail(self, task: Json, args: Json) -> Json:
        children = await self.store.list_tasks(task["factory_id"], parent_id=task["id"])
        topics = await self.store.list_topics(task["factory_id"], task_id=task["id"])
        sessions = await self._sessions(task)
        budget = await self.service.native.limits.status_for_task(task)
        task = {
            **task,
            "budget": budget,
            "consumption": budget.get(f"task:{task['id']}", {}).get("usage", {}),
        }
        limit = args.get("limit", 100)
        events = await self.store.events(
            "task", task["id"], after=args.get("events_after", 0), limit=limit
        )
        results = await self.store.results(
            task["id"], after=args.get("results_after", 0), limit=limit
        )
        return {
            "task": task,
            "children": children,
            "topics": topics,
            "events": events,
            "results": results,
            "events_after": events[-1]["id"] if len(events) == limit else 0,
            "results_after": results[-1]["id"] if len(results) == limit else 0,
            "sessions": sessions,
        }

    async def _sessions(self, record: Json) -> list[Json]:
        executions = await self.store.list_executions(record["factory_id"], scope_id=record["id"])
        return [
            {
                "session": item.get("runtime", {}).get("address", {}),
                "group_id": item["group_id"],
                "participant_id": item["participant_id"],
                "kind": item["kind"],
                "state": item["state"],
            }
            for item in executions
            if item.get("runtime", {}).get("address")
        ]
