"""Permission profiles and operator decisions over the WebUI's RPC contract."""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from typing import Any

from cli._input import _read_text_file
from cli.device_management import run_refusal
from cli.formatting import inline_text, record_fields
from cli.rpc_client import RpcPayload, rpc_call
from cli.server_management import CommandResult, ServerInstance


def dispatch_permission_profiles(
    args: argparse.Namespace, instance: ServerInstance
) -> CommandResult:
    """Show profile names and edit a shared rule set through the operator RPCs."""
    command = args.command
    params: dict[str, Any] = {}
    method = "permissions.profiles"
    if command == "list" and (not 1 <= args.limit <= 200 or args.offset < 0):
        return _failure(
            instance,
            args.json,
            "invalid_arguments",
            "--limit needs 1-200 and --offset needs 0 or more.",
            refused=True,
        )
    if command in {"save", "delete"}:
        refusal = run_refusal(
            f"permission-profiles {command}",
            "the user can manage profiles in their terminal or the WebUI",
        )
        if refusal:
            return _failure(instance, args.json, "operator_required", refusal, refused=True)
        method = f"permissions.{command}_profile"
        params["name"] = args.name
    if command == "save":
        try:
            rules = json.loads(_read_text_file(args.file))
            if not isinstance(rules, dict):
                raise ValueError("expected a JSON object of Permission Rules")
        except (OSError, ValueError) as error:
            return _failure(
                instance,
                args.json,
                "invalid_rules_file",
                f"Cannot read {inline_text(args.file)}: {inline_text(error)}. "
                "No profile was saved.",
                refused=True,
            )
        params["permission_rules"] = rules
    response = rpc_call(instance, method, params)
    if not response.ok:
        return _rpc_failure(response, args.json)
    profiles = response.data.get("profiles")
    if not isinstance(profiles, list) or not all(
        isinstance(profile, dict)
        and isinstance(profile.get("name"), str)
        and isinstance(profile.get("builtin"), bool)
        and isinstance(profile.get("rules"), dict)
        for profile in profiles
    ):
        return _failure(
            instance, args.json, "invalid_response", "The server returned no valid profile catalog."
        )
    if command == "list":
        page = profiles[args.offset : args.offset + args.limit]
        data = {"profiles": page, "total": len(profiles)}
        lines = [f"Permission profiles: showing {len(page)} of {len(profiles)}."]
        lines.extend(
            f"  {inline_text(p['name'])} ({'built-in' if p['builtin'] else 'custom'})" for p in page
        )
        if args.offset + len(page) < len(profiles):
            lines.append(
                f"Next: vbot permission-profiles list --offset {args.offset + len(page)} "
                f"--limit {args.limit}{_target_options(args)}"
            )
    else:
        name = inline_text(args.name)
        profile = next((p for p in profiles if p["name"] == args.name), None)
        if command == "delete" and profile is None:
            data = {"name": args.name, "deleted": True}
            lines = [f"Deleted permission profile {name}."]
        elif command in {"save", "show"} and profile is not None:
            data = profile
            label = "Saved permission profile" if command == "save" else "Permission profile"
            lines = [
                f"{label}: {name}",
                f"Origin: {'built-in' if profile['builtin'] else 'custom'}",
                f"Rules: {json.dumps(profile['rules'], sort_keys=True, ensure_ascii=True)}",
            ]
        elif command == "show":
            return _failure(
                instance,
                args.json,
                "profile_not_found",
                f"Unknown permission profile {name}. Available profiles: "
                + ", ".join(inline_text(p["name"]) for p in profiles),
            )
        else:
            return _failure(
                instance,
                args.json,
                "invalid_response",
                f"The server catalog does not confirm the requested change to {name}.",
            )
    return CommandResult(
        ok=True,
        message=json.dumps(data, ensure_ascii=True) if args.json else "\n".join(lines),
        instance=instance,
    )


