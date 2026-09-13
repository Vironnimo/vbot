"""Dispatch and observe updates without owning the worker's lifetime."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any

import psutil  # type: ignore[import-untyped]

from cli.application.state import (
    ApplicationError,
    Installation,
    Operation,
    create_operation,
    ensure_not_removing,
    exclusive,
    load_operation,
    operations,
)
from core.utils.processes import subprocess_creation_flags


def child_environment(install: Installation) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("VBOT_RUN_")
        and key
        not in {"VBOT_UPDATE_HANDOFF", "PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "VBOT_DATA_DIR"}
    }
    environment["VBOT_INSTALL_ROOT"] = str(install.root)
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def worker_alive(operation: Operation) -> bool:
    if type(operation.worker_pid) is not int or not isinstance(
        operation.worker_created, (float, int)
    ):
        return False
    try:
        process = psutil.Process(operation.worker_pid)
        return bool(
            abs(process.create_time() - operation.worker_created) < 0.001 and process.is_running()
        )
    except psutil.Error:
        return False


def spawn_worker(install: Installation, operation: Operation) -> None:
    executable = install.interpreter(role="Update")
    args = [
        str(executable),
        "-m",
        "cli.application.worker",
        "--root",
        str(install.root),
        "--operation",
        operation.id,
    ]
    startup_log = install.root / "logs" / f"{operation.id}-startup.log"
    startup_log.parent.mkdir(parents=True, exist_ok=True)
    with startup_log.open("ab") as output:
        process = subprocess.Popen(
            args,
            cwd=install.version() / "app",
            env=child_environment(install),
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            creationflags=subprocess_creation_flags(new_process_group=True, breakaway=True),
            start_new_session=os.name != "nt",
        )
    # Dispatch lock keeps the worker from claiming before this identity is saved.
    try:
        process.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        pass
    else:
        raise ApplicationError(f"The update process could not start. Details: {startup_log}")
    try:
        operation.worker_pid = process.pid
        operation.worker_created = psutil.Process(process.pid).create_time()
    except (AttributeError, psutil.Error) as exc:
        raise ApplicationError(
            "The independent update process identity could not be verified"
        ) from exc
    operation.save(install)


def _normalized_handoff(install: Installation, handoff_ticket: str | None) -> str | None:
    if handoff_ticket is None:
        return None
    if not install.owns_server or install.server_data_directory is None:
        raise ApplicationError("An Agent handoff cannot target a client-only installation")
    from core.tools._bash_update_handoff import ticket_id_from_path

    try:
        ticket_id_from_path(Path(install.server_data_directory), handoff_ticket)
    except ValueError as exc:
        raise ApplicationError(
            "The Agent handoff ticket is outside this server data directory"
        ) from exc
    return str(Path(handoff_ticket).expanduser().resolve())


def _equivalent_request(
    operation: Operation,
    *,
    package: Path | None,
    restart: bool,
    handoff_ticket: str | None,
) -> bool:
    requested_package = str(package.expanduser().resolve()) if package is not None else None
    return (
        operation.package == requested_package
        and operation.restart is restart
        and operation.handoff_ticket == handoff_ticket
    )


def request_update(
    install: Installation,
    *,
    package: Path | None = None,
    restart: bool = True,
    handoff_ticket: str | None = None,
) -> Operation:
    if package is not None and not package.expanduser().is_file():
        raise ApplicationError("The selected application package does not exist")
    handoff_ticket = _normalized_handoff(install, handoff_ticket)
    with exclusive(install.root, "dispatch", timeout=5):
        ensure_not_removing(install.root)
        pending = [item for item in operations(install) if not item.terminal]
        if pending:
            operation = pending[0]
            if not _equivalent_request(
                operation,
                package=package,
                restart=restart,
                handoff_ticket=handoff_ticket,
            ):
                raise ApplicationError(
                    f"Update {operation.id} is already in progress with different options; "
                    f"inspect it with vbot update status {operation.id}"
                )
            if not worker_alive(operation):
                spawn_worker(install, operation)
            return operation
        operation = create_operation(
            install,
            package=str(package.expanduser().resolve()) if package else None,
            local_package=package is not None,
            restart=restart,
            previous_version=install.version().name,
            handoff_ticket=handoff_ticket,
        )
        try:
            spawn_worker(install, operation)
        except (OSError, ApplicationError) as exc:
            operation.error = str(exc)
            operation.transition(install, "failed", "Could not start the independent updater")
            raise
        return operation


def recover_operations(install: Installation) -> None:
    with exclusive(install.root, "dispatch", timeout=5):
        ensure_not_removing(install.root)
        for operation in operations(install):
            if not operation.terminal and not worker_alive(operation):
                spawn_worker(install, operation)
                return


def status(install: Installation, operation_id: str | None = None) -> Operation | None:
    if operation_id:
        return load_operation(install, operation_id)
    records = operations(install)
    return records[0] if records else None


def public_result(operation: Operation) -> dict[str, Any]:
    return {
        "operation_id": operation.id,
        "phase": operation.phase,
        "message": operation.message,
        "complete": operation.terminal,
        "successful": operation.phase == "completed",
        "previous_version": operation.previous_version,
        "candidate_version": operation.candidate_version,
        "error": operation.error,
        "status_command": f"vbot update status {operation.id}",
    }


def wait(install: Installation, operation_id: str, *, progress=None) -> Operation:
    last = None
    while True:
        operation = load_operation(install, operation_id)
        current = (operation.phase, operation.message)
        if current != last and progress is not None:
            progress(operation)
        last = current
        if operation.terminal:
            return operation
        if (
            not worker_alive(operation)
            and (
                time.time()
                - Path(install.root / "operations" / f"{operation.id}.json").stat().st_mtime
            )
            > 10
        ):
            recover_operations(install)
        time.sleep(0.5)
