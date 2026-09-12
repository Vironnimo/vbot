"""Local CLI lifecycle dispatch and Desktop launch."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from cli._output import (
    FAILURE_EXIT_CODE,
    SUCCESS_EXIT_CODE,
)
from cli.autostart_management import autostart_status, disable_autostart, enable_autostart
from cli.server_management import (
    DEFAULT_HOST,
    DEFAULT_SERVICE_NAME,
    CommandResult,
    ServerInstance,
    restart_via_systemd_if_managed,
)
from cli.update_management import run_update


def _launch_desktop(argv: Sequence[str]) -> None:
    """Open the Desktop window via its stable entrypoint.

    The Desktop launcher is imported lazily so the default CLI path never
    requires the optional ``[desktop]`` group (pywebview). A missing pywebview
    surfaces through the launcher's own ``load_webview`` message, which is
    raised as ``RuntimeError`` and turned into a failure exit by the caller.
    """

    from desktop.main import main as desktop_main

    desktop_main(list(argv))


@dataclass(frozen=True)
class ServerCommandContext:
    """Parsed server command target and service dispatch functions."""

    command: str
    host: str
    port: int | None
    data_dir: str | None
    service_name: str
    resolve: Callable[..., ServerInstance]
    start: Callable[[ServerInstance], CommandResult]
    stop: Callable[[ServerInstance], CommandResult]
    status: Callable[[ServerInstance], CommandResult]


def dispatch_doctor_command(
    args: argparse.Namespace,
    *,
    doctor_settings_fn: Callable[[str | Path | None], CommandResult],
    doctor_config_fn: Callable[[str | Path | None], CommandResult],
) -> CommandResult:
    """Dispatch one parsed local doctor command."""

    if args.command == "settings":
        return doctor_settings_fn(args.data_dir)
    if args.command == "config":
        return doctor_config_fn(args.data_dir)
    raise ValueError(f"Unsupported doctor command: {args.command}")


def dispatch_autostart_command(
    args: argparse.Namespace,
    *,
    resolve: Callable[..., ServerInstance],
    start: Callable[[ServerInstance], CommandResult],
    enable_fn: Callable[..., CommandResult] = enable_autostart,
    disable_fn: Callable[..., CommandResult] = disable_autostart,
    status_fn: Callable[..., CommandResult] = autostart_status,
) -> CommandResult:
    """Dispatch one parsed autostart command against the local OS."""

    instance = resolve(host=args.host, port=args.port, data_dir=args.data_dir)
    if args.command == "enable":
        return enable_fn(
            instance, start=start, task_name=args.task_name, service_name=args.service_name
        )
    if args.command == "disable":
        return disable_fn(instance, task_name=args.task_name, service_name=args.service_name)
    if args.command == "status":
        return status_fn(instance, task_name=args.task_name, service_name=args.service_name)
    raise ValueError(f"Unsupported autostart command: {args.command}")


def dispatch_update_command(
    args: argparse.Namespace,
    *,
    resolve: Callable[..., ServerInstance],
    stop: Callable[[ServerInstance], CommandResult],
    start: Callable[[ServerInstance], CommandResult],
    run_update_fn: Callable[..., CommandResult] = run_update,
) -> CommandResult:
    """Run the local self-update against the resolved server target."""

    instance = resolve(
        host=args.host if args.host is not None else DEFAULT_HOST,
        port=args.port,
        data_dir=args.data_dir,
    )
    return run_update_fn(
        instance,
        discard=args.discard,
        stash=args.stash,
        restart=not args.no_restart,
        stop=stop,
        start=start,
        service_name=getattr(args, "service_name", None) or DEFAULT_SERVICE_NAME,
        resolve=resolve,
        host=args.host,
        port=args.port,
        data_dir=args.data_dir,
    )


def dispatch_desktop_command(
    args: argparse.Namespace,
    *,
    launch_desktop_fn: Callable[[Sequence[str]], None],
) -> int:
    """Launch the Desktop window locally and return a stable exit code.

    This is a local GUI-launch action, not an RPC management command: it
    branches before the shared ``resolve(...)`` and never builds a
    ``ServerInstance``. Only the flags the user actually supplied are forwarded,
    so a bare ``vbot desktop`` reaches the launcher's last-used auto-connect path
    instead of a silent localhost target. The call blocks until the window
    closes; a missing ``[desktop]`` group raises ``RuntimeError`` (the launcher's
    own install hint), which maps to a failure exit.
    """

    launch_argv = _desktop_launch_argv(args)
    try:
        launch_desktop_fn(launch_argv)
    except RuntimeError as exc:
        print(f"error: {exc}")
        return FAILURE_EXIT_CODE
    print("desktop window closed")
    return SUCCESS_EXIT_CODE


def _desktop_launch_argv(args: argparse.Namespace) -> list[str]:
    """Build the Desktop launcher argv from only the supplied target flags."""

    launch_argv: list[str] = []
    if args.host is not None:
        launch_argv.extend(["--host", args.host])
    if args.port is not None:
        launch_argv.extend(["--port", str(args.port)])
    return launch_argv


def dispatch_server_command(
    context: ServerCommandContext,
    *,
    announce: Callable[[str, ServerInstance], None] | None = None,
) -> CommandResult:
    """Resolve the target and dispatch the requested server command."""

    instance = context.resolve(host=context.host, port=context.port, data_dir=context.data_dir)
    if announce is not None:
        announce(context.command, instance)
    if context.command == "start":
        return context.start(instance)
    if context.command == "stop":
        return context.stop(instance)
    if context.command == "restart":
        via_systemd = restart_via_systemd_if_managed(instance, service_name=context.service_name)
        if via_systemd is not None:
            return via_systemd
        stop_result = context.stop(instance)
        if not stop_result.ok:
            return stop_result
        restarted_instance = context.resolve(
            host=context.host,
            port=context.port,
            data_dir=context.data_dir,
        )
        return context.start(restarted_instance)
    if context.command == "status":
        return context.status(instance)
    raise ValueError(f"Unsupported server command: {context.command}")
