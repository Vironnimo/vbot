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

from ._native_identities import FactoryIdentities, personal_agents
from ._native_records import _capacity, _text
from ._native_values import CONFIG, FACTORY, LOCAL, MEMBER, OPERATION_SCHEMAS
from ._values import text
from .store import FactoryError

if TYPE_CHECKING:
    from ._native_store import NativeFactoryStore
    from .extension import FactoryExtension


type Json = dict[str, Any]


_READ_ACTIONS = {"list", "get", "history"}


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
            if (
                resource == "task"
                and action == "create"
                and result.get("task", {}).get("conversation_id")
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
        if "work" in result:
            return f"{len(result['work'])} Project work items."
        if "workflow_run" in result:
            return (
                f"Workflow run {result['workflow_run']['id']}: {result['workflow_run']['state']}."
            )
        record = result.get(resource)
        if isinstance(record, dict):
            return (
                f"{resource.title()} {record['id']}: {record.get('state', 'saved')}; "
                f"revision {record['revision']}."
            )
        return f"{len(result.get(_PLURALS[resource], []))} {resource} records."

    async def _operation(self, name: str, args: Json, actor: Json | None) -> Json:
        if name == "agent.profile_targets" and actor is None:
            team = await self.store.get_factory(text(args, "scope"))
            members = await self.store.list_members(
                team["id"], state="active", after=args.get("after", "")
            )
            profiles = await self.identities.member_profiles(members)
            return {
                "agents": [
                    {
                        "id": row["agent_id"],
                        "name": row["agent_name"],
                        "placement": row["agent_placement"],
                    }
                    for row in profiles
                    if row["created_by_factory"] and row["profile_available"]
                ],
                "next_after": members[-1]["id"] if len(members) == 100 else None,
            }
        if name.startswith("conversation.") and actor is None:
            from ._conversations import conversation

            return await conversation(self, name, args)
        resource, _, action = name.partition(".")
        config = args.get("config", {})
        if not isinstance(config, dict):
            raise FactoryError("invalid_config", "Supply a configuration object.")
        if resource in {"member", "factory"} and (
            {"model", "thinking_effort"} & config.keys()
            or "identity_config" in args
            or "thinking_effort" in args
            or ("model" in args and (name != "member.create" or actor is not None))
        ):
            raise FactoryError(
                "agent_settings", "Open the team member's Agent profile to change its settings."
            )

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
            try:
                previous = await getattr(self.store, f"get_{resource}")(text(args, "id"))
            except FactoryError as error:
                if name != "task.get" or error.code != "task_not_found":
                    raise
                store = self.service.records()
                try:
                    card = await store.get_card(text(args, "id"))
                except FactoryError as card_error:
                    if card_error.code != "card_not_found":
                        raise
                    raise error from card_error
                if actor and card["project_id"] != actor["factory"]["project_id"]:
                    raise FactoryError(
                        "outside_factory", "Read work in your own Project."
                    ) from error
                return {
                    "workflow_run": {
                        key: card[key]
                        for key in ("id", "title", "state", "waiting_reason", "summary")
                    },
                    "outputs": await store.recent_outputs(card["id"]),
                }
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
                if actor is None:
                    result["members"] = await self.identities.member_profiles(records)
                catalog = await self.service.host.catalog()
                result["catalog"] = {
                    "agents": personal_agents(catalog),
                    **{key: catalog.get(key, []) for key in ("models", "skills", "tools")},
                }
            return result
        if action == "get":
            assert previous is not None
            if resource == "factory" and actor is None:
                leader = await self.store.get_member(previous["leader_member_id"])
                (profile,) = await self.identities.member_profiles([leader])
                return {
                    "factory": {
                        **previous,
                        "leader_available": leader["state"] == "active"
                        and profile["profile_available"],
                    }
                }
            return {resource: previous}
        if previous is not None:
            self._revision(previous, args)
        if factory and factory.get("state") == "archived":
            raise FactoryError(
                "factory_archived", "This Factory is archived. Its records remain readable."
            )
        if resource == "factory":
            saved = await self._factory(action, args, identity, previous, actor)
        elif resource == "member":
            saved = await self._member(action, args, identity, previous, actor)
        else:
            raise FactoryError(
                "unknown_operation", "Choose an operation from the Factory operation catalog."
            )
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
                    for key in (
                        "state",
                        "limits",
                        "config",
                        "capacity",
                        "remit",
                        "clear_fields",
                        "leader_member_id",
                    )
                )
            ):
                raise FactoryError(
                    "operator_boundary",
                    (
                        "The user sets Factory lifecycle, remit, limits and configuration "
                        "boundaries. Propose that change in a topic."
                    ),
                )
        if resource == "task":
            raise FactoryError("unknown_operation", "Factory tasks have been replaced by Jobs.")

    async def _factory(
        self, action: str, args: Json, identity: str, previous: Json | None, actor: Json | None
    ) -> Json:
        if action == "create":
            project_id = text(args, "project_id", maximum=200)
            host = self.service.host
            assert host is not None and host.catalog is not None
            catalog = await host.catalog()
            await self._validate_permission_ceiling(args.get("config", {}), catalog)
            if not any(project["id"] == project_id for project in catalog["projects"]):
                raise FactoryError("project_missing", "Choose a registered Project from catalog.")
            existing = await self.store.list_factories(project_id=project_id)
            if existing:
                if args.get("request_id") and not args.get("leader_agent_id"):
                    reservation = await self.store.identity_reservation(text(args, "request_id"))
                    leader_member = await self.store.get_member(existing[0]["leader_member_id"])
                    if reservation and (
                        reservation.get("resource_id") == existing[0]["id"]
                        or (
                            reservation.get("proof") == leader_member.get("agent_proof")
                            and reservation["agent_id"] == leader_member["agent_id"]
                        )
                    ):
                        await self.identities._create_identity(args, leader=True)
                        await self.store.complete_identity_reservation(
                            text(args, "request_id"), existing[0]["id"]
                        )
                        return existing[0]
                raise FactoryError(
                    "factory_exists",
                    "This Project already has a Factory. Read it instead of creating another.",
                )
            settings = {key: deepcopy(args[key]) for key in FACTORY if key in args}
            project = next(
                project for project in catalog["projects"] if project["id"] == project_id
            )
            settings.setdefault(
                "name", project.get("display_name") or project.get("name") or project_id
            )
            _text(settings["name"], "name", 200)
            _text(settings.get("remit", ""), "remit")
            _capacity(settings.get("capacity", 2))
            if args.get("leader_agent_id"):
                if args.get("leader_name") or args.get("leader_model"):
                    raise FactoryError(
                        "ambiguous_leader",
                        (
                            "Choose an existing leader_agent_id or a new leader_name and "
                            "leader_model, not both."
                        ),
                    )
                leader = await self.identities.for_link(
                    text(args, "leader_agent_id"), project_id=project_id
                )
            else:
                leader = await self.identities._create_identity(args, leader=True)
            boundary = args.get("config", {})
            preview = await self.identities._preview_identity(leader, project_id)
            self.identities._check_model_boundary(preview["model"], boundary.get("allowed_models"))
            saved = await self.store.create_factory(
                project_id,
                leader_created_by_factory=not args.get("leader_agent_id")
                or bool(leader.get("pending_receipt")),
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
            elif pending := leader.get("pending_receipt"):
                await self.store.complete_identity_reservation(pending["request_id"], saved["id"])
            return saved
        assert previous is not None
        patch = {key: deepcopy(args[key]) for key in (*FACTORY, "state") if key in args}
        if "leader_member_id" in args:
            current = await self.store.get_member(previous["leader_member_id"])
            candidate = await self.store.get_member(text(args, "leader_member_id"))
            if candidate["factory_id"] != previous["id"] or candidate["state"] != "active":
                raise FactoryError("outside_factory", "Choose an active member of this Factory.")
            old, new = await self.identities.member_profiles([current, candidate])
            if current["state"] == "active" and old["profile_available"]:
                raise FactoryError(
                    "leader_available", "The current Leader's Agent profile is available."
                )
            if not new["profile_available"]:
                raise FactoryError(
                    "member_profile_unavailable", "Choose a member with an available Agent profile."
                )
            patch["leader_member_id"] = candidate["id"]
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
            await self._validate_permission_ceiling(boundary)
            after = ""
            while True:
                members = await self.store.list_members(previous["id"], state="active", after=after)
                for member in members:
                    info = await self.identities._identity(member["agent_id"])
                    await self.identities._validate_effective_model(
                        info, previous["project_id"], boundary
                    )
                if len(members) < 100:
                    break
                after = members[-1]["id"]
        return await self.store.update_factory(
            previous["id"],
            patch,
            actor=identity,
            expected_revision=previous["revision"],
            clear_config_fields=clear_fields,
        )

    async def _validate_permission_ceiling(self, config: Json, catalog: Json | None = None) -> None:
        if "permission_ceiling_profile" in config:
            if catalog is None:
                host = self.service.host
                if host is None or host.catalog is None:
                    raise FactoryError(
                        "unavailable", "The permission profile catalog is unavailable."
                    )
                catalog = await host.catalog()
            if config["permission_ceiling_profile"] not in catalog.get("permission_profiles", []):
                raise FactoryError(
                    "invalid_config", "Choose an available permission profile for the Factory."
                )
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
            _text(args.get("role", ""), "role", 200)
            _text(args.get("instructions", ""), "instructions")
            _capacity(args.get("capacity", 1))
            info = (
                await self.identities._create_identity(args)
                if action == "create"
                else await self.identities.for_link(
                    text(args, "agent_id"), text(args, "factory_id")
                )
            )
            if action == "create":
                member = await self.store.identity_creation_member(
                    info["agent_id"], info["incarnation"]
                )
                if member:
                    if member["factory_id"] != text(args, "factory_id"):
                        raise FactoryError(
                            "identity_conflict", "The creation record belongs to another Factory."
                        )
                    await self.store.complete_identity_reservation(
                        text(args, "request_id"), member["id"]
                    )
                    return member
            await self._validate_member_model(args, info)
            saved = await self.store.add_member(
                text(args, "factory_id"),
                info["agent_id"],
                agent_proof=info["incarnation"],
                created_by_factory=action == "create" or bool(info.get("pending_receipt")),
                name=info.get("name") or info["agent_id"],
                actor=identity,
                **{key: deepcopy(args[key]) for key in MEMBER if key in args and key not in LOCAL},
            )
            if action == "create":
                await self.store.complete_identity_reservation(
                    text(args, "request_id"), saved["id"]
                )
            elif pending := info.get("pending_receipt"):
                await self.store.complete_identity_reservation(
                    pending["request_id"],
                    saved["id"] if pending["payload"].get("factory_id") else saved["factory_id"],
                )
            return saved
        assert previous is not None
        clear_fields = args.get("clear_fields", [])
        if not isinstance(clear_fields, list) or any(field not in LOCAL for field in clear_fields):
            raise FactoryError(
                "invalid_field",
                "clear_fields supports allowed_skills, allowed_tools and denied_tools.",
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
                await self.identities._identity(previous["agent_id"]),
                effective_previous,
            )
        patch = {key: deepcopy(args[key]) for key in MEMBER if key in args and key not in LOCAL}
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
        return await self.store.update_member(
            previous["id"],
            patch,
            actor=identity,
            expected_revision=previous["revision"],
            clear_config_fields=clear_fields,
        )

    async def _member_config(self, args: Json, actor: Json | None) -> None:
        config = deepcopy(args.get("config", {}))
        for key in LOCAL:
            if key in args:
                config[key] = deepcopy(args[key])
        if not isinstance(config, dict):
            raise FactoryError(
                "invalid_config",
                "Supply named Skill or Tool settings.",
            )
        if actor:
            boundary = actor["factory"].get("config", {})
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
            if config.keys() - LOCAL.keys():
                raise FactoryError(
                    "configuration_boundary",
                    ("Member settings support allowed_skills, allowed_tools and denied_tools."),
                )
        if config or "config" in args:
            args["config"] = config

    async def _validate_member_model(
        self, args: Json, info: Json, previous: Json | None = None
    ) -> None:
        host = self.service.host
        assert host is not None
        factory_id = args.get("factory_id") or (previous or {}).get("factory_id")
        factory = await self.store.get_factory(str(factory_id))
        await self.identities._validate_effective_model(
            info, factory["project_id"], factory.get("config", {})
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
