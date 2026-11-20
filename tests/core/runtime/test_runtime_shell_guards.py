"""Runtime supplies live protected roots to both non-interactive shell callers."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path

import pytest

from core.permissions import PermissionBoundary
from core.runtime.runtime import Runtime
from core.tools.shell import HostShellMissingError, HostShellStart
from core.tools.shell_environment import CommandEnvironment
from core.tools.watch_checks import WatchCheckError
from core.utils.config import Config
from tests.core.extensions.extension_test_support import tool_context
from tests.directory_links import link_directory


@pytest.mark.asyncio
async def test_shell_and_watch_checks_receive_current_protected_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    received: list[set[Path]] = []

    def capture(
        command: str,
        workdir: Path,
        environment: CommandEnvironment,
        protected: Iterable[str] = (),
    ) -> HostShellStart:
        received.append({Path(folder) for folder in protected})
        raise HostShellMissingError("test-owned launch boundary")

    monkeypatch.setattr("core.tools.shell.host_shell_start", capture)
    monkeypatch.setattr("core.tools.watch_checks.host_shell_start", capture)
    context = replace(tool_context("bash", tmp_path), agent_id="main")
    data_link = tmp_path / "linked-data"
    link_directory(data_link, tmp_path / "data")
    runtime = Runtime(Config(data_dir=data_link))
    expected = {data_link, data_link.resolve()}
    runtime.start()
    try:
        for index in range(2):
            project = runtime.projects.create(
                f"protected-{index}", "Protected", tmp_path / f"repo-{index}"
            )
            agent = runtime.agents.create(
                f"protected-{index}", workspace=tmp_path / f"workspace-{index}"
            )
            expected.update((Path(project.cwd), Path(agent.workspace)))

            result = await runtime.tools.dispatch(context, {"command": "echo sentinel"})
            assert result["error"]["code"] == "command_not_started", result
            checks = runtime._watch_checks  # noqa: SLF001 - Runtime wiring under test.
            assert checks is not None
            with pytest.raises(WatchCheckError):
                await checks.run_context_command(
                    "echo sentinel", workdir=tmp_path, boundary=PermissionBoundary.UNRESTRICTED
                )
            assert len(received) == (index + 1) * 2
            for roots in received[-2:]:
                assert expected <= roots, expected - roots
    finally:
        await runtime.aclose()
