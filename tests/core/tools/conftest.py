"""Keep fake terminal processes inside the test harness."""

from typing import Any

import pytest

from core.tools import terminal_backend
from core.tools.terminal_backend import TerminalAdapter
from tests.core.tools.terminal_manager_helpers import FakeTerminalAdapter


@pytest.fixture(autouse=True)
def _terminate_fake_terminals(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never send an invented PID to the host's process-tree terminator.

    Patch the OS boundary, retaining terminal shutdown coordination and the
    explicit failure/retry fakes installed by individual lifecycle tests.
    Real adapters still exercise real process termination.
    """
    kill_process_tree = terminal_backend.kill_process_tree

    def kill(adapter: TerminalAdapter, *, targets: list[Any] | None = None) -> None:
        if isinstance(adapter, FakeTerminalAdapter):
            adapter.terminate()
        else:
            kill_process_tree(adapter, targets=targets)

    monkeypatch.setattr(terminal_backend, "kill_process_tree", kill)
