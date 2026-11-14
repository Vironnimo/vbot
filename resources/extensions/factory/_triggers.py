"""Workflow triggers: the host's Extension triggers that create Factory Cards.

Each trigger of a saved Workflow is one host trigger keyed
``<workflow id>:<trigger id>``; Cron owns its schedule, webhook URL, secret and
catch-up. A fire creates one Card per webhook request, or one Card per
scheduled fire, with the event condensed so the Agents can read it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.utils.logging import get_logger

from . import _workflows
from .store import FactoryError

if TYPE_CHECKING:
    from .extension import FactoryExtension

type Json = dict[str, Any]
_LOGGER = get_logger("extensions.factory")
_MAX_CARDS_PER_FIRE = 20
_MAX_COMMITS = 20
_TEXT_LIMIT = 4000
_REPOSITORY = re.compile(
    r'"repository"\s*:\s*\{[^{}]*?"full_name"\s*:\s*"([^"/\s]+/[^"\s]+)"', re.DOTALL
)
_REMOTE = re.compile(r"^\s*url\s*=\s*(\S+)\s*$", re.MULTILINE)


def trigger_key(workflow_id: str, trigger_id: str) -> str:
    return f"{workflow_id}:{trigger_id}"


def repository_key(url: str) -> str:
    """``host/owner/name`` of a Git remote or repository URL, lowercase; empty if unknown."""
    value = url.strip()
    if not value:
        return ""
    if match := re.match(r"^[\w.-]+@([\w.-]+):(.+)$", value):
        host, path = match.groups()
    else:
        value = re.sub(r"^[a-z+]+://", "", value, flags=re.IGNORECASE)
        value = value.rpartition("@")[2] if "@" in value.partition("/")[0] else value
        host, _, path = value.partition("/")
        host = host.partition(":")[0]
    path = path.strip("/").removesuffix(".git")
    if not host or path.count("/") < 1:
        return ""
    return f"{host}/{path}".casefold()


def _short(value: Any, limit: int = _TEXT_LIMIT) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + " …"
    return value


def _pick(value: Any, *fields: str) -> Json:
    if not isinstance(value, dict):
        return {}
    return {field: _short(value[field]) for field in fields if value.get(field) not in (None, "")}


def condense(headers: Json, body: Any) -> Json:
    """The parts of a webhook request an Agent needs, GitHub-aware; raw text is cut."""
    event = next(
        (
            str(value)
            for name, value in headers.items()
            if name.startswith("x-") and name.endswith("-event")
        ),
        "",
    )
    result: Json = {"event": event} if event else {}
    if not isinstance(body, dict):
        if body not in (None, ""):
            text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
            result["body"] = _short(text)
        return result
    repository = body.get("repository")
    if isinstance(repository, dict):
        result["repository"] = _pick(
            repository, "full_name", "html_url", "clone_url", "default_branch"
        )
    for field in ("action", "ref", "before", "after", "compare", "base_ref"):
        if isinstance(body.get(field), (str, int)) and body[field] != "":
            result[field] = _short(body[field])
    if isinstance(body.get("pusher"), dict):
        result["pusher"] = _pick(body["pusher"], "name")
    if isinstance(body.get("sender"), dict):
        result["sender"] = _pick(body["sender"], "login")
    commits = body.get("commits")
    if isinstance(commits, list):
        result["commits"] = [
            _pick(commit, "id", "message", "url") for commit in commits[:_MAX_COMMITS]
        ]
        if len(commits) > _MAX_COMMITS:
            result["more_commits"] = len(commits) - _MAX_COMMITS
    for field in ("pull_request", "issue"):
        if isinstance(body.get(field), dict):
            item = body[field]
            result[field] = _pick(item, "number", "title", "html_url", "state", "body") | {
                key: item[key]["ref"]
                for key in ("head", "base")
                if isinstance(item.get(key), dict) and isinstance(item[key].get("ref"), str)
            }
    if isinstance(body.get("comment"), dict):
        result["comment"] = _pick(body["comment"], "body", "html_url")
    if isinstance(body.get("release"), dict):
        result["release"] = _pick(body["release"], "tag_name", "name", "html_url")
    if len(result) <= (1 if event else 0):
        # Not a payload this knows: hand over the JSON itself, cut.
        result["body"] = _short(json.dumps(body, ensure_ascii=False))
    return result


def repository_of(event: Json, raw: Any) -> str:
    """The repository a webhook request is about, as a :func:`repository_key`."""
    repository = event.get("repository") or {}
    for field in ("clone_url", "html_url"):
        if key := repository_key(str(repository.get(field) or "")):
            return key
    if full_name := repository.get("full_name"):
        return f"github.com/{full_name}".casefold()
    if isinstance(raw, str) and (match := _REPOSITORY.search(raw)):
        return f"github.com/{match.group(1)}".casefold()
    return ""


def project_repositories(cwd: str) -> set[str]:
    """The repositories a Project's folder points at through its Git remotes."""
    config = Path(cwd) / ".git" / "config"
    try:
        text = config.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set()
    return {key for url in _REMOTE.findall(text) if (key := repository_key(url))}


