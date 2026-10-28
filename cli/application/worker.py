"""Restartable update transaction. Never a child owned by the vBot Runtime.

Data safety: after the previous server stopped, the worker takes the
pre-update data snapshot offline and keeps it in memory, bound to this
operation. The worker runs the previous version's code, which wrote the data,
so its canonical database declarations record and verify owner facts. If the
candidate then fails its verification start, the snapshot is restored
automatically only when the kernel proves nothing but the candidate wrote
since (``core.database.update_rollback``) and no other server runs on the
data directory. Otherwise nothing is restored and the outcome says so. A
snapshot is never restored after the candidate verified, nor while recovering
an interrupted operation.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from pathlib import Path

import psutil  # type: ignore[import-untyped]

from cli.application import processes
from cli.application.packages import (
    OlderReleaseError,
    download_release,
    require_access_compatible_release,
    require_version_switch_release,
    stage_package,
    validate_release,
    version_label,
)
from cli.application.retention import remove_retired, retire_unneeded
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
from core.database import (
    DatabaseError,
    DatabaseSpec,
    UpdateRollbackRefusedError,
    UpdateSnapshot,
    create_update_snapshot,
    data_changed_since,
    find_update_snapshot,
    read_maintenance,
    restore_update_snapshot,
)
from core.utils.server_control import live_server_ports, process_started, read_server_control

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
        raise ApplicationError(f"Server update control failed: {result.message}")
    return result.data


def wait_for_handoff(install: Installation, operation: Operation) -> str | None:
    if operation.handoff_ticket is None:
        return None
    if not install.owns_server:
        raise ApplicationError("An Agent handoff cannot target a client-only installation")
    assert install.server_data_directory is not None
    from core.tools.update_handoff import read_handoff_ticket, ticket_id_from_path

    # The server validates the same capability before it arms the continuation.
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


def arm_continuation(install: Installation, operation: Operation) -> None:
    """Arm the originating Session's continuation of an Agent-started update.

    Armed before the server stops: the stop cancels the originating Run with every
    other active Run, and the once-Bootstrap resumes its Session on the next normal
    startup, so a later failure is still reported there.
    """
    if operation.handoff_ticket is None:
        return
    operation.transition(
        install, "preparing", "Waiting for the originating Tool result to be saved"
    )
    ticket = wait_for_handoff(install, operation)
    control(install, "update_continuation", operation, handoff_ticket_id=ticket)


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


def _database_specs(data_dir: Path) -> tuple[DatabaseSpec, ...]:
    """The canonical database declarations of this version, for owner facts.

    Extension databases have none here (declaring them would run Extension
    code); the snapshot verifies them by kernel identity and integrity only.
    """
    from core.runtime.databases import canonical_database_specs

    return canonical_database_specs(data_dir)


def take_update_snapshot(install: Installation, operation: Operation) -> UpdateSnapshot | None:
    """Snapshot the stopped server's data for this operation, or raise ``ApplicationError``."""
    data_dir = _data_dir(install)
    running = _live_servers(data_dir)
    if running is not None:
        raise ApplicationError(f"The data snapshot needs a stopped server, but {running}")
    try:
        snapshot = create_update_snapshot(
            data_dir,
            operation_id=operation.id,
            specs=_database_specs(data_dir),
            retain_as=operation.id,
        )
        operation.data_snapshot = snapshot.snapshot_id if snapshot is not None else None
        operation.save(install)
        return snapshot
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


