"""Task-model binding management RPC commands for the vBot CLI."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from cli.config_management import coerce_config_value
from cli.formatting import string_or_default as _string_or_default
from cli.rpc_client import httpx as httpx
from cli.rpc_client import rpc_call as _rpc_call
from cli.server_management import CommandResult, ServerInstance


def task_model_list(instance: ServerInstance) -> CommandResult:
    """Return formatted task-model bindings from `task_model.settings` RPC."""

    payload = _rpc_call(instance, "task_model.settings", {})
    if not payload.ok:
        return payload.to_command_result()
    model_tasks = payload.data.get("model_tasks")
    if not isinstance(model_tasks, dict):
        return CommandResult(
            ok=False, message="RPC result missing model_tasks object", instance=instance
        )
    return CommandResult(ok=True, message=_format_binding_rows(model_tasks), instance=instance)


def task_model_targets(instance: ServerInstance, task_type: str) -> CommandResult:
    """Return formatted target rows from `task_model.list_targets` RPC."""

    payload = _rpc_call(instance, "task_model.list_targets", {"task_type": task_type})
    if not payload.ok:
        return payload.to_command_result()
    targets = payload.data.get("targets")
    if not isinstance(targets, list):
        return CommandResult(ok=False, message="RPC result missing targets list", instance=instance)
    return CommandResult(
        ok=True, message=_format_target_rows(task_type, targets), instance=instance
    )


def task_model_options(
    instance: ServerInstance, task_type: str, target: str | None
) -> CommandResult:
    """Return one target's option schema from `task_model.options` RPC."""

    params = {"task_type": task_type}
    if target is not None:
        params["target"] = target
    payload = _rpc_call(instance, "task_model.options", params)
    if not payload.ok:
        return payload.to_command_result()
    schema = payload.data.get("schema")
    if schema is None:
        return CommandResult(ok=False, message="RPC result missing schema", instance=instance)
    return CommandResult(
        ok=True,
        message=json.dumps(schema, indent=2, ensure_ascii=False, sort_keys=True),
        instance=instance,
    )


def task_model_set(
    instance: ServerInstance,
    task_type: str,
    target: str,
    options_json: str | None,
    option_pairs: Sequence[Sequence[str]] = (),
) -> CommandResult:
    """Bind a task type to a target via `task_model.update` RPC."""

    binding: dict[str, Any] = {"target": target}
    if options_json is not None:
        try:
            options = json.loads(options_json)
        except json.JSONDecodeError as exc:
            return CommandResult(
                ok=False, message=f"--options is not valid JSON: {exc}", instance=instance
            )
        if not isinstance(options, dict):
            return CommandResult(
                ok=False, message="--options must be a JSON object", instance=instance
            )
        binding["options"] = options
    elif option_pairs:
        options = {}
        for pair in option_pairs:
            name, raw_value = pair
            if name in options:
                return CommandResult(
                    ok=False,
                    message=f"--option was provided more than once for {name!r}",
                    instance=instance,
                )
            options[name] = coerce_config_value(raw_value)
        binding["options"] = options

    payload = _rpc_call(instance, "task_model.update", {"model_tasks": {task_type: binding}})
    if not payload.ok:
        return payload.to_command_result()
    return _saved_binding_result(instance, payload.data, task_type)


def task_model_set_option(
    instance: ServerInstance,
    task_type: str,
    name: str,
    raw_value: str,
) -> CommandResult:
    """Set one option on the current binding without replacing sibling options."""

    return _patch_task_model_options(
        instance,
        task_type,
        set_values={name: coerce_config_value(raw_value)},
    )


def task_model_unset_option(
    instance: ServerInstance,
    task_type: str,
    name: str,
) -> CommandResult:
    """Remove one option from the current binding without replacing sibling options."""

    return _patch_task_model_options(instance, task_type, unset_names=[name])


def _patch_task_model_options(
    instance: ServerInstance,
    task_type: str,
    *,
    set_values: dict[str, Any] | None = None,
    unset_names: list[str] | None = None,
) -> CommandResult:
    params: dict[str, Any] = {"task_type": task_type}
    if set_values:
        params["set"] = set_values
    if unset_names:
        params["unset"] = unset_names
    payload = _rpc_call(instance, "task_model.patch_options", params)
    if not payload.ok:
        return payload.to_command_result()
    return _saved_binding_result(instance, payload.data, task_type)


def _saved_binding_result(
    instance: ServerInstance,
    payload: Mapping[str, Any],
    task_type: str,
) -> CommandResult:
    model_tasks = payload.get("model_tasks")
    if not isinstance(model_tasks, dict) or task_type not in model_tasks:
        return CommandResult(
            ok=False,
            message="RPC result missing saved task-model binding",
            instance=instance,
        )
    return CommandResult(
        ok=True,
        message=_format_binding_row(task_type, model_tasks[task_type]).removeprefix("- "),
        instance=instance,
    )


def task_model_clear(instance: ServerInstance, task_type: str) -> CommandResult:
    """Remove one task-type binding via `task_model.update` RPC."""

    params = {"model_tasks": {task_type: {"target": ""}}}
    payload = _rpc_call(instance, "task_model.update", params)
    if not payload.ok:
        return payload.to_command_result()
    return CommandResult(ok=True, message=f"cleared {task_type} binding", instance=instance)


def _format_binding_rows(model_tasks: dict[str, Any]) -> str:
    if not model_tasks:
        return "no task-model bindings configured"

    lines = ["task-model bindings:"]
    for task_type in sorted(model_tasks):
        lines.append(_format_binding_row(task_type, model_tasks[task_type]))
    return "\n".join(lines)


def _format_binding_row(task_type: str, binding: object) -> str:
    if not isinstance(binding, dict):
        return f"- {task_type}: invalid binding entry"

    target = _string_or_default(binding.get("target"), "?")
    options = binding.get("options")
    options_text = (
        json.dumps(options, ensure_ascii=False, sort_keys=True)
        if isinstance(options, dict) and options
        else "{}"
    )
    return f"- {task_type}: target={target} options={options_text}"


def _format_target_rows(task_type: str, targets: Sequence[object]) -> str:
    if not targets:
        return f"no targets available for {task_type}"

    lines = [f"targets for {task_type}:"]
    for target in targets:
        lines.append(_format_target_row(target))
    return "\n".join(lines)


def _format_target_row(target: object) -> str:
    if not isinstance(target, dict):
        return "- invalid target entry"

    target_id = _string_or_default(target.get("id"), "?")
    kind = _string_or_default(target.get("kind"), "?")
    label = _string_or_default(target.get("label"), "?")
    usable = "yes" if target.get("usable") else "no"
    return f"- id={target_id} kind={kind} label={label} usable={usable}"
