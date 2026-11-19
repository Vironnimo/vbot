"""Prepare an exact profile change for the vbot Tool and bind its approved write."""

from __future__ import annotations

import argparse
from contextvars import ContextVar
from typing import Any

from cli.device_management import run_refusal
from cli.rpc_client import rpc_call
from cli.server_management import ServerInstance

type JsonObject = dict[str, Any]

# None is the standalone CLI. The bound Tool has {} when the command needs no profile snapshot.
bound_profile_review: ContextVar[JsonObject | None] = ContextVar(
    "bound_profile_review", default=None
)


def profile_change_refusal(action: str) -> str | None:
    if bound_profile_review.get() is not None:
        return None
    return run_refusal(action, "use the vbot Tool for this change")


def profile_write_condition() -> JsonObject:
    snapshot = bound_profile_review.get()
    if not snapshot:
        return {}
    if snapshot["action"] in {"save", "delete"}:
        return {"base": snapshot["base"]}
    return {"expected_profile_rules": snapshot["rules"]}


def _read(instance: ServerInstance, method: str, **params: Any) -> JsonObject:
    result = rpc_call(instance, method, params)
    if not result.ok:
        raise ValueError(result.message)
    return dict(result.data)


def _rules(data: JsonObject, field: str) -> JsonObject | None:
    from core.permissions import complete_effective_permission_rules, permission_rules_to_dict

    if field not in data or not isinstance(data[field], dict | None):
        raise ValueError("The server could not preview the resulting Permission Rules.")
    rules = complete_effective_permission_rules(data[field])
    return permission_rules_to_dict(rules) if rules is not None else None


def _catalog_entry(instance: ServerInstance, name: str) -> JsonObject | None:
    from cli.approval_management import profile_rows

    rows = profile_rows(_read(instance, "permissions.profiles"))
    return next((row for row in rows if row["name"] == name), None)


def _catalog_change(instance: ServerInstance, args: argparse.Namespace) -> JsonObject:
    from cli.approval_management import profile_input
    from core.permissions import (
        custom_permission_profile_name,
        normalize_custom_permission_profile,
        permission_rules_to_dict,
    )

    name = custom_permission_profile_name(args.name)
    rules = None
    if args.command == "save":
        if args.file is not None:
            raise ValueError(
                "Use --stdin and pass the Permission Rules JSON in the vbot Tool's stdin "
                "so the user can approve the exact rules."
            )
        rules = permission_rules_to_dict(
            normalize_custom_permission_profile(name, profile_input(args))
        )
    entry = _catalog_entry(instance, name)
    if entry is None and args.command == "delete":
        raise ValueError(f"Unknown permission profile {name!r}.")
    return {
        "action": args.command,
        "name": name,
        "rules": rules,
        "base": entry["rules"] if entry else None,
    }


def _assignment(instance: ServerInstance, name: str, target: str) -> JsonObject:
    entry = _catalog_entry(instance, name)
    if entry is None:
        raise ValueError(f"Unknown permission profile {name!r}.")
    return {"action": "assign", "name": name, "target": target, "rules": entry["rules"]}


def _effective_detail(agent: JsonObject) -> JsonObject:
    effective = agent.get("effective")
    detail = effective.get("permission_rules") if isinstance(effective, dict) else None
    if not isinstance(detail, dict) or detail.get("error") or "value" not in detail:
        raise ValueError("The server could not resolve the Agent's Permission Rules.")
    return detail


def _team_fallback(instance: ServerInstance, args: argparse.Namespace) -> JsonObject | None:
    project = _read(instance, "project.show", project_id=args.id)
    scan = project.get("scan")
    team = scan.get("team") if isinstance(scan, dict) else None
    for member in team if isinstance(team, list) else ():
        if not isinstance(member, dict) or member.get("agent_id") != args.agent:
            continue
        detail = _effective_detail(member)
        if "overrides" not in member:
            break
        overrides = member["overrides"]
        if overrides is None or isinstance(overrides, dict):
            if overrides and overrides.get(args.field) is not None:
                return _rules(member, "permission_rules_default")
            return _rules(detail, "value")
        break
    raise ValueError("The server could not preview this Project Agent's Permission Rules.")


def prepare_profile_review(instance: ServerInstance, args: argparse.Namespace) -> JsonObject | None:
    if args.area == "permission-profiles" and args.command in {"save", "delete"}:
        return _catalog_change(instance, args)
    if args.area == "agent" and args.command in {"create", "update"}:
        from cli._dispatch_agents import _agent_tool_access_args_error, _permission_edit_from_args

        if error := _agent_tool_access_args_error(args):
            raise ValueError(error)
        target = args.id
        if args.permission_profile is not None:
            return _assignment(instance, args.permission_profile, target)
        edit = _permission_edit_from_args(args)
        if args.command == "create" or edit is None or edit.keeps_current:
            return None
        if edit.clear:
            agent = _read(instance, "agent.get", id=target)
            _effective_detail(agent)
            inherited = _rules(agent, "permission_rules_default")
            return {"action": "inherit", "name": None, "target": target, "rules": inherited}
        own: object = edit.apply(None)
    elif args.area == "project" and args.command in {"set-override", "clear-override"}:
        from cli.project_management import _coerce_override_value

        target = f"{args.agent}@{args.id}"
        if args.field not in {"permission_profile", "permission_rules"}:
            return None
        if args.command == "clear-override":
            return {
                "action": "inherit",
                "name": None,
                "target": target,
                "rules": _team_fallback(instance, args),
            }
        if args.field == "permission_profile":
            return _assignment(instance, args.value, target)
        own = _coerce_override_value(args.field, args.value)
    else:
        return None
    checked = _read(instance, "permissions.validate", permission_rules=own, agent_id=target)
    if checked.get("valid") is not True:
        raise ValueError(f"The server could not validate the Permission Rules for {target!r}.")
    return {
        "action": "inline",
        "name": None,
        "target": target,
        "rules": _rules(checked, "effective"),
    }