def keep_previous(
    install: Installation, operation: Operation, reason: str, *, restart: bool = True
) -> None:
    """End an update whose candidate never started; the previous version stays active.

    A previous server that ran is started again, unless *restart* is false because
    this update never stopped it.
    """
    operation.error = reason
    if restart and install.owns_server and operation.server_was_running:
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
        _log_data_kept(operation, snapshot_id, running)
        return not_restored.format(reason=running)
    try:
        change = data_changed_since(data_dir, snapshot)
    except DatabaseError:
        change = "the data could not be compared"
    if change is None:
        _LOGGER.info(
            "Update data snapshot not needed; the candidate left the data unchanged"
            " (operation=%s snapshot=%s)",
            operation.id,
            snapshot_id,
        )
        return f"The new version left the data unchanged; snapshot {snapshot_id} was not needed."
    try:
        if operation.data_restore_started:
            # This operation deliberately retired newer database registrations.
            # The ordinary automatic rollback fence correctly refuses those;
            # the captured safety snapshot restores them through operator restore.
            from core.database.recovery import restore_data_snapshot
            from core.database.snapshots import snapshot_root
            from core.database.update_rollback import data_stamp, describe_change

            before_recovery = data_stamp(data_dir)

            def admit_recovery() -> None:
                refusal = _live_servers(data_dir) or describe_change(
                    before_recovery, data_stamp(data_dir)
                )
                if refusal is not None:
                    raise ApplicationError(
                        f"The safety snapshot {snapshot_id} was not restored: {refusal}. "
                        "The preserved data needs inspection before starting vBot"
                    )

            restore_data_snapshot(
                data_dir,
                snapshot_root(data_dir) / snapshot.snapshot_id,
                specs=_database_specs(data_dir),
                documents=True,
                retire_unlisted=True,
                cause="failed user-selected application rollback",
                before_restore=admit_recovery,
            )
        else:
            restore_update_snapshot(data_dir, snapshot, specs=_database_specs(data_dir))
    except UpdateRollbackRefusedError as exc:
        _log_data_kept(operation, snapshot_id, str(exc))
        return not_restored.format(reason=str(exc))
    except (DatabaseError, OSError, ValueError) as exc:
        raise ApplicationError(
            f"The new version failed its startup check ({operation.error}), and restoring "
            f"the data snapshot {snapshot_id} did not complete: {exc}. Run "
            f"`vbot data-store snapshot restore {snapshot_id} --all --yes` before starting vBot"
        ) from exc
    _LOGGER.warning(
        "Update data restored from its pre-update snapshot (operation=%s snapshot=%s)",
        operation.id,
        snapshot_id,
    )
    return f"The data was restored from snapshot {snapshot_id}, taken before the update."


def _log_data_kept(operation: Operation, snapshot_id: str, reason: str) -> None:
    _LOGGER.warning(
        "Update data snapshot not restored; the data may hold candidate changes"
        " (operation=%s snapshot=%s reason=%s)",
        operation.id,
        snapshot_id,
        json.dumps(reason, ensure_ascii=False),
    )


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


def _restore_previous_server(install: Installation, operation: Operation) -> bool:
    """Return the server to how the interrupted update found it; ``False`` when unsafe.

    A candidate verification server that answers is stopped. The previous server is
    started again only when none of it is alive, so a busy one is never duplicated; a
    living one keeps running. A verification server that does not answer is left
    alone and the operation needs attention instead.
    """
    candidate = processes.server_state(
        install, version_id=operation.candidate_version, verification=True
    )
    if candidate == "running":
        require_ok(processes.stop(install, initiator="update"))
    elif candidate == "unresponsive":
        operation.transition(
            install,
            "needs_attention",
            "The new version's verification server is running but does not answer its health "
            "check; versions and data have been preserved. Stop it with `vbot server stop` "
            "before trying again",
        )
        return False
    if not operation.server_was_running:
        return True
    previous = processes.server_state(install, version_id=operation.previous_version)
    if previous == "foreign":
        raise ApplicationError(
            "The server target is occupied by a different application version or startup mode"
        )
    if previous == "absent":
        require_ok(processes.start(install, version_id=operation.previous_version, breakaway=False))
    return True


