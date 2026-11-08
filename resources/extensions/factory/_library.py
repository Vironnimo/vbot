"""Reusable Workflow definitions and three-way updates of their Project uses.

Execution sees ordinary flat steps. A source stores the published baseline and
the identity mapping needed to compare the next version with Project edits.
This module neither persists records nor resolves Agents.
"""

from __future__ import annotations

import hashlib
import re
from copy import deepcopy
from typing import Any

from . import _workflows
from .store import FactoryError

type Json = dict[str, Any]
KINDS = ("step", "sequence", "workflow")
_ID = re.compile(r"[a-z0-9]{1,24}")
_FIELDS = (
    "name",
    "source",
    "agent",
    "model",
    "effort",
    "task",
    "after",
    "approval",
    "returns_to",
    "rounds",
)


def _error(message: str, code: str = "invalid_library") -> FactoryError:
    return FactoryError(code, message)


def normalize_template(kind: str, workflow: Any) -> Json:
    """Published templates are bounded flat Workflows without Project links."""
    if kind not in KINDS:
        raise _error("Choose template kind step, sequence or workflow.")
    if not isinstance(workflow, dict):
        raise _error("Supply a Workflow with a name and steps.")
    raw = {key: deepcopy(value) for key, value in workflow.items() if key != "sources"}
    result = _workflows.normalize([raw])[0]
    if kind == "step" and len(result["steps"]) != 1:
        raise _error("A step template contains exactly one step.")
    return result


def _selected(workflow: Json, step_ids: list[str] | None) -> list[Json]:
    steps = workflow["steps"]
    if step_ids is None:
        return deepcopy(steps)
    if (
        not isinstance(step_ids, list)
        or not step_ids
        or any(not isinstance(item, str) for item in step_ids)
        or len(set(step_ids)) != len(step_ids)
    ):
        raise _error("Select one or more different steps.")
    indexes = [index for index, step in enumerate(steps) if step["id"] in step_ids]
    if len(indexes) != len(step_ids):
        raise _error("A selected step no longer exists. Refresh the Workflow.")
    if indexes != list(range(indexes[0], indexes[-1] + 1)):
        raise _error("Select consecutive steps to save a step sequence.")
    return deepcopy(steps[indexes[0] : indexes[-1] + 1])


def _source(workflow: Json, source_id: str) -> Json:
    source: Json | None = next(
        (item for item in workflow.get("sources", []) if item["id"] == source_id), None
    )
    if source is None:
        raise _error(
            "This template use no longer exists. Refresh the Workflow.", "source_not_found"
        )
    return source


def _stable_id(seed: str, taken: set[str]) -> str:
    counter = 0
    while True:
        result = "s" + hashlib.sha256(f"{seed}:{counter}".encode()).hexdigest()[:16]
        if result not in taken:
            return result
        counter += 1


def extract(
    workflow: Json, step_ids: list[str] | None = None, *, source_id: str | None = None
) -> Json:
    """Export a whole Workflow or consecutive steps; links never nest.

    Exporting an existing use maps its Project step identities back to the library
    identities, so publishing a revision preserves the update history.
    """
    workflow = _workflows.normalize([workflow])[0]
    source = _source(workflow, source_id) if source_id else None
    selected, _mapping = _export_steps(workflow, step_ids, source)
    return _workflows.normalize([{**workflow, "steps": selected, "sources": []}])[0]


def _export_steps(
    workflow: Json, step_ids: list[str] | None, source: Json | None
) -> tuple[list[Json], dict[str, str]]:
    if source is not None and step_ids is None and not source["whole_workflow"]:
        members = set(source["step_map"].values())
        positions = [index for index, step in enumerate(workflow["steps"]) if step["id"] in members]
        if not positions:
            raise _error("All steps of this use were removed. Select the steps to publish.")
        step_ids = [step["id"] for step in workflow["steps"][positions[0] : positions[-1] + 1]]
    selected = _selected(workflow, step_ids)
    inverse = {local: original for original, local in source["step_map"].items()} if source else {}
    mapping: dict[str, str] = {}
    taken = set(inverse.values())
    for step in selected:
        local = step["id"]
        original = inverse.get(local)
        if original is None:
            original = local if local not in taken else _stable_id(local, taken)
        mapping[local] = original
        taken.add(original)
    for step in selected:
        local = step["id"]
        step["id"] = mapping[local]
        step["after"] = [mapping[item] for item in step["after"] if item in mapping]
        step["returns_to"] = mapping.get(step["returns_to"], "")
    return selected, mapping


