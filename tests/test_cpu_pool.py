from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests import cpu_pool


def test_a_run_waits_until_the_cores_it_asks_for_are_free(tmp_path: Path) -> None:
    first = cpu_pool.take(tmp_path, cores=5, size=2)
    assert len(first) == 2

    waiting = threading.Event()
    second: list[Any] = []
    thread = threading.Thread(
        target=lambda: second.extend(cpu_pool.take(tmp_path, 2, 2, waiting.set)), daemon=True
    )
    thread.start()
    assert waiting.wait(5)
    assert not second

    cpu_pool.release(first)
    thread.join(5)
    assert len(second) == 2
    cpu_pool.release(second)


def test_checks_leave_the_reserved_cores_to_direct_runs(tmp_path: Path) -> None:
    check = cpu_pool.take(tmp_path, cores=4, size=4, check=True)
    assert len(check) == 4 - cpu_pool.RESERVED_CORES

    waiting = threading.Event()
    direct = cpu_pool.take(tmp_path, 2, 4, waiting.set)

    assert len(direct) == 2
    assert not waiting.is_set()
    cpu_pool.release([*check, *direct])


class _PluginManager:
    def __init__(self) -> None:
        self.plugins: dict[str, Any] = {}

    def register(self, plugin: Any, name: str) -> None:
        self.plugins[name] = plugin

    def get_plugin(self, name: str) -> Any:
        return SimpleNamespace(stats={"passed": [1, 2, 3]}) if name == "terminalreporter" else None


def _config(workers: int) -> Any:
    option = SimpleNamespace(numprocesses=workers, tx=["popen"] * workers)
    return SimpleNamespace(
        option=option,
        getoption=lambda name, default=None: getattr(option, name, default),
        pluginmanager=_PluginManager(),
        rootpath=Path("checkout"),
    )


@pytest.mark.parametrize(
    ("environment", "claims"),
    [({}, True), ({"CI": "true"}, False), ({cpu_pool.HELD_VARIABLE: "1"}, False)],
    ids=["local", "ci", "inside a run holding cores"],
)
def test_a_run_starts_no_more_workers_than_the_pool_grants_and_logs_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, environment: dict[str, str], claims: bool
) -> None:
    # This test itself runs inside a pytest run that holds cores; claim() marks the
    # environment, which the monkeypatch restores.
    monkeypatch.setenv(cpu_pool.HELD_VARIABLE, "")
    monkeypatch.setenv("CI", "")
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv(cpu_pool.KIND_VARIABLE, "branch")
    # A check of the commit hook may hold the two cores beyond the reserved ones.
    monkeypatch.setattr(cpu_pool, "pool_size", lambda: 4)
    config = _config(workers=12)

    cpu_pool.claim(config, tmp_path)

    if not claims:
        assert config.option.numprocesses == 12
        assert not config.pluginmanager.plugins
        return
    assert config.option.numprocesses == 2
    assert config.option.tx == ["popen", "popen"]
    [run] = config.pluginmanager.plugins.values()
    run.pytest_sessionfinish(exitstatus=0)
    run.pytest_unconfigure(config)

    [line] = (tmp_path / cpu_pool.RUN_LOG).read_text(encoding="utf-8").splitlines()
    record = json.loads(line)
    assert (record["kind"], record["cores_asked"], record["cores"]) == ("branch", 12, 2)
    assert (record["exit"], record["passed"]) == (0, 3)
    # The run released its cores.
    cpu_pool.release(cpu_pool.take(tmp_path, 4, 4))
