"""Human CLI rendering of the shared application operation contract."""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import os
import sys
from pathlib import Path

from cli._output import print_application_update_result
from cli._progress import ProgressPrinter
from cli.application.state import ApplicationError, Installation, Operation, discover, exclusive
from cli.formatting import output_mode


def add_parsers(subparsers) -> None:
    app = subparsers.add_parser("application", help="Manage the packaged local vBot application")
    commands = app.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="Show the installed version and latest update")
    channel = commands.add_parser(
        "channel", help="Select whether updates install releases or the newest main build"
    )
    channel.add_argument("update_channel", choices=("main", "release"))
    _add_access_parsers(commands)
    commands.add_parser("tray", help="Run the vBot tray application")
    commands.add_parser("exit", help="Stop the owned tray application cleanly")
    commands.add_parser("removal-begin", help="Guard the native uninstaller's removal phase")
    commands.add_parser("removal-reset", help="Clear an interrupted removal after its owner exits")
    dependencies = commands.add_parser("dependencies", help="Manage extra Extension dependencies")
    dependencies.add_argument("dependency_action", choices=("status", "install"))
    dependencies.add_argument(
        "--requirements", type=Path, help="Complete replacement requirements recipe"
    )


def _add_access_parsers(commands) -> None:
    from cli.application.install import add_access_arguments
    from core.access.config import ACCESS_MODE_LABELS, ACCESS_MODES

    access = commands.add_parser(
        "access",
        help="Show or change who may connect to this installation's server",
        description=(
            "The access mode decides who may connect to the server: "
            + "; ".join(f"{mode} = {ACCESS_MODE_LABELS[mode]}" for mode in ACCESS_MODES)
            + ". A change applies when the server starts next."
        ),
    )
    actions = access.add_subparsers(dest="access_action", required=True)
    actions.add_parser("show", help="Show the recorded access mode and its options")
    setter = actions.add_parser(
        "set",
        help="Change the access mode; applies when the server starts next",
        description=(
            "Record a new access mode with all of its options; options not given are "
            "cleared. Example: vbot application access set authenticated "
            "--public-url https://vbot.example.org --trusted-proxy 127.0.0.1"
        ),
    )
    setter.add_argument("mode", choices=ACCESS_MODES)
    setter.add_argument(
        "--host",
        dest="bind_host",
        metavar="<address>",
        help=(
            "Bind address; omitted keeps the current one while the mode and its transport "
            "stay, else 127.0.0.1 (local, authenticated behind a proxy) or 0.0.0.0"
        ),
    )
    add_access_arguments(setter)


# Commands that manage an installed application, and what a development checkout,
# which is no installation, does instead.
_INSTALLATION_AREAS = {
    "application": "This command requires a packaged vBot installation",
    "update": (
        "vbot update updates a packaged vBot installation. "
        "This is a development checkout: update it with git"
    ),
    "rollback": (
        "vbot rollback requires a packaged vBot installation; this is a development checkout"
    ),
    "uninstall": (
        "vbot uninstall removes a packaged vBot installation. "
        "This is a development checkout: stop its server and delete the checkout yourself"
    ),
    "autostart": (
        "vbot autostart registers a packaged vBot installation. "
        "This is a development checkout: start its server with: vbot server start"
    ),
}


def _print(value, *, lines: list[str] | None = None) -> None:
    if lines is None or output_mode.get() == "plain":
        print(json.dumps(value, ensure_ascii=False, indent=2))
    else:
        print("\n".join(lines))


def _print_update_result(
    install: Installation, operation: Operation, *, handoff: bool = False
) -> None:
    from cli.application.operations import public_result

    if output_mode.get() == "plain":
        _print(public_result(operation))
    else:
        print_application_update_result(install, operation, handoff=handoff)


def _wait_update(install: Installation, operation: Operation) -> Operation:
    from cli._progress import status_line
    from cli.application import operations
    from cli.application.packages import version_label
    from cli.application.state import read_json

    if output_mode.get() == "plain":
        return operations.wait(install, operation.id)
    installed = version_label(read_json(install.version() / "release.json", limit=32 * 1024**2))
    print(status_line("busy", f"Checking for updates. Installed: {installed}"), flush=True)
    with ProgressPrinter(live=True) as progress:
        progress.track("Waiting for the update to start")
        announced: str | None = None

        def report(value: Operation) -> None:
            nonlocal announced
            if value.target_label and value.target_label != announced:
                announced = value.target_label
                if value.target_label != installed:
                    progress.emit(
                        "info",
                        f"Updating vBot: {value.previous_label or installed} "
                        f"-> {value.target_label}",
                    )
            if not value.terminal and value.phase != "queued":
                progress.emit("busy", value.message)

        return operations.wait(install, operation.id, progress=report)


