"""Dispatch and observe updates without owning the worker's lifetime.

The worker, and every server it starts, must outlive the process that requested
the update. A requester inside another application's Windows Job (a coding
agent's shell, say) cannot give it that: whatever it starts stays in the Job
and ends when that application closes. The tray host runs outside such Jobs, so
a running tray starts the worker of every update another process requests, and
a requester confined to a foreign Job starts a tray outside it first. The same
dispatch request hands a confined ``vbot server start`` or ``restart`` to the
running tray (``request_server``).
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
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
    read_json,
    write_json,
)
from core.utils.outside_job import confining_job, start_outside_job
from core.utils.processes import ForeignJob, outside_service_unit, subprocess_creation_flags
from core.utils.server_control import process_started

#: Set only for a tray host started by its predecessor's restart handoff.
HOST_SUCCESSOR_ENV = "VBOT_HOST_SUCCESSOR"
#: Asks the running tray host to start the worker of the named operation.
DISPATCH_REQUEST = "host-dispatch-request.json"
#: How long a requester waits for the tray host to start the worker it handed over.
HOST_DISPATCH_SECONDS = 30.0
_HOST_DISPATCH_POLL_SECONDS = 0.2
_LOGGER = logging.getLogger("vbot.application.update")
_sleep = time.sleep
#: How long a requester waits for the result of a server start or restart the tray
#: accepted: a stop (up to 35 s), a start (up to 90 s) and a tray action ahead of them.
HOST_SERVER_SECONDS = 300.0
_SERVER_COMMANDS = frozenset({"start", "restart"})
# DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP: the console bootstrap gets no console window.
_TRAY_CREATION_FLAGS = 0x00000208


@dataclass(frozen=True, slots=True)
class ServerRequest:
    """A ``vbot server start`` or ``restart`` (``command``) another process handed to the tray."""

    id: str
    command: str


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
    if os.name != "nt":
        # Native Windows hosts run in UTF-8 mode; a POSIX runtime needs it requested.
        environment["PYTHONUTF8"] = "1"
    return environment


def worker_alive(operation: Operation) -> bool:
    if type(operation.worker_pid) is not int or not isinstance(
        operation.worker_created, (float, int)
    ):
        return False
    try:
        process = psutil.Process(operation.worker_pid)
        return bool(
            abs(process_started(process) - operation.worker_created) < 0.001
            and process.is_running()
        )
    except psutil.Error:
        return False


def spawn_worker(install: Installation, operation: Operation) -> None:
    executable = install.interpreter(role="Update")
    # The worker stops the server; started from inside the server's systemd
    # unit, it leaves the unit so that the stop does not end it too.
    args = outside_service_unit(
        [
            str(executable),
            "-m",
            "cli.application.worker",
            "--root",
            str(install.root),
            "--operation",
            operation.id,
        ]
    )
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
    from core.tools.update_handoff import read_handoff_ticket, ticket_id_from_path

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
    host: bool = False,
) -> Operation:
    """Request an update, or join the equivalent one in progress.

    ``host`` is true only for the tray host, which always starts the worker
    itself. Any other requester hands the worker to a running tray, or to a tray
    it starts outside the foreign Windows Job that confines it (module docstring),
    and returns once that tray started the worker. A confined requester whose
    handoff fails raises ``ApplicationError`` instead of starting the worker.
    """
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
            if worker_alive(operation):
                return operation
            handoff = _dispatch(install, operation, host=host)
        else:
            operation = create_operation(
                install,
                package=str(package.expanduser().resolve()) if package else None,
                local_package=package is not None,
                restart=restart,
                previous_version=install.version().name,
                handoff_ticket=handoff_ticket,
            )
            try:
                handoff = _dispatch(install, operation, host=host)
            except (OSError, ApplicationError) as exc:
                operation.error = str(exc)
                operation.transition(install, "failed", "Could not start the independent updater")
                raise
    return operation if handoff is None else _await_host(install, operation.id, handoff)


def recover_operations(install: Installation, *, host: bool = False) -> None:
    """Start a worker for an unfinished operation whose worker is gone, like ``request_update``."""

    with exclusive(install.root, "dispatch", timeout=5):
        ensure_not_removing(install.root)
        orphan = next(
            (item for item in operations(install) if not item.terminal and not worker_alive(item)),
            None,
        )
        if orphan is None:
            return
        handoff = _dispatch(install, orphan, host=host)
    if handoff is not None:
        _await_host(install, orphan.id, handoff)


def host_running(install: Installation) -> bool:
    """Whether the tray host of this installation runs, in another process than this one."""

    try:
        record = read_json(contained(install.root, "host.json"), limit=4096)
    except ApplicationError:
        return False
    pid, created = record.get("pid"), record.get("process_created")
    if type(pid) is not int or pid == os.getpid() or not isinstance(created, (int, float)):
        return False
    try:
        process = psutil.Process(pid)
        return abs(process_started(process) - created) < 0.001 and bool(process.is_running())
    except psutil.Error:
        return False


def dispatch_requested(install: Installation) -> ServerRequest | None:
    """Take over what another process handed to this tray host (``DISPATCH_REQUEST``).

    An update request starts its worker here. A server request is accepted (its
    requester stops waiting for a tray that ignores it) and returned for the
    tray to run as its own action and answer with ``answer_server_request``.
    The request is consumed first, so a worker that cannot start is not retried
    on every poll; its requester notices and decides (``request_update``).
    """

    with exclusive(install.root, "dispatch", timeout=5):
        request = contained(install.root, DISPATCH_REQUEST)
        try:
            record = read_json(request, limit=4096)
        except ApplicationError:
            if not request.exists():
                return None
            request.unlink(missing_ok=True)
            raise
        request.unlink(missing_ok=True)
        if "server" in record:
            command, request_id = record.get("server"), record.get("request")
            if (
                record.get("schema_version") != 1
                or command not in _SERVER_COMMANDS
                or not _valid_request_id(request_id)
            ):
                raise ApplicationError("Ignored an invalid server dispatch request")
            assert isinstance(command, str) and isinstance(request_id, str)
            write_json(
                _server_result(install, request_id),
                {"schema_version": 1, "request": request_id, "state": "accepted"},
            )
            return ServerRequest(request_id, command)
        operation_id = record.get("operation")
        if record.get("schema_version") != 1 or not isinstance(operation_id, str):
            raise ApplicationError("Ignored an invalid update dispatch request")
        ensure_not_removing(install.root)
        operation = load_operation(install, operation_id)
        if operation.terminal or worker_alive(operation):
            return None
        spawn_worker(install, operation)
    _LOGGER.debug("Update worker started for another requester (operation=%s)", operation.id)
    return None


def request_server(install: Installation, command: str) -> dict[str, Any] | None:
    """Hand ``vbot server <command>`` (start or restart) to the running tray host.

    The tray runs outside the foreign Windows Job that confines the caller, so
    the server it starts outlives the application that owns that Job. Returns
    the tray's result (``ok``, ``message``, optional ``process_id``), or ``None``
    when no tray accepted the request within ``HOST_DISPATCH_SECONDS`` (none
    answers, or a tray of an older version dropped it), so the caller runs the
    command itself. A tray that accepted but reports nothing within
    ``HOST_SERVER_SECONDS`` yields a failed result: it may still be working.
    """

    request_id = uuid.uuid4().hex
    request = contained(install.root, DISPATCH_REQUEST)
    result = _server_result(install, request_id)
    deadline = time.monotonic() + HOST_DISPATCH_SECONDS
    # One request at a time: a pending one is consumed within a tray poll.
    while True:
        with exclusive(install.root, "dispatch", timeout=5):
            if not request.exists():
                write_json(request, {"schema_version": 1, "server": command, "request": request_id})
                break
        if time.monotonic() >= deadline:
            return None
        _sleep(_HOST_DISPATCH_POLL_SECONDS)
    try:
        while not result.exists():
            if not request.exists() or time.monotonic() >= deadline:
                # The tray consumes a request and accepts it under this lock.
                with exclusive(install.root, "dispatch", timeout=5):
                    if result.exists():
                        break
                    if _requested(request, request_id):
                        request.unlink(missing_ok=True)
                    return None
            _sleep(_HOST_DISPATCH_POLL_SECONDS)
        deadline = time.monotonic() + HOST_SERVER_SECONDS
        while True:
            try:
                answer = read_json(result, limit=4096)
            except ApplicationError:
                answer = {}
            if answer.get("state") == "done":
                return answer
            if time.monotonic() >= deadline:
                return {
                    "ok": False,
                    "message": (
                        f"the vBot tray accepted the server {command} but reported no result "
                        f"within {HOST_SERVER_SECONDS:g} seconds; check the server with "
                        "vbot server status"
                    ),
                }
            _sleep(_HOST_DISPATCH_POLL_SECONDS)
    finally:
        result.unlink(missing_ok=True)


def answer_server_request(
    install: Installation,
    request: ServerRequest,
    *,
    ok: bool,
    message: str,
    process_id: int | None = None,
) -> None:
    """Report the outcome of a server request to its waiting requester."""

    write_json(
        _server_result(install, request.id),
        {
            "schema_version": 1,
            "request": request.id,
            "state": "done",
            "ok": ok,
            "message": message,
            "process_id": process_id,
        },
    )


def _server_result(install: Installation, request_id: str) -> Path:
    return contained(install.root, f"host-server-result-{request_id}.json")


def _valid_request_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 32
        and all(character in "0123456789abcdef" for character in value)
    )


def _requested(request: Path, request_id: str) -> bool:
    """Whether the pending dispatch request is still this requester's own."""

    try:
        return read_json(request, limit=4096).get("request") == request_id
    except ApplicationError:
        return False


