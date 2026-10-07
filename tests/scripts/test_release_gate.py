from __future__ import annotations

import re
from pathlib import Path

import pytest

from scripts import release_gate
from scripts.release_gate import Run

ROOT = Path(__file__).resolve().parents[2]
GREEN: dict[str, str | None] = {
    "static (linux x64)": "success",
    "static (windows x64)": "success",
    "backend (linux x64)": "success",
    "backend (windows x64, shard 1/3)": "success",
    "authorship": "success",
    "frontend": "success",
}


def _run(ci_jobs: dict[str, str | None], *, completed: bool = True, url: str = "run") -> Run:
    return Run(url=url, completed=completed, ci_jobs=ci_jobs)


@pytest.mark.parametrize(
    ("runs", "waited", "decision"),
    [
        ([_run(GREEN)], 0, "pass"),
        # A newer push cancelled the packages after this run's CI passed.
        ([_run(GREEN, url="cancelled run")], 0, "pass"),
        ([_run({**GREEN, "frontend": None}, completed=False)], 0, "wait"),
        # Its CI is green, but the run has not ended, so a job may still be missing.
        ([_run(GREEN, completed=False)], 0, "wait"),
        ([_run({**GREEN, "backend (linux x64)": "failure"}, completed=False)], 0, "fail"),
        ([_run({**GREEN, "frontend": "cancelled"})], 0, "fail"),
        ([_run({k: v for k, v in GREEN.items() if k != "authorship"})], 0, "fail"),
        # A failed run and its green re-run of the same commit.
        ([_run({**GREEN, "frontend": "failure"}), _run(GREEN)], 0, "pass"),
        ([], 10, "wait"),
        ([], 600, "fail"),
    ],
)
def test_a_release_waits_for_one_complete_green_main_build_ci(
    runs: list[Run], waited: float, decision: str
) -> None:
    assert release_gate.verdict(runs, "abc", waited=waited, grace=600)[0] == decision


def test_the_gate_requires_every_job_of_ci_yml() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    jobs = workflow.split("\njobs:\n", 1)[1]
    assert tuple(re.findall(r"^  ([\w-]+):$", jobs, re.MULTILINE)) == release_gate.CI_JOBS