def _material(template: Json) -> Json:
    version = template.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise _error("Choose a published template version.")
    if not isinstance(template.get("id"), str) or not template["id"]:
        raise _error("Choose a saved template.")
    material = normalize_template(template["kind"], template["workflow"])
    material["name"] = template.get("name", material["name"])
    material["description"] = template.get("description", material["description"])
    return material


def _mapped(material: Json, mapping: dict[str, str], after: list[str]) -> Json:
    value = deepcopy(material)
    for step in value["steps"]:
        step["id"] = mapping[step["id"]]
        step["after"] = [mapping[item] for item in step["after"]] if step["after"] else list(after)
        step["returns_to"] = mapping.get(step["returns_to"], "")
    return {key: value.get(key, {}) for key in ("name", "description", "steps", "limits")}


def _terminals(steps: list[Json]) -> list[str]:
    parents = {item for step in steps for item in step["after"]}
    return [step["id"] for step in steps if step["id"] not in parents]


def _new_source(
    template: Json,
    material: Json,
    mapping: dict[str, str],
    *,
    after: list[str],
    whole: bool,
    position: int,
) -> Json:
    return {
        "id": _workflows._new_id("l"),
        "template_id": template["id"],
        "version": template["version"],
        "step_map": mapping,
        "base": _mapped(material, mapping, after),
        "whole_workflow": whole,
        "after": list(after),
        "position": position,
        "connections": {},
    }


def insert(workflow: Json | None, template: Json, *, after: list[str] | None = None) -> Json:
    """Copy a published version into a Project and retain its update baseline."""
    material = _material(template)
    whole = workflow is None
    result = (
        {
            "id": _workflows._new_id("w"),
            "name": material["name"],
            "description": material["description"],
            "limits": deepcopy(material.get("limits", {})),
            "steps": [],
        }
        if whole
        else _workflows.normalize([workflow])[0]
    )
    existing = result["steps"]
    by_id = {step["id"]: index for index, step in enumerate(existing)}
    if after is not None and (
        not isinstance(after, list) or any(not isinstance(item, str) for item in after)
    ):
        raise _error("Choose existing steps after which to insert the template.")
    anchors = _terminals(existing) if after is None else list(after)
    if len(set(anchors)) != len(anchors) or any(item not in by_id for item in anchors):
        raise _error("Choose existing steps after which to insert the template.")
    position = max((by_id[item] + 1 for item in anchors), default=0)
    taken = set(by_id)
    mapping: dict[str, str] = {}
    for step in material["steps"]:
        local = _stable_id(_workflows._new_id("l") + step["id"], taken)
        mapping[step["id"]] = local
        taken.add(local)
    source = _new_source(template, material, mapping, after=anchors, whole=whole, position=position)
    inserted = deepcopy(source["base"]["steps"])
    names = {step["name"].casefold() for step in existing}
    for step in inserted:
        name = step["name"]
        suffix = 2
        while step["name"].casefold() in names:
            step["name"] = f"{name[:70]} {suffix}"
            suffix += 1
        names.add(step["name"].casefold())
    exits = _terminals(inserted)
    rewired: set[str] = set()
    for step in existing[position:]:
        if (anchors and set(step["after"]) & set(anchors)) or (not anchors and not step["after"]):
            step["after"] = list(
                dict.fromkeys([item for item in step["after"] if item not in anchors] + exits)
            )
            source["connections"][step["id"]] = list(step["after"])
            rewired.add(step["id"])
    for previous in result.get("sources", []):
        previous_exits = set(_terminals(previous["base"]["steps"]))
        for step in existing:
            if step["id"] in rewired and step["id"] in previous["connections"]:
                if previous_exits & set(step["after"]):
                    previous["connections"][step["id"]] = list(step["after"])
                else:
                    previous["connections"].pop(step["id"])
        for step in inserted:
            if previous_exits & set(step["after"]):
                previous["connections"][step["id"]] = list(step["after"])
    result["steps"] = existing[:position] + inserted + existing[position:]
    result.setdefault("sources", []).append(source)
    return _workflows.normalize([result])[0]


