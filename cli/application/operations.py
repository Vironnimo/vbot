"""Dispatch and observe updates without owning the worker's lifetime."""

from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import psutil  # type: ignore[import-untyped]

from cli.application.state import (
    ApplicationError,
    Installation,
    Operation,
    contained,
    create_operation,
    ensure_not_removing,
    exclusive,
    load_operation,
    operations,
)
from core.utils.processes import subprocess_creation_flags
from core.utils.server_control import process_started

#: Set only for a tray host started by its predecessor's restart handoff.
HOST_SUCCESSOR_ENV = "VBOT_HOST_SUCCESSOR"


def child_environment(install: Installation) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("VBOT_RUN_")
        and key
        not in {
            "VBOT_UPDATE_HANDOFF",
            HOST_SUCCESSOR_ENV,
            "PYTHONPATH",
            "PYTHONHOME",
            "VIRTUAL_ENV",
            "VBOT_DATA_DIR",
        }
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
        started = process_started(process)
        return bool(abs(started - operation.worker_created) < 0.001 and process.is_running())
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
        operation.worker_created = process_started(psutil.Process(process.pid))
    except (AttributeError, psutil.Error) as exc:
        raise ApplicationError(
            "The independent update process identity could not be verified"
        ) from exc
    operation.save(install)


def _claimed_handoff(install: Installation, handoff_token: str | None) -> str | None:
    """Exchange a Bash call's handoff token for the server's durable ticket path."""
    if not handoff_token:
        return None
    if not install.owns_server or install.server_data_directory is None:
        raise ApplicationError("An Agent handoff cannot target a client-only installation")
    from cli.application.processes import target
    from cli.rpc_client import rpc_call

    result = rpc_call(
        target(install), "application.update_handoff_mint", {"handoff_token": handoff_token}
    )
    ticket = result.data.get("handoff_ticket") if result.ok else None
    if not isinstance(ticket, str) or not ticket:
        detail = (result.message or "the server returned no handoff ticket").splitlines()[0]
        raise ApplicationError(
            "No update was started: the vBot server did not accept this command's "
            f"VBOT_UPDATE_HANDOFF value ({detail}). The value is valid only while the Bash "
            "Tool call that started this command is still running on this installation's "
            "server. Run the command again directly in a new Bash Tool call."
        )
    return _normalized_handoff(install, ticket)


def _normalized_handoff(install: Installation, handoff_ticket: str) -> str:
    assert install.server_data_directory is not None
    from core.tools._bash_update_handoff import read_handoff_ticket, ticket_id_from_path

    data_directory = Path(install.server_data_directory)
    try:
        read_handoff_ticket(data_directory, ticket_id_from_path(data_directory, handoff_ticket))
    except ValueError as exc:
        raise ApplicationError(
            "The server returned an Agent handoff ticket that is not valid for this "
            "installation's data directory"
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
    handoff_token: str | None = None,
) -> Operation:
    if package is not None and not package.expanduser().is_file():
        raise ApplicationError("The selected application package does not exist")
    # Claiming is idempotent per token, so a repeated Agent request coalesces below.
    handoff_ticket = _claimed_handoff(install, handoff_token)
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


class OperationObserver:
    """Report the newest operation, rereading only records whose file changed.

    Each record is replaced atomically, so a changed modification time or size
    identifies exactly the records to validate again; a long-running observer
    stays as cheap as one directory listing.
    """

    def __init__(self, install: Installation) -> None:
        self._install = install
        self._lock = threading.Lock()
        self._directory: Path | None = None
        self._records: dict[str, tuple[tuple[int, int], Operation | ApplicationError]] = {}

    def latest(self) -> Operation | None:
        """Return the newest operation, raising like :func:`status` for invalid records."""

        with self._lock:
            # The managed directory is verified once; changed records are
            # still loaded through the verifying reader.
            if self._directory is None:
                self._directory = contained(self._install.root, "operations")
            directory = self._directory
            try:
                entries = [
                    entry
                    for entry in os.scandir(directory)
                    if entry.name.endswith(".json") and entry.is_file(follow_symlinks=False)
                ]
            except FileNotFoundError:
                entries = []
            records: dict[str, tuple[tuple[int, int], Operation | ApplicationError]] = {}
            for entry in entries:
                identifier = entry.name.removesuffix(".json")
                stat = entry.stat(follow_symlinks=False)
                signature = (stat.st_mtime_ns, stat.st_size)
                cached = self._records.get(identifier)
                if cached is None or cached[0] != signature:
                    try:
                        cached = (signature, load_operation(self._install, identifier))
                    except ApplicationError as exc:
                        cached = (signature, exc)
                records[identifier] = cached
            self._records = records
        latest: Operation | None = None
        for _signature, record in records.values():
            if isinstance(record, ApplicationError):
                raise record
            if latest is None or record.created_at > latest.created_at:
                latest = record
        return latest


def result_summary(install: Installation, operation: Operation) -> str:
    """Describe a finished operation in one human sentence."""

    if operation.phase == "completed":
        if (
            operation.candidate_version
            and operation.candidate_version == operation.previous_version
        ):
            return "vBot is already up to date; no restart was needed."
        if not install.owns_server:
            return "Update completed — an open Desktop switches to the new version by restarting."
        if operation.server_was_running:
            return "Update completed — server restarted and passed its health check."
        return "Update completed — the server remains stopped."
    return {
        "prepared": "Update prepared. The active version has not changed.",
        "failed": "vBot update failed.",
        "rolled_back": "Update failed. The previous version was restored.",
        "needs_attention": "Update needs attention. Check its status before trying again.",
    }.get(operation.phase, "Update accepted; running in the background.")


def public_result(operation: Operation) -> dict[str, Any]:
    return {
        "operation_id": operation.id,
        "phase": operation.phase,
        "message": operation.message,
        "complete": operation.terminal,
        "successful": operation.phase == "completed",
        "previous_version": operation.previous_version,
        "candidate_version": operation.candidate_version,
        "previous_label": operation.previous_label,
        "target_label": operation.target_label,
        "error": operation.error,
        "status_command": f"vbot update status {operation.id}",
    }


def wait(install: Installation, operation_id: str, *, progress=None) -> Operation:
    last = None
    while True:
        operation = load_operation(install, operation_id)
        current = (operation.phase, operation.message, operation.target_label)
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
