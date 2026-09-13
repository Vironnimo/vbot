"""Human CLI rendering of the shared application operation contract."""

from __future__ import annotations

import argparse
import json
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
    install = commands.add_parser("install", help="Install a prepared application payload")
    install.add_argument("--root", type=Path, required=True)
    install.add_argument("--payload", type=Path, required=True)
    install.add_argument(
        "--shape", choices=("server", "server-desktop", "desktop-client"), required=True
    )
    install.add_argument("--host", default="127.0.0.1")
    install.add_argument("--port", type=int, default=8420)
    install.add_argument("--data-dir", type=Path)
    install.add_argument("--public-key", default="")
    install.add_argument("--from-checkout", type=Path)
    commands.add_parser("status", help="Show the installed version and latest update")
    source = commands.add_parser(
        "source", help="Select release or main updates for this installation"
    )
    source.add_argument("source_track", choices=("main", "release"))
    source.add_argument(
        "--from-checkout", type=Path, help="Retain an existing Git branch as the update source"
    )
    commands.add_parser("tray", help="Run the vBot tray application")
    commands.add_parser("exit", help="Stop the owned tray application cleanly")
    commands.add_parser("removal-begin", help="Guard the native uninstaller's removal phase")
    commands.add_parser("removal-reset", help="Clear an interrupted removal after its owner exits")
    dependencies = commands.add_parser("dependencies", help="Manage extra Extension dependencies")
    dependencies.add_argument("dependency_action", choices=("status", "install"))
    dependencies.add_argument(
        "--requirements", type=Path, help="Complete replacement requirements recipe"
    )
    custom = subparsers.add_parser(
        "customize", help="Prepare, check and activate local vBot features"
    )
    actions = custom.add_subparsers(dest="command", required=True)
    prepare = actions.add_parser(
        "prepare", help="Create a development copy of the exact installed version"
    )
    prepare.add_argument("--source", type=Path, help="Use an existing source repository")
    actions.add_parser("status", help="Show local customization state")
    check = actions.add_parser(
        "check", help="Validate local changes and prepare an application candidate"
    )
    check.add_argument(
        "--intent", required=True, help="Describe the intended behavior of these changes"
    )
    activate = actions.add_parser(
        "activate", help="Activate the previously checked local candidate"
    )
    activate.add_argument("--detach", action="store_true")
    rebase = actions.add_parser("rebase", help="Check a manually resolved update reconciliation")
    rebase.add_argument("--intent", required=True)
    test = actions.add_parser(
        "test", help="Start an isolated test instance without external producers"
    )
    test.add_argument(
        "--port", type=int, default=0, help="Test port; omitted selects a free local port"
    )


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
    from cli._output import print_update_command_start
    from cli.application import operations
    from cli.update_management import read_checkout_version

    if output_mode.get() == "plain":
        return operations.wait(install, operation.id)
    print_update_command_start(read_checkout_version(install.version() / "app"))
    with ProgressPrinter() as progress:
        progress.track("Waiting for the update to start")

        def report(value: Operation) -> None:
            if not value.terminal and value.phase != "queued":
                progress.emit("busy", value.message)

        return operations.wait(install, operation.id, progress=report)