def link(
    workflow: Json,
    template: Json,
    *,
    step_ids: list[str] | None = None,
    replace_sources: bool = False,
) -> Json:
    """Associate an existing exported selection with its just-published template."""
    result = _workflows.normalize([workflow])[0]
    selected = _selected(result, step_ids)
    material = _material(template)
    exported = extract(result, step_ids)
    if exported["steps"] != material["steps"]:
        raise _error("The selected steps differ from this template. Publish them before linking.")
    members = {step["id"] for step in selected}
    surviving = {step["id"] for step in result["steps"]}
    retained_sources = []
    for previous in result.get("sources", []):
        previous_members = set(previous["step_map"].values()) & surviving
        if not members & previous_members:
            retained_sources.append(previous)
        elif not replace_sources:
            raise _error(
                "These steps already use a template. Explicitly replace their existing uses.",
                "source_overlap",
            )
        elif not previous_members <= members:
            raise _error(
                "The selection contains only part of an existing template use. "
                "Select all its remaining steps before replacing it.",
                "source_overlap",
            )
    result["sources"] = retained_sources
    after = list(
        dict.fromkeys(item for step in selected for item in step["after"] if item not in members)
    )
    position = next(
        index for index, step in enumerate(result["steps"]) if step["id"] == selected[0]["id"]
    )
    source = _new_source(
        template,
        material,
        {step["id"]: step["id"] for step in selected},
        after=after,
        whole=step_ids is None,
        position=position,
    )
    # Existing boundary choices belong to the Project, so they become local
    # overrides against the exported baseline rather than changing the template.
    exits = set(_terminals(selected))
    source["connections"] = {
        step["id"]: list(step["after"])
        for step in result["steps"]
        if step["id"] not in members and exits & set(step["after"])
    }
    result.setdefault("sources", []).append(source)
    return _workflows.normalize([result])[0]


def normalize_sources(sources: Any, steps: list[Json]) -> list[Json]:
    """Validate durable link metadata without discarding local deleted steps."""
    if sources is None:
        return []
    if not isinstance(sources, list) or len(sources) > _workflows.MAX_STEPS:
        raise _error("A Workflow can hold at most 20 template uses.")
    ids: set[str] = set()
    owned: set[str] = set()
    result = []
    for raw in sources:
        if not isinstance(raw, dict):
            raise _error("A template use must be an object.")
        source = deepcopy(raw)
        source_id = source.get("id")
        if not isinstance(source_id, str) or not _ID.fullmatch(source_id) or source_id in ids:
            raise _error("Template use ids must be distinct valid identifiers.")
        ids.add(source_id)
        if (
            not isinstance(source.get("template_id"), str)
            or not source["template_id"]
            or len(source["template_id"]) > 200
        ):
            raise _error("A template use needs its saved template id.")
        version = source.get("version")
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise _error("A template use needs its published version.")
        mapping = source.get("step_map")
        if not isinstance(mapping, dict) or not mapping or len(mapping) > 400:
            raise _error("A template use needs a bounded step identity map.")
        if any(
            not isinstance(item, str) or not _ID.fullmatch(item)
            for pair in mapping.items()
            for item in pair
        ):
            raise _error("A template step map contains an invalid identifier.")
        members = set(mapping.values())
        if len(members) != len(mapping) or owned & members:
            raise _error("A step cannot belong to two template uses.")
        owned.update(members)
        baseline = source.get("base")
        if (
            not isinstance(baseline, dict)
            or not isinstance(baseline.get("steps"), list)
            or not baseline["steps"]
            or len(baseline["steps"]) > _workflows.MAX_STEPS
        ):
            raise _error("A template use needs its published step baseline.")
        for field in ("name", "description"):
            if not isinstance(baseline.get(field), str):
                raise _error("A template baseline needs a name and description.")
        baseline_ids = []
        for step in baseline["steps"]:
            if (
                not isinstance(step, dict)
                or step.get("id") not in members
                or any(field not in step for field in _FIELDS)
            ):
                raise _error("A template baseline has an invalid step.")
            baseline_ids.append(step["id"])
            if not isinstance(step["after"], list) or any(
                not isinstance(item, str) or not _ID.fullmatch(item) for item in step["after"]
            ):
                raise _error("A template baseline has invalid dependencies.")
            if not isinstance(step["returns_to"], str) or (
                step["returns_to"] and not _ID.fullmatch(step["returns_to"])
            ):
                raise _error("A template baseline has an invalid return target.")
        if len(set(baseline_ids)) != len(baseline_ids):
            raise _error("A template baseline repeats a step.")
        if not isinstance(source.get("whole_workflow"), bool):
            raise _error("A template use must say whether it supplies the whole Workflow.")
        anchors = source.setdefault("after", [])
        if not isinstance(anchors, list) or any(
            not isinstance(item, str) or not _ID.fullmatch(item) for item in anchors
        ):
            raise _error("A template use has invalid insertion dependencies.")
        position = source.setdefault("position", 0)
        if (
            not isinstance(position, int)
            or isinstance(position, bool)
            or not 0 <= position <= _workflows.MAX_STEPS
        ):
            raise _error("A template use has an invalid insertion position.")
        connections = source.setdefault("connections", {})
        if (
            not isinstance(connections, dict)
            or len(connections) > _workflows.MAX_STEPS
            or any(
                not isinstance(key, str)
                or not _ID.fullmatch(key)
                or not isinstance(value, list)
                or any(not isinstance(item, str) or not _ID.fullmatch(item) for item in value)
                for key, value in connections.items()
            )
        ):
            raise _error("A template use has invalid outgoing dependencies.")
        result.append(source)
    return result


