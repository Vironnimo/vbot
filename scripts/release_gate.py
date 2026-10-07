"""Gate a release on the main-build CI of the commit it publishes.

``main-build.yml`` runs the complete CI (``ci.yml``) on every push to ``main``, so
a release of that commit needs that run green rather than a second one. This
waits for the commit's main-build runs and passes once one of them completed
with every CI job successful. It fails once every run failed or was cancelled
before its CI passed, or when no run appears within the grace period.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass

WORKFLOW = "main-build.yml"
#: main-build.yml calls ci.yml as its job ``ci``; GitHub names the called jobs ``ci / <name>``.
CI_PREFIX = "ci / "
#: The jobs of ci.yml; a run whose CI lacks one did not verify the commit completely.
CI_JOBS = ("static", "backend", "authorship", "frontend")


@dataclass(frozen=True)
class Run:
    url: str
    completed: bool
    #: CI job name without the prefix -> its conclusion, ``None`` while it runs.
    ci_jobs: dict[str, str | None]


def ci_state(run: Run) -> str:
    """``passed``, ``failed`` or ``pending`` for the CI of one main-build run."""
    conclusions = run.ci_jobs.values()
    if any(conclusion not in (None, "success") for conclusion in conclusions):
        return "failed"
    if not run.completed:
        return "pending"
    present = {name.split(" (")[0] for name in run.ci_jobs}
    return "passed" if set(CI_JOBS) <= present else "failed"


def verdict(runs: Sequence[Run], sha: str, *, waited: float, grace: float) -> tuple[str, str]:
    """``pass``, ``wait`` or ``fail``, with the message that explains it."""
    states = [(run, ci_state(run)) for run in runs]
    for run, state in states:
        if state == "passed":
            return "pass", f"The main-build CI of {sha} passed: {run.url}"
    if any(state == "pending" for _, state in states):
        return "wait", f"Waiting for the main-build CI of {sha}"
    if not runs:
        if waited < grace:
            return "wait", f"Waiting for a main-build run of {sha} to start"
        return "fail", (
            f"No {WORKFLOW} run exists for {sha}. A push that changes only development "
            f"documentation starts none; start one with `gh workflow run {WORKFLOW} --ref main` "
            "and dispatch the release again."
        )
    urls = ", ".join(run.url for run in runs)
    return "fail", (
        f"The main-build CI of {sha} failed or was cancelled ({urls}). Fix main and push, "
        "or re-run a cancelled run, then dispatch the release again."
    )


def _api(path: str) -> dict[str, object]:
    output = subprocess.run(["gh", "api", path], check=True, capture_output=True, text=True)
    result: dict[str, object] = json.loads(output.stdout)
    return result


def fetch_runs(sha: str) -> list[Run]:
    listing = _api(
        f"repos/{{owner}}/{{repo}}/actions/workflows/{WORKFLOW}/runs?head_sha={sha}&per_page=100"
    )
    runs = []
    for run in listing["workflow_runs"]:  # type: ignore[attr-defined]
        jobs = _api(f"repos/{{owner}}/{{repo}}/actions/runs/{run['id']}/jobs?per_page=100")["jobs"]
        ci_jobs = {
            job["name"].removeprefix(CI_PREFIX): job["conclusion"]
            for job in jobs  # type: ignore[attr-defined]
            if job["name"].startswith(CI_PREFIX)
        }
        runs.append(
            Run(url=run["html_url"], completed=run["status"] == "completed", ci_jobs=ci_jobs)
        )
    return runs


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sha", required=True, help="the commit the release publishes")
    parser.add_argument(
        "--grace", type=float, default=600, help="seconds to wait for a run to appear"
    )
    parser.add_argument("--interval", type=float, default=30, help="seconds between checks")
    args = parser.parse_args(argv)
    start = time.monotonic()
    last = ""
    while True:
        decision, message = verdict(
            fetch_runs(args.sha), args.sha, waited=time.monotonic() - start, grace=args.grace
        )
        if decision == "fail":
            print(f"::error::{message}", flush=True)
            return 1
        if message != last:
            print(message, flush=True)
            last = message
        if decision == "pass":
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
