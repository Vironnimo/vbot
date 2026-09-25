"""Restartable update transaction. Never a child owned by the vBot Runtime.

Data safety: after the previous server stopped, the worker takes the
pre-update data snapshot offline and keeps it in memory, bound to this
operation. If the candidate then fails its verification start, the snapshot is
restored automatically only when the kernel proves nothing but the candidate
wrote since (``core.database.update_rollback``) and no other server runs on the
data directory. Otherwise nothing is restored and the outcome says so. A
snapshot is never restored after the candidate verified, nor while recovering
an interrupted operation.
"""

from __future__ import annotations

import argparse
import logging
import os
import time
from pathlib import Path

import psutil  # type: ignore[import-untyped]

from cli.application import processes
from cli.application.integration import refresh_gui_entrypoints
from cli.application.packages import (
    download_release,
    stage_package,
    validate_release,
    version_label,
)
from cli.application.state import (
    ApplicationError,
    Installation,
    Operation,
    exclusive,
    load_installation,
    load_operation,
    read_json,
)
from cli.rpc_client import rpc_call
from cli.server_management import probe_health
from core.database import (
    DatabaseError,
    UpdateRollbackRefusedError,
    UpdateSnapshot,
    create_update_snapshot,
    data_changed_since,
    find_update_snapshot,
    read_maintenance,
    restore_update_snapshot,
)
from core.utils.server_control import live_server_ports, read_server_control

_LOGGER = logging.getLogger("vbot.application.update")
#: How long a just-stopped server's lifetime claim may take to disappear.
_CLAIM_SETTLE_SECONDS = 10.0


def control(install: Installation, method: str, operation: Operation, **extra):
    instance = processes.target(install)
    record = read_server_control(instance.data_dir, instance.port)
    if record is None:
        raise ApplicationError("The exact server control record is unavailable")
    result = rpc_call(
        instance,
        f"application.{method}",
        {"control_token": record.token, "operation_id": operation.id, **extra},
    )
    if not result.ok:
        raise ApplicationError(f"Server maintenance failed: {result.message}")
    return result.data


def wait_for_handoff(install: Installation, operation: Operation) -> str | None:
    if operation.handoff_ticket is None:
        return None
    if not install.owns_server:
        raise ApplicationError("An Agent handoff cannot target a client-only installation")
    assert install.server_data_directory is not None
    from core.tools._bash_update_handoff import read_handoff_ticket, ticket_id_from_path

    # The server validates the same capability before permitting any exemption.
    ticket_path = Path(operation.handoff_ticket)
    ticket_id = ticket_id_from_path(Path(install.server_data_directory), ticket_path)
    deadline = time.monotonic() + 300
    while True:
        ticket = read_handoff_ticket(Path(install.server_data_directory), ticket_id)
        if ticket.get("acknowledged") is True:
            return ticket_id
        if time.monotonic() >= deadline:
            raise ApplicationError(
                "The originating Tool result was not durably acknowledged; update was not activated"
            )
        time.sleep(0.25)


def quiesce(install: Installation, operation: Operation) -> None:
    operation.transition(install, "waiting_for_idle", "Waiting for accepted work to finish")
    ticket = wait_for_handoff(install, operation)
    extra = {"handoff_ticket_id": ticket} if ticket else {}
    if ticket:
        # Arm the exact Session continuation before requesting cancellation of
        # its originating Run. A failure after cancellation can then still be
        # reported on the next process start.
        control(install, "update_continuation", operation, handoff_ticket_id=ticket)
    control(install, "maintenance_begin", operation, **extra)
    while True:
        result = control(install, "maintenance_status", operation)
        if result.get("safe_to_stop") is True:
            break
        time.sleep(1)


def require_ok(result) -> None:
    if not result.ok:
        raise ApplicationError(result.message)


def _data_dir(install: Installation) -> Path:
    return Path(processes.target(install).data_dir)


def _live_servers(data_dir: Path) -> str | None:
    """Describe servers still claiming ``data_dir`` once a just-stopped one let go.

    Windows may release the lifetime claim of an exited process with a short
    delay, so a held claim is re-checked for ``_CLAIM_SETTLE_SECONDS``. A
    check that fails counts as a running server.
    """
    deadline = time.monotonic() + _CLAIM_SETTLE_SECONDS
    while True:
        try:
            ports = live_server_ports(data_dir)
        except OSError as exc:
            return f"the servers running on the data directory could not be checked ({exc})"
        if not ports:
            return None
        if time.monotonic() >= deadline:
            return (
                "a vBot server is running on the data directory (port "
                + ", ".join(str(port) for port in ports)
                + ")"
            )
        time.sleep(0.25)