class _Merge:
    def __init__(self, resolutions: Json | None) -> None:
        if resolutions is not None and (
            not isinstance(resolutions, dict)
            or any(
                not isinstance(value, str) or value not in {"local", "template"}
                for value in resolutions.values()
            )
        ):
            raise _error("Resolve conflicts with local or template.", "invalid_resolution")
        self.resolutions = resolutions or {}
        self.conflicts: list[Json] = []
        self.changes: list[Json] = []

    def value(
        self, step: str, field: str, base: Any, local: Any, template: Any, *, force: bool = False
    ) -> Any:
        if template != base:
            self.changes.append(
                {
                    "step": step,
                    "field": field,
                    "base": deepcopy(base),
                    "template": deepcopy(template),
                }
            )
        if not force:
            if local == template or template == base:
                return deepcopy(local)
            if local == base:
                return deepcopy(template)
        conflict_id = f"{step or 'workflow'}.{field}"
        resolution = self.resolutions.get(conflict_id, "")
        self.conflicts.append(
            {
                "id": conflict_id,
                "step": step,
                "field": field,
                "base": deepcopy(base),
                "local": deepcopy(local),
                "template": deepcopy(template),
                "resolution": resolution,
            }
        )
        return deepcopy(template if resolution == "template" else local)


def _order(steps: list[Json], members: set[str]) -> list[str]:
    return [step["id"] for step in steps if step["id"] in members]


def _retain_position(order: list[str], step_id: str, local_order: list[str]) -> None:
    """Keep a retained Project step between its surviving local neighbours."""
    position = local_order.index(step_id)
    before = [item for item in local_order[:position] if item in order]
    after = [item for item in local_order[position + 1 :] if item in order]
    if after and (not before or order.index(before[-1]) < order.index(after[0])):
        order.insert(order.index(after[0]), step_id)
    elif before:
        order.insert(order.index(before[-1]) + 1, step_id)
    else:
        order.append(step_id)


