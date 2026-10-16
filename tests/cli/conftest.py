"""Fixtures for CLI command tests."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from cli import main as cli_main
from cli.server_management import ServerInstance
from core.utils import processes
from tests.cli.cli_test_support import FakeRpc, RunCli, make_instance


@pytest.fixture(autouse=True)
def _outside_foreign_jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run every command as if no foreign Windows Job held the test process.

    Test runners often run inside one (a coding agent's shell, a CI runner); the
    real inspection would make server starts and update requests go through WMI
    or the tray. Tests of those paths patch ``processes.foreign_job`` themselves.
    """

    monkeypatch.setattr(processes, "foreign_job", lambda: None)


@pytest.fixture
def instance(tmp_path: Path) -> ServerInstance:
    return make_instance(tmp_path)


@pytest.fixture
def rpc(monkeypatch: pytest.MonkeyPatch) -> FakeRpc:
    fake = FakeRpc()
    monkeypatch.setattr(httpx, "post", fake.post)
    return fake


@pytest.fixture
def run_cli(instance: ServerInstance, capsys: pytest.CaptureFixture[str]) -> RunCli:
    """Run one ``vbot`` command line against ``instance`` and return (exit code, out, err)."""

    def run(*argv: str) -> tuple[int, str, str]:
        capsys.readouterr()
        code = cli_main.run(list(argv), resolve=lambda **_target: instance)
        out, err = capsys.readouterr()
        return code, out, err

    return run
