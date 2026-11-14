"""Thin CLI over the server's managed optional-service operations."""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from collections.abc import Mapping
from typing import Any

from cli._output import print_management_command_result
from cli._recovery import format_command, target_options
from cli._server_target import ServerInstance
from cli.application._service_prerequisites import ActionRequiredError as ActionRequiredError
from cli.formatting import inline_text
from cli.rpc_client import RpcPayload, rpc_call


def dispatch_service_command(args: argparse.Namespace, instance: ServerInstance) -> int:
    params: dict[str, Any] = {"id": args.service_id}
    if args.command in {"status", "install", "update"}:
        payload = rpc_call(instance, "managed_service.get", dict(params))
        if not payload.ok or args.command == "status":
            return _print_result(args, instance, payload)
        plan = payload.data.get("plan")
        if isinstance(plan, dict) and plan.get("local_authorization") is True:
            try:
                if args.json:
                    raise ActionRequiredError(
                        "JSON commands cannot request system authorization. "
                        + _host_authorization_guidance(payload.data)
                    )
                installed = install_local_service_prerequisites(
                    instance, payload.data, allow_prerequisites=args.install_prerequisites
                )
            except ActionRequiredError as error:
                return _print_result(
                    args,
                    instance,
                    RpcPayload(
                        ok=True,
                        instance=instance,
                        data={
                            **payload.data,
                            "state": "action_required",
                            "phase": "prerequisites",
                            "message": str(error),
                        },
                    ),
                )
            if installed:
                payload = rpc_call(instance, "managed_service.get", dict(params))
                if not payload.ok:
                    return _print_result(args, instance, payload)
        plan = payload.data.get("plan")
        plan_id = plan.get("id") if isinstance(plan, dict) else None
        if not isinstance(plan_id, str) or not plan_id:
            failed = RpcPayload(
                ok=False,
                instance=instance,
                message="The server returned no installation plan. No installation was requested.",
            )
            return _print_result(args, instance, failed)
        params["plan_id"] = plan_id
        params["allow_prerequisites"] = args.install_prerequisites
    params["action"] = args.command
    return _print_result(args, instance, rpc_call(instance, "managed_service.apply", params))


def install_local_service_prerequisites(
    instance: ServerInstance, state: Mapping[str, Any], *, allow_prerequisites: bool
) -> bool:
    """Authorize fixed host prerequisites from a human terminal for its owned server.

    A true result requires callers to fetch a fresh service plan before applying it.
    The terminal setup screen suspends itself while calling this blocking helper.
    """
    from cli.application import processes
    from cli.application._service_prerequisites import install_local_prerequisites
    from cli.device_management import run_refusal

    plan = state.get("plan")
    if not isinstance(plan, Mapping) or not (
        plan.get("requires_prerequisites") is True and plan.get("local_authorization") is True
    ):
        return False
    guidance = _host_authorization_guidance(state)
    if not allow_prerequisites:
        raise ActionRequiredError(
            "Installing system prerequisites requires --install-prerequisites or the "
            "setup prerequisite checkbox. " + guidance
        )
    refusal = run_refusal(
        "System authorization", "ask the user to install prerequisites on the vBot server"
    )
    if refusal is not None:
        raise ActionRequiredError(f"{refusal}. {guidance}")
    if not all(stream is not None and stream.isatty() for stream in (sys.stdin, sys.stdout)):
        raise ActionRequiredError("System authorization needs an interactive terminal. " + guidance)
    install = processes.owning_installation(instance)
    if install is None or processes.target(install).url != instance.url:
        raise ActionRequiredError(
            "The selected server is not this terminal's installed server. " + guidance
        )
    # The RPC command is guidance only. The application owner selects fixed local argv.
    install_local_prerequisites()
    return True


def _host_authorization_guidance(state: Mapping[str, Any]) -> str:
    plan = state.get("plan")
    command = plan.get("prerequisite_command") if isinstance(plan, Mapping) else None
    if isinstance(command, list) and command and all(isinstance(part, str) for part in command):
        return (
            f"Run {inline_text(shlex.join(command))} in a terminal on the vBot server, "
            "then repeat the service command or resume setup."
        )
    return (
        "Install Docker or Podman on the vBot server, "
        "then repeat the service command or resume setup."
    )


def _print_result(args: argparse.Namespace, instance: ServerInstance, payload: RpcPayload) -> int:
    if args.json:
        value = (
            {"ok": True, "result": dict(payload.data)}
            if payload.ok
            else {
                "ok": False,
                "error": {
                    "code": payload.failure.code if payload.failure else "service_failed",
                    "message": payload.message,
                },
            }
        )
        print(json.dumps(value, ensure_ascii=False))
    elif not payload.ok:
        print_management_command_result(payload.to_command_result())
    else:
        data = payload.data
        state = inline_text(str(data.get("state", "unknown")))
        print(f"Service {inline_text(args.service_id)}: {state}")
        print(f"server: {inline_text(instance.url)}")
        for key in ("installed", "phase", "message", "error", "url", "runtime"):
            if data.get(key) is not None:
                print(f"{key}: {inline_text(str(data[key]))}")
        plan = data.get("plan")
        if args.command == "status" and isinstance(plan, dict):
            print(f"requires_prerequisites: {plan.get('requires_prerequisites') is True}")
            for step in plan.get("steps", []):
                if isinstance(step, dict):
                    print(f"  {inline_text(str(step.get('label', '')))}")
        if args.command != "status" and data.get("state") == "running":
            followup = ("vbot", "service", "status", args.service_id, *target_options(instance))
            print(f"Next: {format_command(followup)}")
    failed = not payload.ok or (
        args.command != "status" and payload.data.get("state") in {"failed", "action_required"}
    )
    return 1 if failed else 0