def _connection_update(
    baseline: list[str], old_steps: list[Json], new_steps: list[Json]
) -> tuple[list[str], bool]:
    """Follow only the exits this consumer used, flagging removed partial branches."""
    old_exits = set(_terminals(old_steps))
    new_exits = _terminals(new_steps)
    used = old_exits & set(baseline)
    if not used or old_exits == set(new_exits):
        return list(baseline), False
    replacements: dict[str, list[str]] = {}
    ambiguous = False
    if used == old_exits:
        # This consumer used the entire template, so it follows all new exits.
        replacements = dict.fromkeys(used, new_exits)
    else:
        new_ids = {step["id"] for step in new_steps}
        for used_exit in used:
            if used_exit in new_ids:
                replacements[used_exit] = [
                    end
                    for end in new_exits
                    if end == used_exit or used_exit in _workflows._closure(new_steps, [end])
                ]
            else:
                # The template removed only one of the consumer's branches.
                # Its surviving ancestors suggest a continuation, but the user
                # must decide whether that still expresses the Project's intent.
                ancestors = _workflows._closure(old_steps, [used_exit]) & new_ids
                replacements[used_exit] = [
                    end for end in new_exits if ancestors & _workflows._closure(new_steps, [end])
                ] or list(new_exits)
                ambiguous = True
    updated = list(
        dict.fromkeys(target for item in baseline for target in replacements.get(item, [item]))
    )
    return updated, ambiguous