def take_update_snapshot(install: Installation, operation: Operation) -> UpdateSnapshot | None:
    """Snapshot the stopped server's data for this operation, or raise ``ApplicationError``."""
    data_dir = _data_dir(install)
    running = _live_servers(data_dir)
    if running is not None:
        raise ApplicationError(f"The data snapshot needs a stopped server, but {running}")
    try:
        return create_update_snapshot(data_dir, operation_id=operation.id)
    except (DatabaseError, OSError, ValueError) as exc:
        raise ApplicationError(f"The pre-update data snapshot failed: {exc}") from exc


def data_fence(install: Installation, snapshot: UpdateSnapshot | None) -> str | None:
    """Why the candidate must not start on this data, or ``None``."""
    data_dir = _data_dir(install)
    running = _live_servers(data_dir)
    if running is not None:
        return running
    if snapshot is None:
        return None
    try:
        change = data_changed_since(data_dir, snapshot)
    except DatabaseError as exc:
        return f"the data could not be compared with the pre-update snapshot: {exc}"
    return None if change is None else f"the data changed after the pre-update snapshot: {change}"


def keep_previous(install: Installation, operation: Operation, reason: str) -> None:
    """End an update whose candidate never started; the previous version stays active."""
    operation.error = reason
    if install.owns_server and operation.server_was_running:
        require_ok(processes.start(install, version_id=operation.previous_version, breakaway=False))
    operation.transition(
        install,
        "failed",
        f"{reason[:1].upper()}{reason[1:]}. The update was not activated; "
        "the previous version is still active",
    )


def roll_back_data(
    install: Installation, operation: Operation, snapshot: UpdateSnapshot | None
) -> str:
    """Restore the pre-update data after a failed candidate when that is provably safe.

    Returns a sentence for the operation message. Raises ``ApplicationError``
    when a restore started but did not complete; the maintenance guard then
    keeps every version from opening the half-restored data.
    """
    if snapshot is None:
        return "No database was registered, so no data snapshot was restored."
    snapshot_id = snapshot.snapshot_id
    not_restored = (
        f"The data snapshot {snapshot_id} was not restored automatically: {{reason}}; "
        "the data may still hold changes made by the new version."
    )
    data_dir = _data_dir(install)
    running = _live_servers(data_dir)
    if running is not None:
        return not_restored.format(reason=running)
    try:
        change = data_changed_since(data_dir, snapshot)
    except DatabaseError:
        change = "the data could not be compared"
    if change is None:
        return f"The new version left the data unchanged; snapshot {snapshot_id} was not needed."
    try:
        restore_update_snapshot(data_dir, snapshot)
    except UpdateRollbackRefusedError as exc:
        return not_restored.format(reason=str(exc))
    except (DatabaseError, OSError, ValueError) as exc:
        raise ApplicationError(
            f"The new version failed its startup check ({operation.error}), and restoring "
            f"the data snapshot {snapshot_id} did not complete: {exc}. Run "
            f"`vbot data-store snapshot restore {snapshot_id} --all --yes` before starting vBot"
        ) from exc
    return f"The data was restored from snapshot {snapshot_id}, taken before the update."


def _interrupted_restore(install: Installation, operation: Operation) -> str | None:
    """Describe a data restore this operation left incomplete, if any."""
    if not install.owns_server:
        return None
    data_dir = _data_dir(install)
    try:
        guard = read_maintenance(data_dir)
    except DatabaseError as exc:
        return f"The data maintenance guard cannot be read: {exc}"
    if guard is None:
        return None
    message = (
        f"Data maintenance ({guard.operation}) is incomplete, so no version can open the data. "
        "Inspect it with `vbot data-store status`"
    )
    snapshot_id = find_update_snapshot(data_dir, operation.id)
    if guard.operation == "restore" and snapshot_id is not None:
        message += (
            "; if this update's automatic data rollback was interrupted, finish it with "
            f"`vbot data-store snapshot restore {snapshot_id} --all --yes`"
        )
    return message


