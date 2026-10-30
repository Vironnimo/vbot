"""Operator permission decisions over the WebUI's RPC contract."""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from typing import Any

from cli.device_management import run_refusal
from cli.formatting import inline_text, record_fields
from cli.rpc_client import RpcPayload, rpc_call
from cli.server_management import CommandResult, ServerInstance


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


def _failure(instance: ServerInstance, as_json: bool, code: str, message: str) -> CommandResult:
    return CommandResult(
        ok=False,
        message=json.dumps(
            {"ok": False, "error": {"code": code, "message": message}}, ensure_ascii=True
        )
        if as_json
        else message,
        instance=instance,
    )