def preview_update(
    workflow: Json, source_id: str, template: Json, *, resolutions: Json | None = None
) -> Json:
    """Return a proposed update; unresolved conflicts retain the local value."""
    result = _workflows.normalize([workflow])[0]
    source = _source(result, source_id)
    material = _material(template)
    if source["template_id"] != template["id"] or template["version"] < source["version"]:
        raise _error("Choose the same template at its current or a newer version.")
    mapping = dict(source["step_map"])
    taken = {step["id"] for step in result["steps"]} | set(mapping.values())
    # A Project may publish its own local additions as a new template version.
    # Match their exact exported definitions before allocating a second copy.
    candidates: dict[str, tuple[str, Json]] = {}
    if source["whole_workflow"] or set(mapping.values()) & {step["id"] for step in result["steps"]}:
        exported, export_map = _export_steps(result, None, source)
        exported_by_id = {step["id"]: step for step in exported}
        other_members = {
            local
            for item in result.get("sources", [])
            if item["id"] != source_id
            for local in item["step_map"].values()
        }
        candidates = {
            original: (local, exported_by_id[original])
            for local, original in export_map.items()
            if local not in mapping.values() and local not in other_members
        }
    for step in material["steps"]:
        if step["id"] not in mapping:
            candidate = candidates.get(step["id"])
            local_id = (
                candidate[0]
                if candidate is not None and candidate[1] == step
                else _stable_id(f"{source_id}:{step['id']}", taken)
            )
            mapping[step["id"]] = local_id
            taken.add(local_id)
    current = {step["id"]: step for step in result["steps"]}
    current_ids = set(current)
    base = deepcopy(source["base"])
    base_members = {step["id"] for step in base["steps"]}
    entries = [step for step in base["steps"] if not base_members & set(step["after"])]
    live_entries = [step for step in entries if step["id"] in current]
    # Dependencies outside a use are its Project placement, not template
    # edits. Carry that placement to new entry steps even when an earlier
    # template update changed the output this use follows.
    anchors = (
        list(
            dict.fromkeys(
                parent
                for step in live_entries
                for parent in current[step["id"]]["after"]
                if parent not in set(source["step_map"].values())
            )
        )
        if live_entries
        else [item for item in source["after"] if item in current_ids]
    )
    entry_inputs = {
        entry["id"]: [
            parent
            for parent in current[entry["id"]]["after"]
            if parent not in set(source["step_map"].values())
        ]
        for entry in live_entries
    }
    if all(set(inputs) == set(anchors) for inputs in entry_inputs.values()):
        for entry in live_entries:
            entry["after"] = entry_inputs[entry["id"]]
    upstream = _mapped(material, mapping, anchors)
    before = {step["id"]: step for step in base["steps"]}
    incoming = {step["id"]: step for step in upstream["steps"]}
    members = set(before) | set(incoming)
    merge = _Merge(resolutions)
    merged: dict[str, Json] = {}
    for step_id in dict.fromkeys([*before, *incoming]):
        old, local, new = before.get(step_id), current.get(step_id), incoming.get(step_id)
        if old is None or local is None or new is None:
            value = merge.value(step_id, "step", old, local, new)
        else:
            value = {"id": step_id} | {
                field: merge.value(step_id, field, old[field], local[field], new[field])
                for field in _FIELDS
            }
            value["limits"] = merge.value(
                step_id,
                "limits",
                old.get("limits", {}),
                local.get("limits", {}),
                new.get("limits", {}),
            )
        if value is not None:
            merged[step_id] = value

    if source["whole_workflow"]:
        for field in ("name", "description", "limits"):
            result[field] = merge.value(
                "", field, base.get(field, {}), result.get(field, {}), upstream.get(field, {})
            )

    surviving = set(merged)
    common = set(before) & set(current) & set(incoming) & surviving
    old_order = _order(base["steps"], common)
    local_order = _order(result["steps"], common)
    new_order = _order(upstream["steps"], common)
    chosen_order = merge.value("", "order", old_order, local_order, new_order)
    desired = _order(upstream["steps"], surviving)
    # Preserve the chosen ordering of existing steps, while keeping additions
    # adjacent to their nearest preceding template step.
    if chosen_order != new_order:
        extras = [item for item in desired if item not in common]
        desired = list(chosen_order)
        for extra in extras:
            source_index = next(
                index for index, step in enumerate(upstream["steps"]) if step["id"] == extra
            )
            preceding = [
                step["id"] for step in upstream["steps"][:source_index] if step["id"] in desired
            ]
            index = desired.index(preceding[-1]) + 1 if preceding else 0
            desired.insert(index, extra)
    project_order = [step["id"] for step in result["steps"]]
    for step in result["steps"]:
        if step["id"] in surviving and step["id"] not in desired:
            _retain_position(desired, step["id"], project_order)

    connections: Json = {}
    for step_id, baseline in source["connections"].items():
        next_after, ambiguous = _connection_update(baseline, base["steps"], upstream["steps"])
        connections[step_id] = next_after
        if step_id in current:
            current[step_id]["after"] = merge.value(
                step_id,
                "after",
                baseline,
                current[step_id]["after"],
                next_after,
                force=ambiguous,
            )

    # A locally added reference is not permission to delete its target. Keep
    # that target unless the user deliberately accepts its removal, and expose
    # any remaining invalid references in the preview.
    for step_id in set(before) - set(incoming) - set(merged):
        external = [
            step
            for step in current.values()
            if step["id"] not in members
            and (step_id in step["after"] or step["returns_to"] == step_id)
        ]
        if (
            external
            and step_id in current
            and not any(item["id"] == f"{step_id}.step" for item in merge.conflicts)
        ):
            value = merge.value(
                step_id, "step", before[step_id], current[step_id], None, force=True
            )
            if value is not None:
                merged[step_id] = value
                _retain_position(desired, step_id, project_order)

    projected: list[Json] = []
    todo = list(desired)
    slots = [index for index, step in enumerate(result["steps"]) if step["id"] in members]
    last = slots[-1] if slots else -1
    for index, step in enumerate(result["steps"]):
        if step["id"] in members:
            if todo:
                projected.append(merged[todo.pop(0)])
            if index == last:
                projected.extend(merged[item] for item in todo)
                todo.clear()
        else:
            projected.append(current[step["id"]])
    if todo:
        position = min(source["position"], len(projected))
        projected[position:position] = [merged[item] for item in todo]
    result["steps"] = projected
    source.update(
        version=template["version"],
        step_map=mapping,
        base=upstream,
        after=anchors,
        connections=connections,
    )
    unknown = set(merge.resolutions) - {item["id"] for item in merge.conflicts}
    if unknown:
        raise _error(
            "The conflicts changed. Refresh the update preview before resolving them.",
            "invalid_resolution",
        )
    problems = []
    try:
        result = _workflows.normalize([result])[0]
    except FactoryError as error:
        problems.append(str(error))
    return {
        "workflow": result,
        "conflicts": merge.conflicts,
        "changes": merge.changes,
        "valid": not problems,
        "problems": problems,
    }
