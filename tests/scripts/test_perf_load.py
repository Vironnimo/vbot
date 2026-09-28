"""Smoke test of the concurrent-Agent load test CLI (``scripts/perf_load.py``).

A real run starts a disposable server; the smoke test stops at the load runner.
"""

from pathlib import Path
from typing import Any

import pytest

import scripts.perf_load as perf_load


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (
            ["--agents", "2", "--turns", "1", "--ui-profile"],
            {
                "scenario": "sessions",
                "levels": (2,),
                "turns": 1,
                "duration_minutes": None,
                "ui": True,
                "ui_profile": True,
            },
        ),
        (
            ["--scenario", "swarm", "--agents", "3", "--duration", "2"],
            {"scenario": "swarm", "levels": (3,), "duration_minutes": 2.0},
        ),
        (
            ["--agents", "1", "--turns", "2", "--ui-history-turns", "300"],
            {"levels": (1,), "ui": True, "ui_profile": False, "ui_history_turns": 300},
        ),
    ],
)
def test_a_run_hands_its_configuration_to_the_load_runner_and_reports_the_outcome(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    argv: list[str],
    expected: dict[str, Any],
) -> None:
    configs: list[Any] = []

    def run_load(config: Any) -> tuple[Path, dict[str, Any]]:
        configs.append(config)
        return tmp_path, {"levels": [{"agents": 2, "status": "ok"}]}

    monkeypatch.setattr(perf_load, "activate_process_containment", lambda: None)
    monkeypatch.setattr(perf_load, "run_load", run_load)

    assert perf_load.main(argv) == 0
    assert {key: getattr(configs[0], key) for key in expected} == expected