#: How a requester handed its worker over: to the running tray host (``True``), or
#: to a tray it still starts outside the named foreign Job.
type _Handoff = bool | ForeignJob


def _dispatch(install: Installation, operation: Operation, *, host: bool) -> _Handoff | None:
    """Start the worker here (``None``) or hand it to a tray host; under the dispatch lock."""

    if not host:
        if host_running(install):
            _hand_to_host(install, operation)
            return True
        job = confining_job()
        if job is not None:
            _hand_to_host(install, operation)
            return job
    spawn_worker(install, operation)
    return None


def _hand_to_host(install: Installation, operation: Operation) -> None:
    if operation.worker_pid is not None or operation.worker_created is not None:
        # The previous worker is gone; the tray's worker claims the operation anew.
        operation.worker_pid = operation.worker_created = None
        operation.save(install)
    write_json(
        contained(install.root, DISPATCH_REQUEST), {"schema_version": 1, "operation": operation.id}
    )


def _await_host(install: Installation, operation_id: str, handoff: _Handoff) -> Operation:
    """Wait until a tray host started the handed-over worker, else settle it here."""

    job = handoff if isinstance(handoff, ForeignJob) else None
    if job is not None:
        try:
            _start_host_outside_job(install)
        except OSError as exc:
            return _settle(
                install,
                operation_id,
                job,
                "The vBot tray, which runs the update outside that Job, is not running and "
                f"could not be started ({exc}). Start vBot from the Start menu, then run the "
                "command again.",
            )
        _LOGGER.info(
            "Application tray started outside a foreign Windows Job for an update "
            "(operation=%s job_owner=%s job_owner_pid=%s)",
            operation_id,
            job.name,
            job.pid,
        )
    deadline = time.monotonic() + HOST_DISPATCH_SECONDS
    while True:
        operation = load_operation(install, operation_id)
        if operation.terminal or operation.worker_pid is not None:
            return operation
        if time.monotonic() >= deadline:
            break
        _sleep(_HOST_DISPATCH_POLL_SECONDS)
    return _settle(
        install,
        operation_id,
        job,
        f"The vBot tray did not take the update over within {HOST_DISPATCH_SECONDS:g} "
        "seconds. Restart vBot from the Start menu, then run the command again.",
    )