def dispatch(args: argparse.Namespace) -> int | None:
    install = discover()
    if install is None:
        if args.area in _INSTALLATION_AREAS:
            raise ApplicationError(_INSTALLATION_AREAS[args.area])
        return None
    from core.utils.logging import LogManager

    # Commands on an installation log beside its host and update worker.
    manager = LogManager(data_dir=install.root, enable_console=False)
    try:
        return _dispatch_installed(args, install)
    finally:
        manager.close()


def _dispatch_installed(args: argparse.Namespace, install: Installation) -> int | None:
    from cli.application import operations, processes

    if args.area == "application":
        if args.command == "channel":
            from cli.application.state import CHANNEL_URLS
            from cli.application.state import operations as saved_operations

            with exclusive(install.root, "dispatch"), exclusive(install.root):
                if any(not item.terminal for item in saved_operations(install)):
                    raise ApplicationError(
                        "Wait for the pending update before changing its channel"
                    )
                selected = dataclasses.replace(
                    install, release_url=CHANNEL_URLS[args.update_channel]
                )
                selected.save()
                logging.getLogger("vbot.application.update").info(
                    "Application update channel changed (from=%s to=%s)",
                    install.channel,
                    selected.channel,
                )
            label = "the newest main build" if args.update_channel == "main" else "releases"
            _print(
                {"channel": args.update_channel, "next_command": "vbot update"},
                lines=[f"Updates now install {label}.", "Apply an update with: vbot update"],
            )
            return 0
        if args.command == "access":
            return _access_command(args, install)
        if args.command in {"removal-begin", "removal-reset"}:
            from cli.application.integration import begin_removal, reset_removal

            _print(
                begin_removal(install)
                if args.command == "removal-begin"
                else reset_removal(install)
            )
            return 0
        if args.command == "dependencies":
            from cli.application.dependencies import (
                install_dependencies,
            )
            from cli.application.dependencies import (
                status as dependency_status,
            )

            if args.dependency_action == "status":
                _print(dependency_status(install))
            else:
                if args.requirements is None:
                    raise ApplicationError(
                        "Supply --requirements with the complete Extension dependency recipe"
                    )
                with exclusive(install.root):
                    dependency_result = install_dependencies(install, args.requirements)
                dependency_result["restart_required"] = True
                _print(
                    dependency_result,
                    lines=[
                        "Extension dependencies are prepared.",
                        "Apply them with: vbot server restart",
                    ],
                )
            return 0
        if args.command == "exit":
            from cli.application.integration import request_host_exit

            request_host_exit(install)
            return 0
        if args.command == "tray":
            from cli.application.host import main

            main()
            return 0
        operation = operations.status(install)
        _print(
            {
                "root": str(install.root),
                "version": install.version().name,
                "shape": install.install_shape,
                "channel": install.channel,
                "update": operations.public_result(operation) if operation else None,
            }
        )
        return 0
    if args.area == "home":
        print(f"vbot_root: {install.version() / 'app'}")
        data_directory = (
            str(Path(args.data_dir).expanduser().resolve())
            if args.data_dir
            else install.server_data_directory
        )
        print(f"data_dir: {data_directory or 'not applicable (Desktop Client)'}")
        print(f"application_root: {install.root}")
        return 0
    if args.area in {"server", "autostart"} and (
        # Explicit lifecycle overrides remain deliberate targets, never silently
        # change which installed payload a process belongs to.
        getattr(args, "host", None) not in {None, "127.0.0.1", install.server_host}
        or getattr(args, "port", None) not in {None, install.server_port}
        or (
            getattr(args, "data_dir", None) is not None
            and Path(args.data_dir).resolve() != Path(install.server_data_directory or "").resolve()
        )
    ):
        raise ApplicationError(
            "Packaged lifecycle commands must target this installation's recorded server"
        )
    if args.area == "autostart":
        from cli.application.autostart import autostart

        registration = autostart(install, args.command)
        lines = [f"Autostart: {'enabled' if registration['enabled'] else 'disabled'}."]
        if "unit" in registration:
            lines.append(f"systemd user unit: {registration['unit']}")
        if "attention" in registration:
            lines.append(f"Attention: {registration['attention']}")
        _print(registration, lines=lines)
        return 0
    if args.area == "uninstall":
        from cli.application.integration import RemovalMode, uninstall

        data = Path(install.server_data_directory) if install.server_data_directory else None
        if args.uninstall_mode is None:
            if not sys.stdin.isatty():
                raise ApplicationError("Select --app-only, --data-only or --all explicitly")
            print(f"Application: {install.root}")
            mode = _choose_removal(data)
        else:
            mode = args.uninstall_mode
        if mode in {"all", "data-only"} and data is None:
            raise ApplicationError("This Desktop Client does not own server data")
        if mode is not None and not args.yes:
            if not sys.stdin.isatty():
                raise ApplicationError("Use --yes to confirm the explicitly selected removal mode")
            mode = mode if _confirm_removal(mode, data) else None
        if mode is None:
            print("uninstall: cancelled; no changes made")
            return 0
        removal = uninstall(install, mode=RemovalMode(mode))
        lines = (
            [
                "vBot data has been reset. The application is still installed.",
                "Server: restarted." if removal["server_restarted"] else "Server: stopped.",
            ]
            if mode == "data-only"
            else [
                f"vBot has been removed from {install.root}.",
                "Server data was removed."
                if removal["data_removed"]
                else "Server data is preserved.",
            ]
            if removal.get("removed")
            else [
                "The uninstaller has started. Application removal is not yet confirmed.",
                "Server data was removed."
                if removal["data_removed"]
                else "Server data is preserved.",
            ]
        )
        _print(removal, lines=lines)
        return 0
    if args.area == "server":
        from cli._output import exit_code_for, print_command_result, print_server_command_start

        print_server_command_start(args.command, processes.target(install))
        if args.command == "status":
            from cli.server_management import get_status

            result = get_status(processes.target(install))
            print_command_result("status", result)
            return exit_code_for("status", result)
        elif args.command == "stop":
            with exclusive(install.root, allow_removal=True):
                result = processes.stop(install)
        else:
            result = processes.start_or_restart(install, args.command)
        print_command_result(args.command, result)
        return 0 if result.ok else 1
    if args.area == "desktop":
        from cli.application.host import ApplicationFacade

        ApplicationFacade(install).open_desktop(host=args.host, port=args.port)
        print("Desktop launch requested")
        return 0
    if args.area == "rollback":
        if args.restore_data is not None and not args.yes:
            from cli.application.rollback import require_rollback_target

            point = require_rollback_target(install, restore_snapshot=args.restore_data)
            raise ApplicationError(
                f"Restoring snapshot {args.restore_data} from update {point.id} discards "
                "subsequent data changes. Nothing was changed. Confirm with "
                f"vbot rollback --restore-data {args.restore_data} --yes"
            )
        if args.restore_data is not None:
            from cli.device_management import run_refusal

            refusal = run_refusal(
                "rollback --restore-data",
                "restore the saved data yourself in a terminal; "
                "the restore can remove this Run's history",
            )
            if refusal is not None:
                raise ApplicationError(refusal)
        handoff = os.environ.get("VBOT_UPDATE_HANDOFF") if install.owns_server else None
        operation = operations.request_update(
            install, rollback=True, restore_snapshot=args.restore_data, handoff_token=handoff
        )
        if args.detach or handoff:
            _print_update_result(install, operation, handoff=bool(handoff))
            return 0
        outcome = _wait_update(install, operation)
        _print_update_result(install, outcome)
        return 0 if outcome.phase == "completed" else 1
    if args.area == "update":
        if args.update_action == "status":
            value = operations.status(install, args.operation_id)
            _print(
                operations.public_result(value)
                if value
                else {"message": "No update operation has been recorded"}
            )
            return 0
        if args.operation_id and args.update_action != "activate":
            raise ApplicationError(
                "An operation id is only valid with update status or update activate"
            )
        package: Path | None = None
        prepared_version: str | None = None
        release_version = (
            args.update_action if args.update_action not in {None, "status", "activate"} else None
        )
        if args.update_action == "activate":
            prepared = operations.status(install, args.operation_id)
            if prepared is None or prepared.phase != "prepared" or not prepared.candidate_version:
                raise ApplicationError("Select a prepared update operation to activate")
            prepared_version = prepared.candidate_version
            release_version = prepared.release_version
        elif args.package:
            package = Path(args.package)
        handoff = os.environ.get("VBOT_UPDATE_HANDOFF") if not args.no_restart else None
        if handoff and not install.owns_server:
            # An Agent on another server updating a client has no local Run to resume.
            handoff = None
        options = {"release_version": release_version} if release_version is not None else {}
        operation = operations.request_update(
            install,
            package=package,
            prepared_version=prepared_version,
            restart=not args.no_restart,
            handoff_token=handoff,
            **options,
        )
        if args.detach or handoff:
            _print_update_result(install, operation, handoff=bool(handoff))
            return 0
        outcome = _wait_update(install, operation)
        _print_update_result(install, outcome)
        return 0 if outcome.phase in {"completed", "prepared"} else 1
    # Ordinary management RPCs use the installed target by default. Client-only
    # remains a remote accessor, whose explicit host/port is resolved normally.
    if install.owns_server:
        if getattr(args, "data_dir", None) is None:
            args.data_dir = install.server_data_directory
        if getattr(args, "port", None) is None:
            args.port = install.server_port
        if getattr(args, "host", None) in {None, "127.0.0.1"}:
            args.host = install.server_host
    return None