def dispatch(args: argparse.Namespace) -> int | None:
    if args.area == "application" and args.command == "install":
        from cli.application.install import install_payload

        installed = install_payload(
            args.root,
            args.payload,
            shape=args.shape,
            host=args.host,
            port=args.port,
            data_dir=args.data_dir,
            public_key=args.public_key,
            from_checkout=args.from_checkout,
        )
        _print(
            {"installed": True, "root": str(installed.root), "version": installed.version().name},
            lines=[f"vBot installed at {installed.root}."],
        )
        return 0
    install = discover()
    if install is None:
        if args.area in {"application", "customize"} or (
            args.area == "update"
            and (
                getattr(args, "update_action", None)
                or getattr(args, "detach", False)
                or getattr(args, "package", None)
            )
        ):
            raise ApplicationError(
                "This command requires a packaged vBot installation; "
                "source-checkout updates retain their existing workflow"
            )
        return None
    from cli.application import operations, processes

    if args.area == "application":
        if args.command == "source":
            from cli.application.source_updates import select_source
            from cli.application.state import operations as saved_operations

            with exclusive(install.root, "dispatch"), exclusive(install.root):
                if any(not item.terminal for item in saved_operations(install)):
                    raise ApplicationError("Wait for the pending update before changing its source")
                selected = select_source(
                    install, args.source_track, from_checkout=args.from_checkout
                )
                label = (
                    f"branch {selected.get('branch', 'main')}"
                    if args.source_track == "main"
                    else "published releases"
                )
                _print(
                    {**selected, "next_command": "vbot update"},
                    lines=[f"Updates now follow {label}.", "Apply an update with: vbot update"],
                )
            return 0
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
        from cli.application.source_updates import read_binding

        source = read_binding(install)
        _print(
            {
                "root": str(install.root),
                "version": install.version().name,
                "shape": install.install_shape,
                "update_source": {"kind": "checkout", **source} if source else {"kind": "release"},
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
    if args.area in {"server", "update", "autostart", "uninstall"} and (
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
        from cli.application.integration import autostart

        if args.task_name or args.service_name:
            raise ApplicationError(
                "Packaged Autostart uses this installation's owned logon registration"
            )
        registration = autostart(install, args.command)
        _print(
            registration,
            lines=[f"Autostart: {'enabled' if registration['enabled'] else 'disabled'}."],
        )
        return 0
    if args.area == "uninstall":
        from cli.application.integration import uninstall
        from cli.uninstall_management import (
            UninstallMode,
            UninstallResult,
            _choose_mode,
            _confirm_mode,
        )

        data = Path(install.server_data_directory) if install.server_data_directory else None
        if args.uninstall_mode is None:
            if not sys.stdin.isatty():
                raise ApplicationError("Select --app-only, --data-only or --all explicitly")
            print(f"Application: {install.root}")
            choice = _choose_mode(data, input_fn=input, output_fn=print)
            if isinstance(choice, UninstallResult):
                print(choice.message)
                return 0 if choice.ok else 1
            mode = choice.value
        else:
            mode = args.uninstall_mode
        if mode in {"all", "data-only"} and data is None:
            raise ApplicationError("This Desktop Client does not own server data")
        if not args.yes:
            if not sys.stdin.isatty():
                raise ApplicationError("Use --yes to confirm the explicitly selected removal mode")
            confirmed = _confirm_mode(UninstallMode(mode), data, input_fn=input, output_fn=print)
            if confirmed is not None:
                print(confirmed.message)
                return 0 if confirmed.ok else 1
        removal = uninstall(install, remove_data=mode == "all", data_only=mode == "data-only")
        lines = (
            [
                "vBot data has been reset. The application is still installed.",
                "Server: restarted." if removal["server_restarted"] else "Server: stopped.",
            ]
            if mode == "data-only"
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
        else:
            with exclusive(install.root, allow_removal=args.command == "stop"):
                if args.command in {"stop", "restart"}:
                    result = processes.stop(install)
                    if not result.ok:
                        print_command_result(args.command, result)
                        return 1
                if args.command in {"start", "restart"}:
                    result = processes.start(install)
        print_command_result(args.command, result)
        return 0 if result.ok else 1
    if args.area == "desktop":
        from cli.application.host import ApplicationFacade

        ApplicationFacade(install).open_desktop(host=args.host, port=args.port)
        print("Desktop launch requested")
        return 0
    if args.area == "update":
        if args.update_action == "status":
            value = operations.status(install, args.operation_id)
            _print(
                operations.public_result(value)
                if value
                else {"message": "No update operation has been recorded"}
            )
            return 0
        if args.discard or args.stash:
            raise ApplicationError(
                "Packaged updates preserve managed local changes automatically; "
                "--discard and --stash apply only to source checkouts"
            )
        if args.operation_id and args.update_action != "activate":
            raise ApplicationError(
                "An operation id is only valid with update status or update activate"
            )
        package: Path | None
        if args.update_action == "activate":
            prepared = operations.status(install, args.operation_id)
            if prepared is None or prepared.phase != "prepared" or not prepared.candidate_version:
                raise ApplicationError("Select a prepared update operation to activate")
            from cli.application.install import archive_payload

            package = archive_payload(install, prepared.candidate_version)
        else:
            package = Path(args.package) if args.package else None
        handoff = os.environ.get("VBOT_UPDATE_HANDOFF") if not args.no_restart else None
        if handoff:
            from core.tools._bash_update_handoff import read_handoff_ticket

            if not install.owns_server:
                # An Agent on another server updating a client has no local Run to resume.
                handoff = None
            else:
                assert install.server_data_directory is not None
                read_handoff_ticket(Path(install.server_data_directory), Path(handoff).stem)
        operation = operations.request_update(
            install, package=package, restart=not args.no_restart, handoff_ticket=handoff
        )
        if args.detach or handoff:
            _print_update_result(install, operation, handoff=bool(handoff))
            return 0
        outcome = _wait_update(install, operation)
        _print_update_result(install, outcome)
        return 0 if outcome.phase in {"completed", "prepared"} else 1
    if args.area == "customize":
        from cli.application import customize

        if args.command == "prepare":
            prepared_source = customize.prepare(install, source=args.source)
            _print(
                {"source": str(prepared_source)},
                lines=[
                    f"Development source: {prepared_source}",
                    "After editing, run: vbot customize check --intent <description>",
                ],
            )
        elif args.command == "status":
            _print(customize.development_state(install) or {"message": "No local changes prepared"})
        elif args.command == "check":
            _print(
                customize.check(install, intent=args.intent),
                lines=[
                    "Your changes passed validation. The active version has not changed.",
                    "Test: vbot customize test",
                    "Activate: vbot customize activate",
                ],
            )
        elif args.command == "rebase":
            _print(
                customize.finish_rebase(install, intent=args.intent),
                lines=[
                    "The resolved changes passed validation. The active version has not changed.",
                    "Activate: vbot customize activate",
                ],
            )
        elif args.command == "activate":
            archive = customize.activation_archive(install)
            operation = operations.request_update(
                install, package=archive, handoff_ticket=os.environ.get("VBOT_UPDATE_HANDOFF")
            )
            if not args.detach and not operation.handoff_ticket:
                operation = _wait_update(install, operation)
            _print_update_result(install, operation, handoff=bool(operation.handoff_ticket))
            return 0 if not operation.terminal or operation.phase == "completed" else 1
        elif args.command == "test":
            customize.run_test_instance(install, port=args.port)
        return 0
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