def _settle(
    install: Installation, operation_id: str, job: ForeignJob | None, reason: str
) -> Operation:
    """Withdraw a handoff no tray took: start the worker here unless a foreign Job confines it.

    A tray of an older version, or one that hangs, ignores the request; outside
    a foreign Job this requester starts the worker as it always did.
    """

    with exclusive(install.root, "dispatch", timeout=5):
        operation = load_operation(install, operation_id)
        if operation.terminal or operation.worker_pid is not None:
            return operation
        contained(install.root, DISPATCH_REQUEST).unlink(missing_ok=True)
        if job is None:
            job = confining_job()
        if job is None:
            _LOGGER.warning(
                "Application tray did not start the update worker; starting it here (operation=%s)",
                operation.id,
            )
            spawn_worker(install, operation)
            return operation
        message = f"No update was started: {_confinement(job)} {reason}"
        if operation.phase == "queued":
            # Nothing ran yet; a tray started later must not run it by surprise.
            operation.error = message
            operation.transition(install, "failed", "The vBot tray did not start the update")
        raise ApplicationError(message)


def _confinement(job: ForeignJob) -> str:
    return (
        f"this command runs inside the Windows Job of {job.name} (pid {job.pid}), so the "
        "update and the vBot server it restarts would end when that application closes."
    )


def _start_host_outside_job(install: Installation) -> None:
    """Start the tray host through WMI, outside every Job of this process (Windows only).

    The tray gets the user's default environment, like one started at sign-in.
    """

    start_outside_job(
        [str(install.bootstrap)], cwd=install.root, creationflags=_TRAY_CREATION_FLAGS
    )


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
