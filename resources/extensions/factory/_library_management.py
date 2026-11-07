"""Library operations behind Factory management; Project changes remain setup saves."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from core.utils.logging import get_logger

from . import _library
from ._values import text
from .store import FactoryError

if TYPE_CHECKING:
    from .extension import FactoryExtension

type Json = dict[str, Any]
_LOGGER = get_logger("extensions.factory")


def _workflow(arguments: Json) -> Json:
    value = arguments.get("workflow")
    if not isinstance(value, dict):
        raise FactoryError("invalid_workflow", "Supply workflow as a Workflow object.")
    return value


def _revision(arguments: Json, *, creating: bool = False) -> int:
    value = arguments.get("expected_revision", 0 if creating else None)
    if type(value) is not int or value < 0:
        raise FactoryError(
            "revision_required",
            "Supply expected_revision from library.get before changing an existing template.",
        )
    return value


def _description(arguments: Json) -> str:
    value = arguments.get("description", "")
    if not isinstance(value, str) or len(value) > 2000:
        raise FactoryError(
            "invalid_description", "Supply description as text of at most 2000 characters."
        )
    return value


def _changed(service: FactoryExtension, template: Json, action: str) -> None:
    host = service.host
    if host is not None and host.publish_change is not None:
        host.publish_change("library", [template["id"]], template["revision"])
    _LOGGER.info(
        "Factory template %s (template=%s actor=user version=%d revision=%d)",
        action,
        template["id"],
        template["version"],
        template["revision"],
    )


async def operation(service: FactoryExtension, name: str, arguments: Json) -> Json:
    store = service.records()
    if name == "library.list":
        return await store.list_templates(
            include_archived=arguments.get("include_archived", False),
            after=arguments.get("after", ""),
            limit=arguments.get("limit", 50),
        )
    if name == "library.get":
        return {
            "template": await store.get_template(text(arguments, "id"), arguments.get("version"))
        }
    if name == "library.publish":
        kind = text(arguments, "kind")
        draft = _workflow(arguments)
        assert draft is not None
        workflow = _library.normalize_template(kind, draft)
        title = text(arguments, "name", maximum=80).strip()
        description = _description(arguments)
        workflow.update(name=title, description=description)
        values = {"kind": kind, "name": title, "description": description, "workflow": workflow}
        if "id" in arguments:
            values["id"] = text(arguments, "id")
        async with service.lock:
            saved = await store.publish_template(
                values, expected_revision=_revision(arguments, creating="id" not in values)
            )
            _changed(service, saved, "published")
        return {"template": saved}
    if name == "library.archive":
        async with service.lock:
            template_id = text(arguments, "id")
            previous = await store.get_template(template_id)
            saved = await store.archive_template(
                template_id,
                expected_revision=_revision(arguments),
                archived=arguments.get("archived", True),
            )
            if saved["revision"] != previous["revision"]:
                _changed(service, saved, "archived" if saved["archived"] else "restored")
        return {"template": saved}
    if name == "library.extract":
        workflow = _workflow(arguments)
        assert workflow is not None
        return {
            "workflow": _library.extract(
                workflow,
                arguments.get("step_ids"),
                source_id=arguments.get("source_id"),
            )
        }
    if name in {"library.insert", "library.link"}:
        template = await store.get_template(text(arguments, "id"), arguments.get("version"))
        if template["archived"]:
            raise FactoryError(
                "template_archived", "The template is archived. Restore it before inserting it."
            )
        if name == "library.link":
            workflow = _workflow(arguments)
            assert workflow is not None
            return {
                "workflow": _library.link(
                    workflow,
                    template,
                    step_ids=arguments.get("step_ids"),
                    replace_sources=arguments.get("replace_sources", False),
                )
            }
        return {
            "workflow": _library.insert(
                _workflow(arguments) if "workflow" in arguments else None,
                template,
                after=arguments.get("after"),
            )
        }
    if name in {"library.preview_update", "library.apply_update"}:
        workflow = _workflow(arguments)
        assert workflow is not None
        source_id = text(arguments, "source_id")
        source = next(
            (
                item
                for item in workflow.get("sources", [])
                if isinstance(item, dict) and item.get("id") == source_id
            ),
            None,
        )
        if source is None:
            raise FactoryError(
                "source_not_found", "The Workflow has no such source. Choose one of its sources."
            )
        template = await store.get_template(source["template_id"], arguments.get("version"))
        result = _library.preview_update(
            workflow, source_id, template, resolutions=arguments.get("resolutions")
        )
        result.update(template_id=template["id"], version=template["version"])
        if name == "library.apply_update":
            if "version" not in arguments:
                raise FactoryError(
                    "version_required",
                    "Supply the exact version returned by library.preview_update.",
                )
            if any(not item["resolution"] for item in result["conflicts"]):
                raise FactoryError(
                    "update_conflicts",
                    "Resolve every conflict shown by library.preview_update before applying.",
                )
            if not result["valid"]:
                raise FactoryError(
                    "invalid_workflow",
                    "The updated Workflow is invalid: " + "; ".join(result["problems"]),
                )
        return result
    raise FactoryError(
        "unknown_operation", "Unknown library operation. Read the operation catalog."
    )
