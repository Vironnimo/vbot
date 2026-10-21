"""Preparation and rollback of Terminal process resources."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import (
    Callable,
    Mapping,
    Sequence,
)
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import BinaryIO

from core.runs import RunExecutionOwner
from core.storage.temp_files import TemporaryFileLease, TemporaryFileManager, artifact_owner
from core.tools import terminal_backend
from core.tools.shell_environment import terminal_environment
from core.tools.terminal_backend import (
    TerminalAdapter,
    TerminalAdapterFactory,
)
from core.utils.logging import get_logger
from core.utils.paths import model_path

from ._terminal_launch import ShellLaunch, integrated_shell_kind, interactive_shell
from ._terminal_render_host import TerminalRenderHost, TerminalScreen
from ._terminal_session import (
    TerminalLaunch,
    TerminalSession,
    TerminalSessionServices,
)
from ._terminal_state import (
    TERMINAL_TEMPORARY_CATEGORY,
    TerminalClosedError,
    TerminalLaunchError,
    TerminalOwner,
    _validate_dimensions,
)
from .process_tree import ProcessTree, ProcessTreeTracker

_LOGGER = get_logger("tools.terminal_manager")


class TerminalSessionFactory:
    """Own process preparation and unwind every partially prepared Terminal launch."""

    def __init__(
        self,
        *,
        temporary_files: TemporaryFileManager | None,
        adapter_factory: TerminalAdapterFactory,
        render_host: TerminalRenderHost,
        scrollback_lines: int,
        process_observer: ProcessTreeTracker | None,
        validate_pin: Callable[[str | None], None],
    ) -> None:
        self._temporary_files = temporary_files
        self._adapter_factory = adapter_factory
        self._render_host = render_host
        self._scrollback_lines = scrollback_lines
        self._process_observer = process_observer
        self._validate_pin = validate_pin

    async def start(
        self,
        owner: TerminalOwner | None,
        argv: Sequence[str],
        *,
        launch: TerminalLaunch,
        services: TerminalSessionServices,
        can_start: Callable[[], bool],
        allocate_id: Callable[[], str],
        allocate_call_name: Callable[[], str],
        env: Mapping[str, str] | None,
        columns: int,
        rows: int,
        origin_run_id: str | None,
        group_id: str | None = None,
        execution_owner: RunExecutionOwner | None = None,
        command_line: str | None = None,
        cleanup: Callable[[], None] | None = None,
        integrate_shell: bool = False,
    ) -> TerminalSession:
        """Start one process behind PTY/ConPTY and its Terminal Session.

        With *integrate_shell*, an interactive shell starts with shell integration.
        """
        _validate_dimensions(columns, rows)
        cwd = launch.cwd
        if not cwd.is_dir():
            raise ValueError(f"Terminal workdir is not a directory: {model_path(cwd)}")
        self._validate_pin(group_id)

        log_path: Path | None = None
        log_handle: BinaryIO | None = None
        log_lease: TemporaryFileLease | None = None
        screen = None
        adapter: TerminalAdapter | None = None
        observer: ProcessTree | None = None
        integration: ShellLaunch | None = None
        try:
            screen = await self.open_screen(columns, rows)
            log_path, log_handle, log_lease = await asyncio.to_thread(self._open_raw_log, owner)
            process_env = await asyncio.to_thread(
                partial(terminal_environment, env, confined=launch.permission_scope is not None)
            )
            if integrate_shell:
                kind = integrated_shell_kind(argv)
                integration = await asyncio.to_thread(
                    interactive_shell, argv, environment=process_env
                )
                if integration is not None:
                    launch = replace(launch, shell=kind)
                    argv = integration.argv
                    command_line = integration.command_line
                    process_env.update(integration.environment)
                    if integration.scratch is not None:
                        cleanup = integration.remove_scratch
            if not can_start():
                raise TerminalClosedError("Terminal Session is no longer running")
            adapter = await self._start_process(argv, cwd, process_env, rows, columns, command_line)
            observer = await self._observe(adapter.pid)
        except Exception as error:
            if adapter is not None:
                with contextlib.suppress(OSError, ProcessLookupError):
                    await asyncio.to_thread(terminal_backend.terminate_process_tree, adapter)
                with contextlib.suppress(OSError):
                    await asyncio.to_thread(adapter.close)
            if observer is not None:
                with contextlib.suppress(Exception):
                    observer.close()
            if integration is not None:
                integration.remove_scratch()
            if screen is not None:
                screen.close()
            if log_handle is not None:
                log_handle.close()
            if log_lease is not None:
                log_lease.finish()
            if isinstance(error, TerminalClosedError):
                raise
            raise TerminalLaunchError(f"Terminal process could not be started: {error}") from error

        terminal_id = allocate_id()
        session = TerminalSession(
            terminal_id,
            owner=owner,
            adapter=adapter,
            screen=screen,
            launch=launch,
            call_name=allocate_call_name(),
            group_id=group_id,
            origin_run_id=origin_run_id,
            execution_owner=execution_owner,
            log_path=log_path,
            log_handle=log_handle,
            log_lease=log_lease,
            cleanup=cleanup,
            services=services,
            observer=observer,
        )
        return session

    async def _observe(self, pid: int) -> ProcessTree | None:
        """Observe a program's process tree; None when that failed."""
        if self._process_observer is None:
            return None
        try:
            return await asyncio.to_thread(self._process_observer, pid)
        except Exception:
            _LOGGER.warning(
                "Could not observe the processes of terminal program pid=%s; its activity "
                "relies on output and declared signals",
                pid,
                exc_info=True,
            )
            return None

    async def _start_process(
        self,
        argv: Sequence[str],
        cwd: Path,
        process_env: Mapping[str, str],
        rows: int,
        columns: int,
        command_line: str | None,
    ) -> TerminalAdapter:
        """Start the process in exactly *process_env*."""
        return await asyncio.to_thread(
            self._adapter_factory,
            list(argv),
            cwd,
            process_env,
            rows,
            columns,
            command_line=command_line,
        )

    async def open_screen(self, columns: int, rows: int) -> TerminalScreen:
        """Open a rendered screen; raises RuntimeError once the render host is closed."""
        await asyncio.to_thread(self._render_host.prepare)
        return self._render_host.open_screen(columns, rows, scrollback_lines=self._scrollback_lines)

    def _open_raw_log(
        self, owner: TerminalOwner | None
    ) -> tuple[Path | None, BinaryIO | None, TemporaryFileLease | None]:
        """Open the log that receives the output as the terminal receives it.

        A Session's terminal logs to that Session's folder, which only it may read
        under Permission Rules; an operator terminal's log has no owner.
        """
        if self._temporary_files is None:
            return None, None, None
        lease = self._temporary_files.create(
            TERMINAL_TEMPORARY_CATEGORY,
            ".log",
            owner=(
                artifact_owner(owner.project_id, owner.agent_id, owner.session_id)
                if owner is not None
                else None
            ),
        )
        try:
            handle = lease.path.open("ab")
        except BaseException:
            lease.finish()
            raise
        return lease.path, handle, lease