def recover_interrupted(install: Installation, operation: Operation) -> bool:
    """Never blindly replay activation after losing the worker mid-transaction.

    Never restores a data snapshot: after a lost worker, nothing proves the
    data was written only by the candidate.
    """
    if operation.phase not in {"stopping", "activating", "verifying"}:
        return False
    if operation.phase in {"stopping", "activating"}:
        interrupted = _interrupted_restore(install, operation)
        if interrupted is not None:
            operation.transition(install, "needs_attention", interrupted)
            return True
    active = install.version().name
    candidate = operation.candidate_version
    if operation.phase == "verifying":
        if candidate is None or active != candidate:
            operation.transition(
                install,
                "needs_attention",
                "The active version does not match the interrupted update; "
                "preserved state needs inspection",
            )
            return True
        if (
            operation.server_was_running
            and install.owns_server
            and not processes.running_server_matches(
                install, version_id=candidate, verification=False
            )
        ):
            operation.transition(
                install,
                "needs_attention",
                "The candidate is active but its normal server startup is not proven",
            )
            return True
        validate_release(
            install.version(candidate), shape=install.install_shape, remove_bytecode_caches=True
        )
        from cli.application.customize import finalize_activation

        finalize_activation(install, candidate)
        refresh_gui_entrypoints(install)
        operation.transition(
            install, "completed", "Recovered the verified active application update"
        )
        return True
    if active == operation.previous_version:
        if install.owns_server and processes.running_server_matches(
            install, version_id=operation.candidate_version, verification=True
        ):
            require_ok(processes.stop(install))
        previous_server_running = install.owns_server and processes.running_server_matches(
            install, version_id=operation.previous_version, verification=False
        )
        if operation.server_was_running and install.owns_server and not previous_server_running:
            require_ok(
                processes.start(install, version_id=operation.previous_version, breakaway=False)
            )
        elif operation.server_was_running and previous_server_running:
            # A crash between recording ``stopping`` and stop() leaves the old
            # server alive in maintenance. Release the exact operation before
            # making this rollback terminal so normal admission resumes.
            control(install, "maintenance_end", operation)
        operation.transition(
            install, "rolled_back", "Interrupted activation retained the previous version"
        )
        return True
    operation.transition(
        install,
        "needs_attention",
        "Interrupted activation requires inspection; versions and data have been preserved",
    )
    return True


