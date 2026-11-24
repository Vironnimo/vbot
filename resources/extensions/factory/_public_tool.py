"""Public cross-Factory CRUD Tool (``factory``) for managing Factories, Specialists, and Jobs."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

from core.tools import ToolContext, tool_failure, tool_success
from core.tools.arguments import optional_bool, optional_int, optional_string, required_string
from core.tools.contracts import ToolContractError
from core.utils.ids import new_id

from .store import FactoryError

if TYPE_CHECKING:
    from .extension import FactoryExtension

type Json = dict[str, Any]

PUBLIC_TOOL_NAME = "factory"

PUBLIC_DESCRIPTION = (
    "Manage Software Factories, their specialist Agents, and their Jobs across all Projects, "
    "including creating a Factory or copying Jobs and Specialists between Projects."
)

_FACTORY_ACTIONS = (
    "list_factories",
    "get_factory",
    "create_factory",
    "update_factory",
    "archive_factory",
)
_MEMBER_ACTIONS = (
    "list_members",
    "create_member",
    "update_member",
    "retire_member",
)
_JOB_ACTIONS = (
    "list_jobs",
    "get_job",
    "create_job",
    "update_job",
    "delete_job",
    "trigger_job",
    "list_runs",
)
_ALL_ACTIONS = (*_FACTORY_ACTIONS, *_MEMBER_ACTIONS, *_JOB_ACTIONS)
_VALID_ACTIONS = frozenset(_ALL_ACTIONS)

PUBLIC_PARAMETERS: Json = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": list(_ALL_ACTIONS),
            "description": (
                "update_factory, update_member, and update_job change only the fields you send. "
                "trigger_job starts an immediate job run. list_runs lists recent job runs."
            ),
        },
        "factory_id": {
            "type": "string",
            "description": (
                "Factory id from list_factories or create_factory. Identifies the Factory on "
                "read, member, job, update, and archive actions when project_id is omitted."
            ),
        },
        "project_id": {
            "type": "string",
            "description": (
                "Required for create_factory. Also identifies a Project's Factory when "
                "factory_id is omitted, or filters list_factories."
            ),
        },
        "member_id": {
            "type": "string",
            "description": (
                "Required for update_member and retire_member. For trigger_job: member id, "
                "role, or name to run this execution instead of the job default."
            ),
        },
        "job_id": {
            "type": "string",
            "description": (
                "Required for update_job; identifies the job for get_job, delete_job, "
                "trigger_job, or filters list_runs."
            ),
        },
        "name": {
            "type": "string",
            "description": (
                "Required for create_member and create_job. Optional display name for "
                "create_factory or update_factory; can identify a job for get_job, delete_job, "
                "or trigger_job when job_id is omitted."
            ),
        },
        "role": {
            "type": "string",
            "description": "For create_member or update_member: specialist role label.",
        },
        "instructions": {
            "type": "string",
            "description": (
                "For create_member or update_member: standing instructions for the specialist."
            ),
        },
        "model": {
            "type": "string",
            "description": (
                "For create_member: model id for the new specialist. Omit for the default model."
            ),
        },
        "prompt": {
            "type": "string",
            "description": "Required for create_job. Instructions executed when the job runs.",
        },
        "description": {
            "type": "string",
            "description": "For create_job or update_job: short summary of when to use the job.",
        },
        "agent_member_id": {
            "type": "string",
            "description": (
                "For create_job or update_job: Factory member id assigned to run this job. "
                "Omit to use the Factory Leader."
            ),
        },
        "triggers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "type": {"type": "string", "enum": ["schedule", "webhook"]},
                    "title": {"type": "string"},
                    "schedule": {"type": "string"},
                    "enabled": {"type": "boolean"},
                },
            },
            "description": (
                "For create_job or update_job: schedule (cron expression) or webhook triggers. "
                "Omit for no triggers."
            ),
        },
        "enabled": {
            "type": "boolean",
            "description": (
                "For create_job or update_job: whether the job and its triggers are active. "
                "Omit for true on create_job."
            ),
        },
        "input_note": {
            "type": "string",
            "description": "For trigger_job: extra context passed to this run. Omit if none.",
        },
        "remit": {
            "type": "string",
            "description": (
                "For create_factory or update_factory: scope and purpose of the Factory."
            ),
        },
        "leader_agent_id": {
            "type": "string",
            "description": (
                "For create_factory: existing personal Agent id to link as Leader. "
                "Omit to create a new Leader."
            ),
        },
        "leader_name": {
            "type": "string",
            "description": (
                "For create_factory: name for a new Leader when leader_agent_id is omitted."
            ),
        },
        "leader_model": {
            "type": "string",
            "description": (
                "For create_factory: model id for a newly created Leader when leader_agent_id "
                "is omitted."
            ),
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": 100,
            "description": (
                "For list_factories, list_members, or list_runs: maximum records to return (1-100)."
            ),
        },
    },
    "required": ["action"],
    "additionalProperties": False,
}


class FactoryPublicTool:
    """Handler for the public cross-Factory ``factory`` Tool."""

    def __init__(self, service: FactoryExtension) -> None:
        self.service = service

    async def handle(self, context: ToolContext, arguments: Json) -> Json:
        action = str(arguments.get("action") or "").strip().lower()
        if action not in _VALID_ACTIONS:
            valid = ", ".join(_ALL_ACTIONS)
            if not action:
                return tool_failure(
                    "invalid_arguments", f"action is required. Use one of: {valid}."
                )
            return tool_failure(
                "invalid_arguments",
                f"Unknown action {action!r}. Use one of: {valid}.",
            )

        try:
            return await self._dispatch(context, action, arguments)
        except ToolContractError as error:
            return tool_failure("invalid_arguments", f"Nothing was changed. {error}")
        except FactoryError as error:
            return tool_failure(error.code, f"Nothing was changed. {error}")

    async def _dispatch(self, context: ToolContext, action: str, arguments: Json) -> Json:
        if action in _FACTORY_ACTIONS:
            return await self._handle_factory_action(context, action, arguments)
        if action in _MEMBER_ACTIONS:
            return await self._handle_member_action(context, action, arguments)
        return await self._handle_job_action(action, arguments)

    async def _handle_factory_action(
        self, context: ToolContext, action: str, arguments: Json
    ) -> Json:
        management = self.service.native_management

        if action == "list_factories":
            op_args: Json = {}
            if project_id := optional_string(arguments.get("project_id"), field_name="project_id"):
                op_args["project_id"] = project_id
            if (
                limit := optional_int(
                    arguments.get("limit"), field_name="limit", minimum=1, maximum=100
                )
            ) is not None:
                op_args["limit"] = limit
            result = await management.operation("factory.list", op_args)
            factories = result["factories"]
            return tool_success(
                {
                    "text": f"{len(factories)} factory record(s).",
                    **result,
                }
            )

        if action == "get_factory":
            factory = await self._resolve_factory(arguments)
            result = await management.operation("factory.get", {"id": factory["id"]})
            detail = result["factory"]
            return tool_success(
                {
                    "text": (
                        f"Factory {detail['id']} ({detail['name']}, "
                        f"project {detail['project_id']}): {detail['state']}."
                    ),
                    "factory": detail,
                }
            )

        if action == "create_factory":
            project_id = required_string(arguments.get("project_id"), field_name="project_id")
            op_args = {"project_id": project_id}
            name = optional_string(arguments.get("name"), field_name="name")
            if name:
                op_args["name"] = name
            remit = optional_string(arguments.get("remit"), field_name="remit")
            if remit is not None:
                op_args["remit"] = remit
            leader_agent_id = optional_string(
                arguments.get("leader_agent_id"), field_name="leader_agent_id"
            )
            leader_name = optional_string(arguments.get("leader_name"), field_name="leader_name")
            leader_model = optional_string(arguments.get("leader_model"), field_name="leader_model")
            if leader_agent_id:
                op_args["leader_agent_id"] = leader_agent_id
                if leader_name:
                    op_args["leader_name"] = leader_name
                if leader_model:
                    op_args["leader_model"] = leader_model
            else:
                op_args["request_id"] = self._request_id(context)
                op_args["leader_name"] = leader_name or (f"{name} Leader" if name else "Leader")
                if leader_model:
                    op_args["leader_model"] = leader_model
            result = await management.operation("factory.create", op_args)
            created = result["factory"]
            return tool_success(
                {
                    "text": (
                        f"Created factory {created['id']} ({created['name']}) "
                        f"for project {created['project_id']}."
                    ),
                    "factory": created,
                }
            )

        if action == "update_factory":
            factory = await self._resolve_factory(arguments)
            op_args = {"id": factory["id"], "expected_revision": factory["revision"]}
            if "name" in arguments and arguments.get("name") is not None:
                op_args["name"] = required_string(arguments.get("name"), field_name="name")
            if "remit" in arguments and arguments.get("remit") is not None:
                op_args["remit"] = optional_string(arguments.get("remit"), field_name="remit") or ""
            result = await management.operation("factory.update", op_args)
            updated = result["factory"]
            return tool_success(
                {
                    "text": f"Updated factory {updated['id']} ({updated['name']}).",
                    "factory": updated,
                }
            )

        # archive_factory
        factory = await self._resolve_factory(arguments)
        result = await management.operation(
            "factory.archive",
            {"id": factory["id"], "expected_revision": factory["revision"]},
        )
        archived = result["factory"]
        return tool_success(
            {
                "text": f"Archived factory {archived['id']} ({archived['name']}).",
                "factory": archived,
            }
        )

    async def _handle_member_action(
        self, context: ToolContext, action: str, arguments: Json
    ) -> Json:
        management = self.service.native_management

        if action == "list_members":
            factory = await self._resolve_factory(arguments)
            op_args: Json = {"factory_id": factory["id"]}
            if (
                limit := optional_int(
                    arguments.get("limit"), field_name="limit", minimum=1, maximum=100
                )
            ) is not None:
                op_args["limit"] = limit
            result = await management.operation("member.list", op_args)
            members = result["members"]
            return tool_success(
                {
                    "text": f"{len(members)} member(s) in factory {factory['id']}.",
                    "factory_id": factory["id"],
                    "members": members,
                    "next_after": result.get("next_after", ""),
                }
            )

        if action == "create_member":
            factory = await self._resolve_factory(arguments)
            name = required_string(arguments.get("name"), field_name="name")
            op_args = {
                "factory_id": factory["id"],
                "name": name,
                "request_id": self._request_id(context),
            }
            if (role := optional_string(arguments.get("role"), field_name="role")) is not None:
                op_args["role"] = role
            if (
                instructions := optional_string(
                    arguments.get("instructions"), field_name="instructions"
                )
            ) is not None:
                op_args["instructions"] = instructions
            if model := optional_string(arguments.get("model"), field_name="model"):
                op_args["model"] = model
            result = await management.operation("member.create", op_args)
            created = result["member"]
            return tool_success(
                {
                    "text": (
                        f"Created member {created['id']} ({created['name']}) "
                        f"in factory {factory['id']}."
                    ),
                    "member": created,
                }
            )

        if action == "update_member":
            member_id = required_string(arguments.get("member_id"), field_name="member_id")
            current = await self._resolve_member_record(member_id, arguments)
            op_args = {"id": member_id, "expected_revision": current["revision"]}
            if "role" in arguments and arguments.get("role") is not None:
                op_args["role"] = optional_string(arguments.get("role"), field_name="role") or ""
            if "instructions" in arguments and arguments.get("instructions") is not None:
                op_args["instructions"] = (
                    optional_string(arguments.get("instructions"), field_name="instructions") or ""
                )
            if "model" in arguments and arguments.get("model") is not None:
                op_args["model"] = optional_string(arguments.get("model"), field_name="model") or ""
            result = await management.operation("member.update", op_args)
            updated = result["member"]
            return tool_success(
                {
                    "text": f"Updated member {updated['id']} ({updated['name']}).",
                    "member": updated,
                }
            )

        # retire_member
        member_id = required_string(arguments.get("member_id"), field_name="member_id")
        current = await self._resolve_member_record(member_id, arguments)
        result = await management.operation(
            "member.retire",
            {"id": member_id, "expected_revision": current["revision"]},
        )
        retired = result["member"]
        return tool_success(
            {
                "text": f"Retired member {retired['id']} ({retired['name']}).",
                "member": retired,
            }
        )

    async def _handle_job_action(self, action: str, arguments: Json) -> Json:
        job_triggers = getattr(self.service, "job_triggers", None)

        if action == "list_jobs":
            factory = await self._resolve_factory(arguments)
            async with self.service.lock:
                jobs = await self.service.records().jobs.list_jobs(factory["id"])
                enriched_jobs = [
                    job_triggers.enrich_job(job) if job_triggers is not None else job
                    for job in jobs
                ]
            return tool_success(
                {
                    "text": f"{len(enriched_jobs)} job(s) in factory {factory['id']}.",
                    "factory_id": factory["id"],
                    "jobs": enriched_jobs,
                }
            )

        if action == "get_job":
            async with self.service.lock:
                job = await self._resolve_job(arguments)
                enriched = job_triggers.enrich_job(job) if job_triggers is not None else job
            return tool_success({"text": f"Job {job['id']}: {job['name']}.", "job": enriched})

        if action == "create_job":
            factory = await self._resolve_factory(arguments)
            name = required_string(arguments.get("name"), field_name="name")
            prompt = required_string(arguments.get("prompt"), field_name="prompt")
            description = (
                optional_string(arguments.get("description"), field_name="description") or ""
            )
            enabled = optional_bool(arguments.get("enabled"), field_name="enabled", default=True)
            agent_member_id = (
                optional_string(arguments.get("agent_member_id"), field_name="agent_member_id")
                or ""
            )
            triggers = arguments.get("triggers")
            async with self.service.lock:
                jobs_store = self.service.records().jobs
                created = await jobs_store.create_job(
                    factory["id"],
                    name=name,
                    prompt=prompt,
                    description=description,
                    enabled=enabled,
                    agent_member_id=agent_member_id,
                    triggers=triggers,
                )
                if job_triggers is not None:
                    try:
                        job_triggers.check(created)
                    except FactoryError:
                        await jobs_store.delete_job(created["id"])
                        raise
                    await job_triggers.sync()
                    enriched = job_triggers.enrich_job(created)
                else:
                    enriched = created
                self._publish("jobs", created["id"], created["revision"])
            return tool_success(
                {"text": f"Created job {created['id']}: {created['name']}.", "job": enriched}
            )

        if action == "update_job":
            async with self.service.lock:
                previous = await self._resolve_job(arguments, allow_name=False)
                jobs_store = self.service.records().jobs
                next_name = (
                    optional_string(arguments.get("name"), field_name="name")
                    if "name" in arguments
                    else None
                )
                next_prompt = (
                    optional_string(arguments.get("prompt"), field_name="prompt")
                    if "prompt" in arguments
                    else None
                )
                next_description = (
                    optional_string(arguments.get("description"), field_name="description")
                    if "description" in arguments
                    else None
                )
                next_enabled = (
                    optional_bool(arguments.get("enabled"), field_name="enabled", default=True)
                    if "enabled" in arguments and arguments.get("enabled") is not None
                    else None
                )
                next_member_id = (
                    optional_string(arguments.get("agent_member_id"), field_name="agent_member_id")
                    if "agent_member_id" in arguments
                    else None
                )
                next_triggers = arguments.get("triggers") if "triggers" in arguments else None
                updated = await jobs_store.update_job(
                    previous["id"],
                    name=next_name,
                    prompt=next_prompt,
                    description=next_description,
                    enabled=next_enabled,
                    agent_member_id=next_member_id,
                    triggers=next_triggers,
                    expected_revision=previous["revision"],
                )
                if job_triggers is not None:
                    try:
                        job_triggers.check(updated)
                    except FactoryError:
                        await jobs_store.update_job(
                            previous["id"],
                            name=previous["name"],
                            prompt=previous["prompt"],
                            description=previous["description"],
                            enabled=previous["enabled"],
                            agent_member_id=previous["agent_member_id"],
                            triggers=previous["triggers"],
                            expected_revision=updated["revision"],
                        )
                        raise
                    await job_triggers.sync()
                    enriched = job_triggers.enrich_job(updated)
                else:
                    enriched = updated
                self._publish("jobs", updated["id"], updated["revision"])
            return tool_success(
                {"text": f"Updated job {updated['id']}: {updated['name']}.", "job": enriched}
            )

        if action == "delete_job":
            async with self.service.lock:
                job = await self._resolve_job(arguments)
                await self.service.records().jobs.delete_job(job["id"])
                if job_triggers is not None:
                    await job_triggers.sync()
                self._publish("jobs", job["id"], job["revision"] + 1)
            return tool_success(
                {
                    "text": f"Deleted job {job['id']} ({job['name']}).",
                    "deleted_job_id": job["id"],
                }
            )

        if action == "trigger_job":
            async with self.service.lock:
                job = await self._resolve_job(arguments)
                raw_member = optional_string(arguments.get("member_id"), field_name="member_id")
                member_override = (
                    await self._resolve_member_override(job["factory_id"], raw_member)
                    if raw_member
                    else None
                )
                input_note = (
                    optional_string(arguments.get("input_note"), field_name="input_note") or ""
                )
                run = await self.service.records().jobs.create_run(
                    job["factory_id"],
                    job["id"],
                    origin="public_tool",
                    member_id=member_override,
                    input_note=input_note,
                )
                self._publish("job_runs", run["id"], run["revision"])
                self.service.request_schedule()
            return tool_success(
                {"text": f"Queued job run {run['id']} for {job['name']}.", "run": run}
            )

        # list_runs
        limit = optional_int(
            arguments.get("limit"), field_name="limit", default=20, minimum=1, maximum=100
        )
        job_id = optional_string(arguments.get("job_id"), field_name="job_id")
        has_factory_hint = bool(
            optional_string(arguments.get("factory_id"), field_name="factory_id")
            or optional_string(arguments.get("project_id"), field_name="project_id")
        )
        async with self.service.lock:
            jobs_store = self.service.records().jobs
            if has_factory_hint:
                factory = await self._resolve_factory(arguments)
                factory_id = factory["id"]
                if job_id:
                    job = await jobs_store.get_job(job_id)
                    if job["factory_id"] != factory_id:
                        raise FactoryError(
                            "not_found",
                            f"Job {job_id!r} does not belong to factory {factory_id!r}.",
                        )
            elif job_id:
                job = await jobs_store.get_job(job_id)
                factory_id = job["factory_id"]
            else:
                raise FactoryError(
                    "invalid_arguments",
                    "Supply factory_id, project_id, or job_id for list_runs.",
                )
            runs = await jobs_store.list_runs(factory_id, job_id=job_id or None, limit=limit)
        return tool_success(
            {
                "text": f"{len(runs)} job run(s).",
                "factory_id": factory_id,
                "runs": runs,
            }
        )

    async def _resolve_factory(self, arguments: Json) -> Json:
        native = self.service.records().native
        factory_id = optional_string(arguments.get("factory_id"), field_name="factory_id")
        project_id = optional_string(arguments.get("project_id"), field_name="project_id")
        if factory_id:
            factory = await native.get_factory(factory_id)
            if project_id and factory["project_id"] != project_id:
                raise FactoryError(
                    "invalid_arguments",
                    f"Factory {factory_id!r} belongs to project {factory['project_id']!r}, "
                    f"not {project_id!r}.",
                )
            return factory
        if project_id:
            factories = await native.list_factories(project_id=project_id)
            if not factories:
                raise FactoryError(
                    "not_found",
                    f"No Factory exists for project {project_id!r}. "
                    "Use list_factories to see existing Factories or create_factory to create one.",
                )
            return factories[0]
        raise FactoryError("invalid_arguments", "Supply factory_id or project_id.")

    async def _resolve_member_record(self, member_id: str, arguments: Json) -> Json:
        native = self.service.records().native
        member = await native.get_member(member_id)
        has_factory_hint = bool(
            optional_string(arguments.get("factory_id"), field_name="factory_id")
            or optional_string(arguments.get("project_id"), field_name="project_id")
        )
        if has_factory_hint:
            factory = await self._resolve_factory(arguments)
            if member["factory_id"] != factory["id"]:
                raise FactoryError(
                    "not_found",
                    f"Member {member_id!r} does not belong to factory {factory['id']!r}.",
                )
        return member

    async def _resolve_job(self, arguments: Json, *, allow_name: bool = True) -> Json:
        jobs_store = self.service.records().jobs
        job_id = optional_string(arguments.get("job_id"), field_name="job_id")
        has_factory_hint = bool(
            optional_string(arguments.get("factory_id"), field_name="factory_id")
            or optional_string(arguments.get("project_id"), field_name="project_id")
        )
        if job_id:
            job = await jobs_store.get_job(job_id)
            if has_factory_hint:
                factory = await self._resolve_factory(arguments)
                if job["factory_id"] != factory["id"]:
                    raise FactoryError(
                        "not_found",
                        f"Job {job_id!r} does not belong to factory {factory['id']!r}.",
                    )
            return job
        if allow_name:
            name = optional_string(arguments.get("name"), field_name="name")
            if name:
                factory = await self._resolve_factory(arguments)
                matches = [
                    item
                    for item in await jobs_store.list_jobs(factory["id"])
                    if item["name"] == name
                ]
                if len(matches) == 1:
                    return matches[0]
                if len(matches) > 1:
                    ids = ", ".join(item["id"] for item in matches)
                    raise FactoryError(
                        "ambiguous_job",
                        f"Multiple jobs share the name {name!r} ({ids}). Pass job_id instead.",
                    )
                raise FactoryError(
                    "not_found",
                    f"No job named {name!r} exists in factory {factory['id']!r}.",
                )
        raise FactoryError(
            "invalid_arguments",
            "Supply job_id" + (" or factory_id/project_id with exact name." if allow_name else "."),
        )

    async def _resolve_member_override(self, factory_id: str, target: str) -> str:
        members = await self.service.records().native.list_members(factory_id, state="active")
        for member in members:
            if member["id"] == target:
                return str(member["id"])
        needle = target.casefold()
        matches = [
            member
            for member in members
            if str(member.get("role") or "").strip().casefold() == needle
            or str(member.get("name") or "").strip().casefold() == needle
        ]
        if len(matches) == 1:
            return str(matches[0]["id"])
        if len(matches) > 1:
            ids = ", ".join(f"{m['id']} ({m.get('role') or m.get('name')})" for m in matches)
            raise FactoryError(
                "ambiguous_member",
                f"Multiple active members match {target!r}: {ids}. Pass member_id instead.",
            )
        raise FactoryError(
            "member_unavailable",
            f"No active Factory member matches {target!r} in factory {factory_id!r}.",
        )

    @staticmethod
    def _request_id(context: ToolContext) -> str:
        if context.run_id or context.tool_call_id:
            return hashlib.sha256(f"{context.run_id}:{context.tool_call_id}".encode()).hexdigest()
        return new_id("req")

    def _publish(self, resource: str, record_id: str, revision: int) -> None:
        host = self.service.host
        if host is not None and host.publish_change is not None:
            host.publish_change(resource, [record_id], revision)
