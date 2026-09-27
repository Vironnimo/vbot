"""Smoke test of the concurrent-Agent load test CLI (``scripts/perf_load.py``).

A real run starts a disposable server; the smoke test stops at the load runner.
"""

from pathlib import Path
from typing import Any

import pytest

import scripts.perf_load as perf_load


def test_a_run_hands_its_configuration_to_the_load_runner_and_reports_the_outcome(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    configs: list[Any] = []

    def run_load(config: Any) -> tuple[Path, dict[str, Any]]:
        configs.append(config)
        return tmp_path, {"levels": [{"agents": 2, "status": "ok"}]}

    monkeypatch.setattr(perf_load, "activate_process_containment", lambda: None)
    monkeypatch.setattr(perf_load, "run_load", run_load)

    assert perf_load.main(["--agents", "2", "--turns", "1"]) == 0
    assert (configs[0].levels, configs[0].turns) == ((2,), 1)
