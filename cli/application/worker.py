"""Restartable update transaction. Never a child owned by the vBot Runtime."""

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
from cli.update_management import _ensure_update_data_snapshot
from core.utils.server_control import read_server_control

_LOGGER = logging.getLogger("vbot.application.update")


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


def recover_interrupted(install: Installation, operation: Operation) -> bool:
    """Never blindly replay activation after losing the worker mid-transaction."""
    if operation.phase not in {"stopping", "activating", "verifying"}:
        return False
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
        operation.transition(install, "waiting_for_idle", "Saving a recovery snapshot of the data")
        snapshot = _ensure_update_data_snapshot(processes.target(install))
        if not snapshot.ok:
            raise ApplicationError(snapshot.message)
        operation.transition(
            install, "stopping", "Stopping the current server after draining accepted work"
        )
        require_ok(processes.stop(install))
    operation.transition(install, "activating", "Checking the prepared version before activation")
    if install.owns_server:
        # No producer/user admission in verification mode. Candidate uses same data,
        # but failure never authorizes overwriting it with a stale snapshot.
        # Dispatch already detached this worker from vBot's lifetime Job. It
        # owns no Job itself, so server children outlive it without requesting
        # a second breakaway from an unrelated ambient Windows Job.
        verified = processes.start(
            install, version_id=candidate, verification=True, breakaway=False
        )
        if verified.ok:
            require_ok(processes.stop(install))
        else:
            # Old code is not run against potentially changed state without a probe.
            operation.error = verified.message
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
                    "Candidate startup failed; previous version startup was verified and restored",
                )
                return
            raise ApplicationError(
                "Candidate and previous version could not open the preserved data; "
                "recovery needs attention"
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