def _title(trigger: Json, workflow: Json, event: Json) -> str:
    title = trigger["title"].strip() or workflow["name"]
    details = []
    if name := (event.get("repository") or {}).get("full_name"):
        details.append(str(name))
    if isinstance(event.get("ref"), str):
        details.append(event["ref"].removeprefix("refs/heads/"))
    if isinstance(event.get("after"), str) and event["after"]:
        details.append(event["after"][:7])
    for field in ("pull_request", "issue"):
        if item := event.get(field):
            details.append(f"#{item.get('number', '')} {item.get('title', '')}".strip())
    if details:
        title = f"{title}: {' '.join(details)}"
    return " ".join(title.split())[:200]


class FactoryTriggers:
    """Keeps the host's triggers equal to the saved Workflows' triggers and handles fires."""

    def __init__(self, service: FactoryExtension) -> None:
        self.service = service

    @property
    def host_triggers(self) -> Any:
        host = self.service.host
        return None if host is None else host.triggers

    def check(self, workflow: Json) -> None:
        """Refuse a Workflow whose schedules Cron cannot run."""
        triggers = self.host_triggers
        for trigger in workflow["triggers"]:
            if trigger["type"] != "schedule":
                continue
            if triggers is None:
                raise FactoryError(
                    "triggers_unavailable",
                    "Triggers are unavailable because scheduling is off. "
                    "Remove the trigger or enable scheduling.",
                )
            try:
                triggers.check_schedule(trigger["schedule"])
            except ValueError as error:
                raise FactoryError(
                    "invalid_schedule",
                    f"Workflow {workflow['name']!r}, trigger {trigger['title'] or trigger['id']!r}:"
                    f" {str(error).rstrip('.')}.",
                ) from error
        if triggers is None and workflow["triggers"]:
            raise FactoryError(
                "triggers_unavailable",
                "Triggers are unavailable because scheduling is off. "
                "Remove the trigger or enable scheduling.",
            )

    async def sync(self, workflows: list[Json] | None = None) -> None:
        """Create, update and delete host triggers to match the saved Workflows."""
        triggers = self.host_triggers
        if triggers is None:
            return
        if workflows is None:
            workflows = await self.service.records().list_workflows()
        wanted: dict[str, Json] = {}
        for workflow in workflows:
            for trigger in workflow["triggers"]:
                wanted[trigger_key(workflow["id"], trigger["id"])] = {
                    "name": f"{workflow['name']}: {trigger['title'] or trigger['type']}"[:200],
                    "schedule": "webhook" if trigger["type"] == "webhook" else trigger["schedule"],
                    "enabled": bool(workflow["enabled"] and trigger["enabled"]),
                }
        existing = {item["key"]: item for item in triggers.list()}
        for key, item in existing.items():
            if key not in wanted:
                await triggers.delete(item["id"])
        for key, values in wanted.items():
            current = existing.get(key)
            try:
                if current is None:
                    await triggers.create(key, **values)
                elif current["schedule_type"] == "webhook" and values["schedule"] != "webhook":
                    # A webhook trigger keeps its URL; a changed type gets a new trigger.
                    await triggers.delete(current["id"])
                    await triggers.create(key, **values)
                elif (
                    current["name"],
                    current["enabled"],
                    "webhook" if current["schedule_type"] == "webhook" else current["schedule"],
                ) != (values["name"], values["enabled"], values["schedule"]):
                    await triggers.update(current["id"], **values)
            except ValueError, LookupError:
                _LOGGER.warning("Factory trigger could not be synced (trigger=%s)", key)

    def status(self, workflow_id: str) -> dict[str, Json]:
        """The host state of a Workflow's triggers by trigger id."""
        triggers = self.host_triggers
        if triggers is None:
            return {}
        prefix = f"{workflow_id}:"
        return {
            item["key"].removeprefix(prefix): item
            for item in triggers.list()
            if item["key"].startswith(prefix)
        }

    async def secret(self, workflow_id: str, trigger_id: str, *, rotate: bool) -> str:
        triggers = self.host_triggers
        found = self.status(workflow_id).get(trigger_id)
        if triggers is None or found is None or found["webhook"] is None:
            raise FactoryError(
                "trigger_not_found",
                "That webhook trigger does not exist. Save the Workflow with the trigger first.",
            )
        if rotate:
            return str(await triggers.rotate_webhook_secret(found["id"]))
        return str(await triggers.webhook_secret(found["id"]))

    async def resolve_project(
        self, choice: str, workflow: Json, repository: str
    ) -> tuple[str, str]:
        """(project id, problem) for a triggered Card; the Workflow's own Project wins."""
        if workflow["project_id"]:
            return workflow["project_id"], ""
        if choice != _workflows.REPOSITORY_PROJECT:
            return choice, ""
        if not repository:
            return "", "The trigger's request names no repository to find the Project by."
        host = self.service.host
        assert host is not None and host.catalog is not None
        for project in (await host.catalog())["projects"]:
            if repository in project_repositories(project.get("cwd", "")):
                return str(project["id"]), ""
        return "", (
            f"No Project's Git remote points at {repository}. Add the repository as a Project, "
            "or set the Project on the trigger; then retry the Card."
        )

    async def fire(self, fire: Json) -> None:
        """Create the Cards for one trigger fire; raising lets Cron retry its requests."""
        workflow_id, _, trigger_id = str(fire.get("key", "")).partition(":")
        service = self.service
        async with service.lock:
            store = service.records()
            try:
                workflow = await store.get_workflow(workflow_id)
            except FactoryError:
                _LOGGER.warning(
                    "Factory trigger fired for a deleted Workflow (key=%s)", fire["key"]
                )
                return
            trigger = next(
                (item for item in workflow["triggers"] if item["id"] == trigger_id), None
            )
            if trigger is None or not workflow["enabled"]:
                return
            requests = fire.get("requests") or []
            base = {
                "workflow_id": workflow_id,
                "trigger": trigger_id,
                "type": trigger["type"],
                "fired_at": fire.get("fired_at"),
            }
            events: list[tuple[Json, str]] = []
            if trigger["type"] == "webhook":
                for request in requests[-_MAX_CARDS_PER_FIRE:]:
                    condensed = condense(request.get("headers") or {}, request.get("body"))
                    events.append(
                        (
                            base
                            | {
                                "received_at": request.get("received_at"),
                                "request": condensed,
                                **({"cut": True} if request.get("cut") else {}),
                            },
                            repository_of(condensed, request.get("body")),
                        )
                    )
                skipped = len(requests) - len(events) + int(fire.get("dropped") or 0)
                if skipped and events:
                    events[0][0]["skipped_requests"] = skipped
            else:
                events.append(
                    (
                        base
                        | {
                            "due_at": fire.get("due_at"),
                            **(
                                {"missed": fire["earlier_missed"]}
                                if fire.get("earlier_missed")
                                else {}
                            ),
                        },
                        "",
                    )
                )
            for event, repository in events:
                project_id, _problem = await self.resolve_project(
                    trigger["project"], workflow, repository
                )
                await service.create_triggered(
                    workflow,
                    trigger,
                    event,
                    title=_title(trigger, workflow, event.get("request") or {}),
                    project_id=project_id,
                    repository=repository
                    if trigger["project"] == _workflows.REPOSITORY_PROJECT
                    else "",
                )
            _LOGGER.info(
                "Factory trigger fired (workflow=%s trigger=%s cards=%d)",
                workflow_id,
                trigger_id,
                len(events),
            )
