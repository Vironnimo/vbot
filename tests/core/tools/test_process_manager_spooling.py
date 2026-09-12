"""Process manager: spooling behavior."""

from __future__ import annotations

import sys
import time
from datetime import timedelta
from pathlib import Path

import pytest

from core.storage import TemporaryFileManager
from core.tools.process_manager import (
    ProcessManager,
)
from tests.core.tools.process_manager_helpers import (
    AGENT_A,
    SCOPE_A,
    poll_until_terminal,
)
from tests.core.tools.process_manager_helpers import (
    manager as manager,
)


@pytest.mark.asyncio
async def test_log_file_holds_complete_output_beyond_buffer_cap(tmp_path: Path) -> None:
    # The in-memory buffer keeps only the newest bytes; the spool file must
    # still hold everything the process ever printed.
    manager = ProcessManager(
        buffer_cap_bytes=64,
        sweep_interval_seconds=3600,
        temporary_files=TemporaryFileManager(tmp_path),
    )
    try:
        process_id = await manager.spawn(
            SCOPE_A,
            AGENT_A,
            [sys.executable, "-c", "print('start-marker'); print('x' * 500)"],
            env=None,
            cwd=None,
        )
        await poll_until_terminal(manager, process_id)

        tracked = manager.get_process(process_id, AGENT_A)
        assert tracked.truncated is True
        assert tracked.log_file is not None
        assert tracked.log_file.parent == tmp_path / "artifacts" / "temp" / "bash"

        content = tracked.log_file.read_text(encoding="utf-8")
        assert "start-marker" in content
        assert "x" * 500 in content
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_no_temporary_file_manager_means_no_log_file(manager: ProcessManager) -> None:
    process_id = await manager.spawn(
        SCOPE_A, AGENT_A, [sys.executable, "-c", "print('hi')"], env=None, cwd=None
    )
    await poll_until_terminal(manager, process_id)

    assert manager.get_process(process_id, AGENT_A).log_file is None


@pytest.mark.asyncio
async def test_log_file_is_stripped_of_ansi_escape_sequences(tmp_path: Path) -> None:
    manager = ProcessManager(
        sweep_interval_seconds=3600,
        temporary_files=TemporaryFileManager(tmp_path),
    )
    try:
        script = "import sys; esc = chr(27); sys.stdout.write(f'{esc}[31mred{esc}[0m done\n')"
        process_id = await manager.spawn(
            SCOPE_A, AGENT_A, [sys.executable, "-c", script], env=None, cwd=None
        )
        await poll_until_terminal(manager, process_id)

        tracked = manager.get_process(process_id, AGENT_A)
        assert tracked.log_file is not None
        content = tracked.log_file.read_text(encoding="utf-8")
        assert "red" in content and "done" in content
        assert "\x1b" not in content
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_log_lease_finishes_only_after_process_is_terminal(tmp_path: Path) -> None:
    temporary_files = TemporaryFileManager(
        tmp_path,
        retention={"bash": timedelta(milliseconds=1)},
    )
    manager = ProcessManager(sweep_interval_seconds=3600, temporary_files=temporary_files)
    try:
        process_id = await manager.spawn(
            SCOPE_A,
            AGENT_A,
            [sys.executable, "-c", "import time; time.sleep(30)"],
            env=None,
            cwd=None,
        )
        tracked = manager.get_process(process_id, AGENT_A)
        assert tracked.log_file is not None
        time.sleep(0.01)

        temporary_files.sweep()
        assert tracked.log_file.exists(), "an active process log must survive cleanup"

        await manager.kill(process_id, AGENT_A)
        time.sleep(0.01)
        temporary_files.sweep()
        assert not tracked.log_file.exists()
    finally:
        await manager.aclose()