def recover_interrupted(install: Installation, operation: Operation) -> bool:
    """Never blindly replay activation after losing the worker mid-transaction.

    Never restores a data snapshot: after a lost worker, nothing proves the
    data was written only by the candidate.
    """
    if operation.phase not in {"stopping", "activating", "verifying"}:
        return False
    if operation.data_restore_started and operation.phase != "verifying":
        operation.transition(
            install,
            "needs_attention",
            "The requested data restore was interrupted. No data restore was repeated. "
            f"Inspect vbot data-store status and vbot update status {operation.id}; "
            f"the safety snapshot is {operation.data_snapshot}",
        )
        return True
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
            and processes.server_state(install, version_id=candidate) != "running"
        ):
            operation.transition(
                install,
                "needs_attention",
                "The candidate is active but its normal server startup is not proven",
            )
            return True
        validate_release(
            install.version(candidate),
            shape=install.install_shape,
            remove_bytecode_caches=True,
            access=install.access,
        )
        operation.transition(
            install, "completed", "Recovered the verified active application update"
        )
        return True
    if active == operation.previous_version:
        if install.owns_server and not _restore_previous_server(install, operation):
            return True
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
    from cli.application.operations import require_current_host

    # The predecessor tray can itself queue an ordinary update after activation.
    # Refuse before any data work: it cannot safely recover our richer records.
    require_current_host(install)
    if recover_interrupted(install, operation):
        return
    if install.version().name != operation.previous_version:
        raise ApplicationError("The active installation changed after this update was requested")
    rollback_point: Operation | None = None
    if operation.rollback:
        from cli.application.rollback import require_rollback_target

        rollback_point = require_rollback_target(
            install, source=operation.rollback_source, restore_snapshot=operation.restore_snapshot
        )
    operation.previous_label = version_label(
        read_json(install.version() / "release.json", limit=32 * 1024**2)
    )

    def progress(message: str, target: str | None = None) -> None:
        if target is not None:
            operation.target_label = target
        operation.transition(install, "preparing", message)

    progress("Checking for updates")
    if operation.package:
        package = Path(operation.package)
        # A prepared version (`update activate`) is already unpacked.
        progress(
            "Verifying the prepared version"
            if package.is_dir()
            else "Unpacking and verifying the selected package"
        )
        candidate = stage_package(
            install,
            package,
            local=operation.local_package,
            release_version=operation.release_version,
        )
    else:
        try:
            archive = download_release(
                install,
                operation.id,
                progress=progress,
                release_version=operation.release_version,
            )
        except OlderReleaseError as exc:
            # An expected refusal before anything changed, not a worker failure.
            operation.error = str(exc)
            operation.transition(
                install, "failed", "The update channel offers no newer version; nothing changed"
            )
            return
        if archive is None:
            candidate = install.version().name
        else:
            progress("Unpacking and verifying the downloaded package")
            candidate = stage_package(
                install, archive, local=False, release_version=operation.release_version
            )
    candidate_release = read_json(install.version(candidate) / "release.json", limit=32 * 1024**2)
    if candidate != install.version().name:
        # Before dependencies, stopping, or a verification launch; prepared
        # updates re-enter here as well. Automatic recovery of a failed forward
        # update can still restart its original pre-feature baseline.
        require_access_compatible_release(candidate_release, install.access)
        active_release = read_json(install.version() / "release.json", limit=32 * 1024**2)
        if (
            operation.rollback
            or operation.release_version is not None
            or active_release.get("version_switch_protocol") is not None
        ):
            require_version_switch_release(candidate_release)
    from cli.application.dependencies import prepare_for_version

    progress("Checking Extension dependencies")
    prepare_for_version(install, candidate)
    operation.candidate_version = candidate
    operation.target_label = version_label(candidate_release)
    operation.save(install)
    if install.version().name == candidate:
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
        # One classification decides the server's part: only a running server is
        # stopped, and only a server that ran is started again.
        state = processes.server_state(install, version_id=operation.previous_version)
        if state == "foreign":
            raise ApplicationError(
                "The server target is occupied by a different application version or startup mode"
            )
        operation.server_was_running = state != "absent" or (
            rollback_point is not None
            and rollback_point.phase == "needs_attention"
            and rollback_point.server_was_running
        )
        operation.save(install)
        if state == "unresponsive" and not operation.rollback:
            # A server that does not answer may not honor a normal shutdown, and no
            # continuation can be armed through it: fail before anything changed,
            # leaving it running.
            keep_previous(
                install,
                operation,
                "the running server did not answer its health check, so the update left it "
                "running; try the update again once the server responds",
                restart=False,
            )
            return
        if state == "running" or (state == "unresponsive" and operation.rollback):
            arm_continuation(install, operation)
            # The update takes effect at once: a normal server shutdown, which
            # cancels active Runs and queued work instead of waiting for them.
            operation.transition(install, "stopping", "Stopping the current server")
            require_ok(processes.stop(install, initiator="update"))
        # Take the checkpoint offline, then check both claims and data changes
        # before using it to guard a candidate start or a selected data restore.
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
        if operation.restore_snapshot is not None:
            if update_snapshot is None:
                keep_previous(
                    install, operation, "The current data could not be saved for recovery"
                )
                return
            from core.database.recovery import restore_data_snapshot
            from core.database.snapshots import snapshot_root

            data_dir = _data_dir(install)
            selected = snapshot_root(data_dir) / operation.restore_snapshot
            try:
                restore_data_snapshot(
                    data_dir, selected, documents=True, retire_unlisted=True, check_only=True
                )
            except (DatabaseError, OSError, ValueError) as exc:
                keep_previous(
                    install, operation, f"The selected data snapshot cannot be restored: {exc}"
                )
                return

            def admit_restore() -> None:
                # The restore owner already holds its maintenance guard: a new
                # server cannot open data between this check and replacement.
                fence = data_fence(install, update_snapshot)
                if fence is not None:
                    raise ApplicationError(f"The data was not restored: {fence}")
                operation.data_restore_started = True
                operation.transition(
                    install, "activating", "Restoring the approved pre-update data snapshot"
                )

            try:
                restore_data_snapshot(
                    data_dir,
                    selected,
                    documents=True,
                    retire_unlisted=True,
                    cause="user-approved application rollback",
                    before_restore=admit_restore,
                )
            except ApplicationError as exc:
                if operation.data_restore_started:
                    raise
                keep_previous(install, operation, str(exc))
                return
            except (DatabaseError, OSError, ValueError) as exc:
                if not operation.data_restore_started:
                    keep_previous(
                        install, operation, f"The selected data snapshot cannot be restored: {exc}"
                    )
                    return
                raise ApplicationError(
                    f"The requested data restore did not complete: {exc}. Finish it with "
                    f"vbot data-store snapshot restore {operation.restore_snapshot} --all --yes; "
                    f"the safety snapshot is {operation.data_snapshot}"
                ) from exc
            running = _live_servers(data_dir)
            if running is not None:
                raise ApplicationError(
                    f"The data was restored but the candidate cannot start: {running}"
                )
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
            require_ok(processes.stop(install, initiator="update"))
        else:
            operation.error = verified.message
            data_note = roll_back_data(install, operation, update_snapshot)
            if (
                rollback_point is not None
                and operation.restore_snapshot is None
                and rollback_point.data_snapshot is not None
            ):
                data_note += (
                    " Restoring the previous installation's saved data discards changes "
                    f"made after its snapshot. To approve this, run vbot rollback "
                    f"--restore-data {rollback_point.data_snapshot} --yes."
                )
            # Old code is not run against the data without a probe.
            previous = operation.previous_version
            old_check = processes.start(
                install, version_id=previous, verification=True, breakaway=False
            )
            if old_check.ok:
                require_ok(processes.stop(install, initiator="update"))
                if operation.server_was_running:
                    restarted = processes.start(install, version_id=previous, breakaway=False)
                    if not restarted.ok:
                        raise ApplicationError(
                            f"The current version could not restart: {restarted.message}. "
                            f"{data_note}"
                        )
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
    operation.transition(
        install,
        "completed",
        (
            f"Previous installation restored. Data restored from {operation.restore_snapshot}; "
            f"the prior current data is saved in {operation.data_snapshot}"
        )
        if operation.restore_snapshot is not None
        else "Previous installation restored; current data was kept"
        if operation.rollback
        else "Desktop client updated; an open Desktop restarts into the new version right away"
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
        current_created = process_started(psutil.Process())
        if operation.worker_pid is not None and (
            operation.worker_pid != current_pid
            or operation.worker_created is None
            or abs(operation.worker_created - current_created) >= 0.001
        ):
            return
        operation.worker_pid = current_pid
        operation.worker_created = current_created
        operation.save(install)
    retired: list[Path] = []
    admitted = False
    try:
        # This lifetime lock also excludes manual lifecycle mutations.
        # Dispatch uses its separate short lock so callers can observe/coalesce work.
        with exclusive(install.root, "operation", timeout=3600):
            # A prior worker can finish or publish a newer owner while this
            # process waits for the lifetime lock. Never replay the stale copy.
            operation = load_operation(install, operation_id)
            if operation.terminal or (
                operation.worker_pid != current_pid
                or operation.worker_created is None
                or abs(operation.worker_created - current_created) >= 0.001
            ):
                return
            admitted = True
            execute(install, operation)
            if operation.phase == "completed":
                retired = retire_unneeded(install)
            if operation.terminal and operation.phase != "needs_attention":
                from cli.application.rollback import release_unused_snapshot_claims

                try:
                    release_unused_snapshot_claims(install)
                except ApplicationError, DatabaseError, OSError, ValueError:
                    _LOGGER.warning(
                        "Kept application data snapshot claims after cleanup failed (operation=%s)",
                        operation.id,
                        exc_info=True,
                    )
    except Exception as exc:
        if not admitted:
            # No authoritative transaction was admitted; preserve its record.
            raise
        _LOGGER.exception("Application update failed (operation=%s)", operation.id)
        operation.error = str(exc)
        attention = operation.phase in {"stopping", "activating", "verifying"}
        operation.transition(
            install,
            "needs_attention" if attention else "failed",
            "Update did not complete; inspect the saved error and preserved versions",
        )
    # Deleting takes a while and needs no lock: nothing can use a retired tree.
    remove_retired(retired)


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
