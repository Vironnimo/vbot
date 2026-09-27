"""Fixtures for CLI command tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from cli import main as cli_main
from cli import rpc_client
from cli.server_management import ServerInstance
from tests.cli.cli_test_support import FakeRpc, RunCli, make_instance


@pytest.fixture
def instance(tmp_path: Path) -> ServerInstance:
    return make_instance(tmp_path)


@pytest.fixture
def rpc(monkeypatch: pytest.MonkeyPatch) -> FakeRpc:
    fake = FakeRpc()
    monkeypatch.setattr(rpc_client.httpx, "post", fake.post)
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