def _access_command(args: argparse.Namespace, install: Installation) -> int:
    from cli.application.install import access_from_arguments
    from core.access.config import (
        AccessConfigError,
        check_bind_host,
        default_host,
        load_tls_context,
    )

    current = install.access
    if current is None:
        raise ApplicationError(
            "A Desktop Client has no server; change the access mode on the server's computer"
        )
    if args.access_action == "show":
        _print(
            _access_summary(install.server_host, current),
            lines=_access_lines(install.server_host, current),
        )
        return 0
    from cli.device_management import run_refusal

    refusal = run_refusal("application access set", "change the access mode yourself in a terminal")
    if refusal is not None:
        raise ApplicationError(refusal)
    selected = access_from_arguments(args.mode, args)
    keeps_transport = selected.mode == current.mode and selected.direct_tls == current.direct_tls
    host = args.bind_host or (
        install.server_host if keeps_transport and install.server_host else default_host(selected)
    )
    try:
        check_bind_host(selected, host)
        if selected.tls_cert is not None and selected.tls_key is not None:
            load_tls_context(selected.tls_cert, selected.tls_key)
    except AccessConfigError as exc:
        raise ApplicationError(str(exc)) from exc
    from cli.application.state import operations as saved_operations

    with exclusive(install.root, "dispatch"), exclusive(install.root):
        if any(not item.terminal for item in saved_operations(install)):
            raise ApplicationError("Wait for the pending update before changing the access mode")
        changed = dataclasses.replace(install, server_host=host, server_access=selected)
        changed.save()
        logging.getLogger("vbot.application.integration").info(
            "Server access mode changed (from=%s to=%s host=%s)",
            current.mode,
            selected.mode,
            host,
        )
        from cli.application.autostart import refresh_unit

        try:
            refresh_unit(changed)
        except (ApplicationError, OSError) as exc:
            raise ApplicationError(
                "The access settings were saved, but Autostart could not be refreshed. "
                f"Retry the same access command after resolving this failure: {exc}"
            ) from exc
    summary = _access_summary(host, selected)
    summary["applies"] = "on the next server start"
    _print(
        summary,
        lines=[
            *_access_lines(host, selected),
            "The change applies when the server starts next: vbot server restart",
        ],
    )
    return 0