def dispatch_approval_command(args: argparse.Namespace, instance: ServerInstance) -> CommandResult:
    """List, inspect or answer a pending server-owned approval."""
    if args.command in {"allow", "reject"}:
        refusal = run_refusal(
            "approvals " + args.command,
            "the user can answer in the WebUI or run this command in their own terminal",
        )
        if refusal:
            return _failure(instance, args.json, "user_decision_required", refusal)
        result = rpc_call(
            instance, "permissions.resolve", {"id": args.id, "decision": args.command}
        )
        if not result.ok:
            return _rpc_failure(result, args.json)
        if result.data.get("resolved") is not True:
            return _failure(
                instance, args.json, "invalid_response", "The server did not confirm the decision."
            )
        data = {"id": args.id, "decision": args.command, "resolved": True}
        message = (
            json.dumps(data, ensure_ascii=True)
            if args.json
            else (
                f"{'Allowed once' if args.command == 'allow' else 'Rejected'} "
                f"{inline_text(args.id)}."
            )
        )
        return CommandResult(ok=True, message=message, instance=instance)
    if args.command == "list" and (not 1 <= args.limit <= 200 or args.offset < 0):
        return _failure(
            instance,
            args.json,
            "invalid_arguments",
            "--limit needs 1-200 and --offset needs 0 or more. No request was answered.",
        )
    result = rpc_call(instance, "permissions.pending", {})
    if not result.ok:
        return _rpc_failure(result, args.json)
    requests = result.data.get("requests")
    if not isinstance(requests, list) or not all(isinstance(item, dict) for item in requests):
        return _failure(
            instance,
            args.json,
            "invalid_response",
            "The server response has no valid pending requests list.",
        )
    if args.command == "show":
        request = next((item for item in requests if item.get("id") == args.id), None)
        if request is None:
            return _failure(
                instance,
                args.json,
                "approval_not_found",
                f"Request {inline_text(args.id)} is no longer pending. No request was answered.",
            )
        return CommandResult(
            ok=True,
            message=json.dumps(request, ensure_ascii=True) if args.json else _details(request),
            instance=instance,
        )
    selected = requests[args.offset : args.offset + args.limit]
    data = {"revision": result.data.get("revision"), "requests": selected, "total": len(requests)}
    if args.json:
        message = json.dumps(data, ensure_ascii=True)
    else:
        lines = [f"Pending approvals: showing {len(selected)} of {len(requests)}; server order."]
        lines.extend(_row(item) for item in selected)
        if args.offset + len(selected) < len(requests):
            lines.append(
                f"Next: vbot approvals list --offset {args.offset + len(selected)} "
                f"--limit {args.limit}{_target_options(args)}"
            )
        message = "\n".join(lines)
    return CommandResult(ok=True, message=message, instance=instance)


def _target_options(args: argparse.Namespace) -> str:
    from cli._recovery import format_command

    options = tuple(
        token
        for name, value in getattr(args, "_target_arguments", {}).items()
        for token in (f"--{name.replace('_', '-')}", str(value))
    )
    return " " + format_command(options) if options else ""


def _row(request: Mapping[str, Any]) -> str:
    reason = inline_text(request.get("reason"))
    if len(reason) > 160:
        reason = reason[:160] + "..."
    return record_fields(
        [
            inline_text(request.get("id")),
            f"agent={inline_text(request.get('agent_id'))}",
            f"session={inline_text(request.get('session_id'))}",
            f"tool={inline_text(request.get('tool_name'))}",
            reason,
        ]
    )


def _details(request: Mapping[str, Any]) -> str:
    execution = request.get("execution")
    computer = execution.get("computer_id") if isinstance(execution, dict) else None
    lines = [
        f"Pending approval: {inline_text(request.get('id'))}",
        f"Agent: {inline_text(request.get('agent_id'))}",
        f"Session: {inline_text(request.get('session_id'))}",
        f"Run: {inline_text(request.get('run_id'))}",
        f"Tool: {inline_text(request.get('tool_name'))}",
        f"Computer: {inline_text(computer)}",
        f"Working directory: {inline_text(request.get('cwd'))}",
        f"Reason: {inline_text(request.get('reason'))}",
        "Requested extra access:",
    ]
    if request.get("display_redacted"):
        lines.append("Some values are hidden; the decision applies to the original prepared call.")
    for requirement in request.get("requirements", []):
        lines.append("  " + inline_text(json.dumps(requirement, ensure_ascii=True)))
    arguments = json.dumps(request.get("arguments", {}), ensure_ascii=True)
    if len(arguments) > 7000:
        arguments = arguments[:7000] + "... [preview; --json prints the complete prepared call]"
    lines.extend(["Prepared arguments:", "  " + arguments])
    return "\n".join(lines)


def _rpc_failure(payload: RpcPayload, as_json: bool) -> CommandResult:
    if not as_json:
        return payload.to_command_result()
    return CommandResult(
        ok=False,
        message=json.dumps(
            {
                "ok": False,
                "error": {
                    "code": payload.failure.code if payload.failure else "rpc_error",
                    "message": payload.message,
                },
            },
            ensure_ascii=True,
        ),
        instance=payload.instance,
        failure=payload.failure,
    )


def _failure(
    instance: ServerInstance, as_json: bool, code: str, message: str, *, refused: bool = False
) -> CommandResult:
    return CommandResult(
        ok=False,
        message=json.dumps(
            {"ok": False, "error": {"code": code, "message": message}}, ensure_ascii=True
        )
        if as_json
        else message,
        instance=instance,
        refused=refused,
    )