def execute(install: Installation, operation: Operation) -> None:
    if operation.terminal:
        return
    if recover_interrupted(install, operation):
        return
    operation.previous_label = version_label(
        read_json(install.version() / "release.json", limit=32 * 1024**2)
    )

    def progress(message: str, target: str | None = None) -> None:
        if target is not None:
            operation.target_label = target
        operation.transition(install, "preparing", message)

    progress("Checking for updates")
    if operation.package:
        progress("Unpacking and verifying the selected package")
        candidate = stage_package(install, Path(operation.package), local=operation.local_package)
    else:
        from cli.application.source_updates import prepare_update, read_binding

        binding = read_binding(install)
        if binding is None:
            archive = download_release(install, operation.id, progress=progress)
            progress("Unpacking and verifying the downloaded package")
            candidate = stage_package(install, archive, local=False)
        else:
            candidate = prepare_update(install, operation.id, progress=progress)
    operation.candidate_version = candidate
    operation.target_label = version_label(
        read_json(install.version(candidate) / "release.json", limit=32 * 1024**2)
    )
    operation.save(install)
    # Local changes are reconciled before touching the running installation.
    from cli.application.customize import carry_forward

    candidate = carry_forward(install, candidate)
    from cli.application.dependencies import prepare_for_version

    progress("Checking Extension dependencies")
    prepare_for_version(install, candidate)
    operation.candidate_version = candidate
    operation.target_label = version_label(
        read_json(install.version(candidate) / "release.json", limit=32 * 1024**2)
    )
    operation.save(install)
    if install.version().name == candidate:
        refresh_gui_entrypoints(install)
        operation.transition(
            install, "completed", "vBot is already up to date; no restart was needed"
        )
        return
    if not operation.restart:
        operation.transition(
            install, "prepared", "New version prepared; the active version has not changed"
        )
        return
    update_snapshot: UpdateSnapshot | None = None
    if install.owns_server:
        current_health = probe_health(processes.target(install))
        operation.server_was_running = processes.running_server_matches(
            install, version_id=operation.previous_version, verification=False
        )
        if current_health.reachable and not operation.server_was_running:
            raise ApplicationError(
                "The server target is occupied by a different application version or startup mode"
            )
        operation.save(install)
        if operation.server_was_running:
            quiesce(install, operation)
        operation.transition(
            install, "stopping", "Stopping the current server after draining accepted work"
        )
        require_ok(processes.stop(install))
        # Taken only now: no server can write between this snapshot and a rollback.
        operation.transition(install, "stopping", "Saving a recovery snapshot of the data")
        try:
            update_snapshot = take_update_snapshot(install, operation)
        except ApplicationError as exc:
            keep_previous(install, operation, str(exc))
            return
    operation.transition(install, "activating", "Checking the prepared version before activation")
    if install.owns_server:
        fence = data_fence(install, update_snapshot)
        if fence is not None:
            keep_previous(install, operation, f"The new version was not started: {fence}")
            return
        # No producer/user admission in verification mode. The candidate is the
        # only writer until it stops, so its failure may restore the snapshot.
        # Dispatch already detached this worker from vBot's lifetime Job. It
        # owns no Job itself, so server children outlive it without requesting
        # a second breakaway from an unrelated ambient Windows Job.
        verified = processes.start(
            install, version_id=candidate, verification=True, breakaway=False
        )
        if verified.ok:
            # From here on the snapshot is stale: it is never restored automatically.
            require_ok(processes.stop(install))
        else:
            operation.error = verified.message
            data_note = roll_back_data(install, operation, update_snapshot)
            # Old code is not run against the data without a probe.
            previous = operation.previous_version
            old_check = processes.start(
                install, version_id=previous, verification=True, breakaway=False
            )
            if old_check.ok:
                require_ok(processes.stop(install))
                if operation.server_was_running:
                    require_ok(processes.start(install, version_id=previous, breakaway=False))
                operation.transition(
                    install,
                    "rolled_back",
                    f"Candidate startup failed. {data_note} "
                    "The previous version startup was verified and kept",
                )
                return
            raise ApplicationError(
                f"Candidate and previous version could not open the data. {data_note} "
                "Recovery needs attention"
            )
    install.activate(candidate)
    operation.transition(install, "verifying", "Verifying the active application version")
    if install.owns_server and operation.server_was_running:
        require_ok(processes.start(install, breakaway=False))
    validate_release(install.version(), shape=install.install_shape, remove_bytecode_caches=True)
    from cli.application.customize import finalize_activation

    finalize_activation(install, candidate)
    refresh_gui_entrypoints(install)
    operation.transition(
        install,
        "completed",
        "Desktop client updated; reopen Desktop to use the new version"
        if not install.owns_server
        else "Application update completed and startup verified"
        if operation.server_was_running
        else "Application update completed; server remains stopped",
    )


def run(install: Installation, operation_id: str) -> None:
    with exclusive(install.root, "dispatch", timeout=5):
        operation = load_operation(install, operation_id)
        if operation.terminal:
            return
        current_pid = os.getpid()
        current_created = psutil.Process().create_time()
        if operation.worker_pid is not None and (
            operation.worker_pid != current_pid
            or operation.worker_created is None
            or abs(operation.worker_created - current_created) >= 0.001
        ):
            return
        operation.worker_pid = current_pid
        operation.worker_created = current_created
        operation.save(install)
    try:
        # This lifetime lock also excludes manual lifecycle/customization mutations.
        # Dispatch uses its separate short lock so callers can observe/coalesce work.
        with exclusive(install.root, "operation", timeout=3600):
            execute(install, operation)
    except Exception as exc:
        _LOGGER.exception("Application update failed for %s", operation.id)
        operation.error = str(exc)
        attention = operation.phase in {"stopping", "activating", "verifying"}
        operation.transition(
            install,
            "needs_attention" if attention else "failed",
            "Update did not complete; inspect the saved error and preserved versions",
        )
        if install.owns_server:
            try:
                control(install, "maintenance_end", operation)
            except Exception:
                _LOGGER.warning("Could not release server maintenance for %s", operation.id)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Independent vBot update worker")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--operation", required=True)
    args = parser.parse_args(argv)
    install = load_installation(args.root)
    from core.utils.logging import LogManager

    manager = LogManager(data_dir=install.root, enable_console=False)
    try:
        run(install, args.operation)
    finally:
        manager.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