def _access_summary(host: str | None, access) -> dict:
    return {"host": host, "label": access.label, **access.to_record()}


def _access_lines(host: str | None, access) -> list[str]:
    lines = [f"Access: {access.label} ({access.mode})", f"Bind address: {host}"]
    if access.allow_networks:
        lines.append(f"Also admitted networks: {', '.join(access.allow_networks)}")
    if access.allow_hosts:
        lines.append(f"Also accepted host names: {', '.join(access.allow_hosts)}")
    if access.public_url:
        lines.append(f"Public URL: {access.public_url}")
    if access.trusted_proxies:
        lines.append(f"Trusted proxies: {', '.join(access.trusted_proxies)}")
    if access.direct_tls:
        lines.append(f"TLS certificate: {access.tls_cert}")
        lines.append(f"TLS key: {access.tls_key}")
    return lines


def _answer(prompt: str) -> str | None:
    try:
        return input(prompt).strip()
    except EOFError, KeyboardInterrupt:
        return None


def _choose_removal(data: Path | None) -> str | None:
    """Ask what to remove; ``None`` cancels."""
    if data is None:
        print("This Desktop Client owns no local server or server data directory.")
        choices = {"1": "app-only"}
        print("  1) Application only")
        print("  2) Cancel")
        cancel = "2"
    else:
        choices = {"1": "app-only", "2": "data-only", "3": "all"}
        print("What do you want to remove?")
        print(f"  1) Application only (keep data at {data})")
        print(f"  2) Data only (reset {data}, keep the application)")
        print("  3) Application and data")
        print("  4) Cancel")
        cancel = "4"
    while True:
        answer = _answer(f"Selection [{cancel}]: ")
        if answer is None or answer in {"", cancel, "q", "quit", "cancel"}:
            return None
        if answer in choices:
            return choices[answer]
        print(f"Enter {', '.join([*choices, cancel])}.")


def _confirm_removal(mode: str, data: Path | None) -> bool:
    if mode == "app-only":
        return _answer("Remove the vBot application and keep its data? Type YES to continue: ") == (
            "YES"
        )
    print("WARNING: This permanently deletes settings, credentials, Agents, and Sessions.")
    print(f"Data directory: {data}")
    return _answer("Type DELETE to continue: ") == "DELETE"
