"""Human setup entry points and noninteractive access to its shared durable state."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import webbrowser

from cli._output import print_management_command_result
from cli._server_target import CommandResult, ServerInstance
from cli.formatting import inline_text
from cli.rpc_client import rpc_call

_SETUP_PAGE = "/?setup=1"
_TICKET_PATH = re.compile(r"/auth/ticket/[A-Za-z0-9_-]{16,256}")


def dispatch_setup_command(args: argparse.Namespace, instance: ServerInstance) -> int:
    if args.command != "open":
        return _saved_setup(args, instance)
    if args.browser:
        from cli.device_management import run_refusal

        refusal = run_refusal("Opening browser setup", "open setup yourself in the WebUI")
        if refusal is not None:
            print_management_command_result(
                CommandResult(
                    ok=False, instance=instance, message=f"Error: {refusal}", refused=True
                )
            )
            return 2
    if args.if_needed:
        if not _start_owned_server(instance):
            return 1
        payload = rpc_call(instance, "setup.get", {})
        if not payload.ok:
            print_management_command_result(payload.to_command_result())
            return 1
        status = payload.data.get("status")
        auto_show = payload.data.get("auto_show")
        if status not in ("pending", "completed", "skipped") or auto_show is not (
            status == "pending"
        ):
            print("Error: the server returned an invalid setup status. Setup was not opened.")
            return 1
        if not auto_show:
            print(f"Setup is already {status} for {inline_text(instance.url)}. Nothing to open.")
            return 0
    if not args.browser and not all(
        stream is not None and stream.isatty() for stream in (sys.stdin, sys.stdout)
    ):
        print_management_command_result(
            CommandResult(
                ok=False,
                instance=instance,
                refused=True,
                message="Error: vbot setup requires an interactive terminal. "
                "Use vbot setup status to inspect setup or vbot setup finish to skip it.",
            )
        )
        return 2
    if args.browser:
        return _open_browser(instance, start_server=not args.if_needed)
    if not args.if_needed and not _start_owned_server(instance):
        return 1
    try:
        from cli.tui.setup import run_setup

        return asyncio.run(run_setup(target=instance))
    except ModuleNotFoundError as error:
        if error.name != "textual" and not str(error.name).startswith("textual."):
            raise
        print("Error: the terminal UI dependency is missing. Install vBot with its cli extra.")
        return 1
    except KeyboardInterrupt:
        return 130


def _start_owned_server(instance: ServerInstance) -> bool:
    from cli.application import processes

    install = processes.owning_installation(instance)
    if install is None or processes.target(install).url != instance.url:
        return True
    result = processes.start_or_restart(install, "start")
    if not result.ok:
        print_management_command_result(result)
        return False
    if result.message != "already running":
        print(f"Started the installed server at {inline_text(instance.url)}.")
    return True


def _saved_setup(args: argparse.Namespace, instance: ServerInstance) -> int:
    payload = rpc_call(instance, "setup.get" if args.command == "status" else "setup.finish", {})
    if args.json:
        result = (
            {"ok": True, "result": dict(payload.data)}
            if payload.ok
            else {
                "ok": False,
                "error": {
                    "code": payload.failure.code if payload.failure else "setup_failed",
                    "message": payload.message,
                },
            }
        )
        print(json.dumps(result, ensure_ascii=False))
    elif payload.ok:
        label = "Setup status" if args.command == "status" else "Setup ended"
        print(f"{label}: {inline_text(str(payload.data.get('status', 'unknown')))}")
        print(f"server: {inline_text(instance.url)}")
        for name in ("step", "agent_id", "auto_show"):
            print(f"{name}: {inline_text(str(payload.data.get(name, 'unknown')))}")
        if args.command == "finish":
            print("Current settings were kept. Automatic setup is disabled for this server.")
    else:
        print_management_command_result(payload.to_command_result())
    return 0 if payload.ok else 1


def _open_browser(instance: ServerInstance, *, start_server: bool = True) -> int:
    from cli.application import processes

    if start_server and not _start_owned_server(instance):
        return 1
    payload = rpc_call(instance, "auth.login_ticket", {"name": "Browser", "next": _SETUP_PAGE})
    if not payload.ok:
        if payload.failure is None or payload.failure.code != "auth_disabled":
            print_management_command_result(payload.to_command_result())
            return 1
        url = f"{instance.url}{_SETUP_PAGE}"
    else:
        path = payload.data.get("path")
        if not isinstance(path, str) or _TICKET_PATH.fullmatch(path) is None:
            print("Error: the server returned no valid browser sign-in link.")
            return 1
        origin = instance.url
        install = processes.owning_installation(instance)
        if install is not None and processes.target(install).url == instance.url:
            access = install.access
            if access is not None and access.mode == "authenticated" and access.public_url:
                origin = access.public_url
        url = f"{origin}{path}"
    try:
        opened = webbrowser.open(url)
    except OSError, webbrowser.Error:
        opened = False
    if not opened:
        print(
            "Error: the browser could not be opened. Open "
            f"{inline_text(instance.url)} in a browser and choose Setup."
        )
        return 1
    print(f"Opened setup in the browser for {inline_text(instance.url)}.")
    return 0
