#!/usr/bin/env python
"""Check main completely and push it to origin only when every check passes.

The commit ``main`` points to is checked out detached in a private checkout under
``.worktrees``, apart from every working tree, so uncommitted work cannot change
the result. There, in this order: whether this Python runs the SQLite the packages
bundle, Ruff format check and lint, mypy for Windows and Linux as the commit hook
runs it, the complete pytest suite on every core of the machine's test core pool,
and the WebUI's format check, lint, Vitest and build.
On Windows the complete pytest suite also runs on Linux in WSL, alongside the WebUI
checks (``scripts/linux/push_tests.sh``); without a working WSL that step fails.
Every step runs, whatever failed before it. Backend tests that fail run once more
alone; those that pass then are reported as flaky without blocking.

Only when every step passed is exactly the checked commit pushed to origin's main,
as a fast-forward; the push never forces. When origin's main has commits the local
main lacks, nothing is pushed. ``--no-push`` runs the checks only.

The checkout gets its dependencies as ``scripts/worktree.py`` seeds a new worktree:
the primary checkout's WebUI packages when they match the commit's lock (``npm ci``
otherwise), its search engine and its type checker cache. It is removed when the
run ends, however it ends; a later run or worktree command removes the checkout of
a run that was killed. The complete output of every step goes to a log under the
git directory, whose path the report prints.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple, TextIO

# Direct execution loads helper modules from this checkout.
_checkout_root = Path(__file__).resolve().parents[1]
if sys.path[:1] != [str(_checkout_root)]:
    sys.path.insert(0, str(_checkout_root))

from cli.application.runtime_sqlite import interpreter_problem  # noqa: E402
from scripts import commit_check, worktree  # noqa: E402
from scripts._worktree_seed import (  # noqa: E402
    seed_native_resources,
    seed_type_check_cache,
    seed_webui_packages,
)

REMOTE = "origin"
BRANCH = "main"
LOG_DIR_NAME = "vbot-push-logs"
LOGS_KEPT = 10
# Lines of a failed step's output the report shows; the log holds all of them.
SHOWN_LINES = 30
PYTEST_SUMMARY_PATTERN = re.compile(
    r"^(?:FAILED |ERROR |Interrupted: |=*\s*\d+ (?:failed|passed|errors?)\b)"
)
WORKFLOW = ".vorch/workflows/push-workflow.md"
# Run by bash in WSL with the checked commit as a tar on stdin: unpack it into the
# cache's checkout under the cache's lock, then run the suite there. No quotes, so
# Windows command line quoting cannot change it.
WSL_BOOTSTRAP = (
    "set -e; cache=$HOME/.cache/vbot-push; mkdir -p $cache; exec 9>$cache/lock; flock 9; "
    "rm -rf $cache/checkout; mkdir $cache/checkout; tar -x -C $cache/checkout; "
    "exec bash $cache/checkout/scripts/linux/push_tests.sh $cache/checkout"
)
FLAKY_PREFIX = "flaky: "


class Step(NamedTuple):
    """One check's outcome: *details* shows what failed, *note* what did not block."""

    label: str
    passed: bool
    seconds: float
    details: str = ""
    note: str = ""


def _git(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _run(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run *command* with its output and errors in order in ``stdout``."""
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "NO_COLOR": "1"},
            check=False,
        )
    except OSError as exc:
        return subprocess.CompletedProcess(command, 127, str(exc))


def _excerpt(output: str, keep: Callable[[str], bool] | None = None) -> str:
    """Return the lines of *output* that *keep* selects (else all), at most ``SHOWN_LINES``."""
    lines = [line for line in output.splitlines() if line.strip()]
    if keep is not None:
        lines = [line for line in lines if keep(line)] or lines
    if len(lines) > SHOWN_LINES:
        hidden = len(lines) - SHOWN_LINES
        lines = [f"... {hidden} more lines in the log", *lines[-SHOWN_LINES:]]
    return "\n".join(lines)


def _log(log: TextIO, label: str, command: list[str], code: int, seconds: float, out: str) -> None:
    log.write(f"===== {label}: {' '.join(command)} (exit {code}, {seconds:.1f}s)\n{out}\n\n")
    log.flush()


def _command_step(
    label: str,
    command: list[str],
    cwd: Path,
    log: TextIO,
    keep: Callable[[str], bool] | None = None,
) -> Step:
    start = time.monotonic()
    result = _run(command, cwd)
    seconds = time.monotonic() - start
    _log(log, label, command, result.returncode, seconds, result.stdout)
    if result.returncode == 0:
        return Step(label, True, seconds)
    return Step(label, False, seconds, _excerpt(result.stdout, keep))


def _type_check_steps(checkout: Path, log: TextIO) -> list[Step]:
    """Run mypy for every platform as the commit hook does; every error blocks."""
    start = time.monotonic()
    targets = commit_check.mypy_targets(checkout, [])
    results = commit_check.check_types(checkout, targets, set(), set())
    seconds = time.monotonic() - start
    steps = []
    for result in results:
        log.write(f"===== {result.label}: {result.status} ({seconds:.1f}s)\n{result.details}\n\n")
        details = _excerpt(result.details) if result.blocking else ""
        steps.append(Step(result.label, not result.blocking, seconds, details))
    log.flush()
    return steps


def _last_failed(checkout: Path) -> set[str]:
    """Return the tests pytest's cache in *checkout* records as failed in its last run."""
    record = checkout / ".pytest_cache" / "v" / "cache" / "lastfailed"
    try:
        failed = json.loads(record.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return set(failed) if isinstance(failed, dict) else set()


def _pytest_step(checkout: Path, log: TextIO) -> Step:
    """Run the complete suite; tests that fail run once more alone, as CI does."""
    from tests import cpu_pool

    def summary(line: str) -> bool:
        return PYTEST_SUMMARY_PATTERN.match(line) is not None

    options = ["-rfE", "-q", "--no-header"]
    # The pool hands the run every core once the runs before it are done.
    complete = [sys.executable, "-m", "pytest", "-n", str(cpu_pool.pool_size()), *options]
    print(
        "running the complete pytest suite (first waits for the test cores other runs "
        "hold; no output until done)...",
        flush=True,
    )
    start = time.monotonic()
    result = _run(complete, checkout)
    _log(log, "pytest", complete, result.returncode, time.monotonic() - start, result.stdout)
    failed = _last_failed(checkout)
    if result.returncode == 0:
        return Step("pytest", True, time.monotonic() - start)
    if result.returncode != 1 or not failed:
        return Step("pytest", False, time.monotonic() - start, _excerpt(result.stdout, summary))

    alone = [sys.executable, "-m", "pytest", "--last-failed", "--last-failed-no-failures"]
    alone += ["none", "-n", "0", *options]
    rerun_start = time.monotonic()
    rerun = _run(alone, checkout)
    _log(log, "pytest rerun", alone, rerun.returncode, time.monotonic() - rerun_start, rerun.stdout)
    seconds = time.monotonic() - start
    flaky = sorted(failed - _last_failed(checkout)) if rerun.returncode in (0, 1) else []
    note = ""
    if flaky:
        note = "failed under the parallel load, passed alone (not blocking):\n" + "\n".join(flaky)
    if rerun.returncode == 0:
        return Step("pytest", True, seconds, note=note)
    return Step("pytest", False, seconds, _excerpt(rerun.stdout, summary), note)


def _linux_pytest_step(checkout: Path, log: TextIO) -> Step:
    """Run the complete suite of *checkout*'s commit on Linux in WSL."""
    label = "linux pytest"
    start = time.monotonic()
    wsl = shutil.which("wsl.exe")
    if wsl is None:
        return Step(
            label,
            False,
            0.0,
            "WSL is not installed; the Linux tests need it. Install a distribution with "
            "python3 and its venv module (`wsl --install -d Ubuntu`) and run again.",
        )
    with tempfile.TemporaryDirectory() as temporary:
        archive = Path(temporary) / "commit.tar"
        packed = _git(checkout, "archive", "--format=tar", "-o", str(archive), "HEAD")
        if packed.returncode != 0:
            return Step(label, False, 0.0, f"git archive failed: {packed.stderr.strip()}")
        command = [wsl, "--exec", "bash", "-c", WSL_BOOTSTRAP]
        with archive.open("rb") as stdin:
            try:
                result = subprocess.run(
                    command,
                    stdin=stdin,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    check=False,
                )
            except OSError as exc:
                result = subprocess.CompletedProcess(command, 127, str(exc).encode())
    # wsl.exe writes its own messages in UTF-16, the Linux side UTF-8.
    output = result.stdout.decode("utf-8", errors="replace").replace("\x00", "")
    seconds = time.monotonic() - start
    _log(log, label, command, result.returncode, seconds, output)
    flaky = [
        line[len(FLAKY_PREFIX) :] for line in output.splitlines() if line.startswith(FLAKY_PREFIX)
    ]
    note = ""
    if flaky:
        note = "failed under the parallel load, passed alone (not blocking):\n" + "\n".join(flaky)
    if result.returncode == 0:
        return Step(label, True, seconds, note=note)

    def summary(line: str) -> bool:
        return line.startswith("linux tests: ") or PYTEST_SUMMARY_PATTERN.match(line) is not None

    return Step(label, False, seconds, _excerpt(output, summary), note)


def _provide_webui_packages(checkout: Path, log: TextIO) -> Step | None:
    """Give *checkout* packages matching its lock; return the failed install, or None."""
    if seed_webui_packages(worktree.PROJECT_ROOT, checkout):
        return None
    print(
        "installing the webui dependencies: the primary checkout's differ from this "
        "commit's lock (npm ci, no output until done)...",
        flush=True,
    )
    npm = shutil.which("npm") or "npm"
    install = _command_step("webui deps", [npm, "ci"], checkout / "webui", log)
    if install.passed:
        return None
    return install._replace(details=install.details + "\nThe WebUI checks did not run.")


def run_checks(checkout: Path, log: TextIO) -> list[Step]:
    """Prepare *checkout* and run every check there; print each verdict as it comes."""
    steps: list[Step] = []

    def record(*done: Step) -> None:
        for step in done:
            steps.append(step)
            verdict = "PASS" if step.passed else "FAIL"
            print(f"  {step.label:<14}{verdict}  {step.seconds:6.1f}s", flush=True)

    python = sys.executable
    # The suite and the WebUI checks run on this interpreter: its SQLite decides the
    # databases' journal mode, which must be the one installations run.
    problem = interpreter_problem(checkout)
    record(Step("sqlite", problem is None, 0.0, problem or ""))
    print("preparing the checkout (copies dependencies, no output until done)...", flush=True)
    seed_type_check_cache(worktree.PROJECT_ROOT, checkout)
    seed_native_resources(worktree.PROJECT_ROOT, checkout)
    engine = _command_step("search engine", [python, "-m", "cli.search_runtime"], checkout, log)
    if not engine.passed:
        record(engine)
    packages = _provide_webui_packages(checkout, log)
    if packages is not None:
        record(packages)

    ruff = [python, "-m", "ruff"]
    record(_command_step("ruff format", [*ruff, "format", "--check", "."], checkout, log))
    lint = [*ruff, "check", "--output-format", "concise", "."]
    record(_command_step("ruff check", lint, checkout, log))
    record(*_type_check_steps(checkout, log))
    record(_pytest_step(checkout, log))
    with ThreadPoolExecutor(max_workers=1) as background:
        linux: Future[Step] | None = None
        # The Linux step logs into its own buffer, copied to the log once it is done.
        linux_log = io.StringIO()
        if sys.platform == "win32":
            print(
                "running the complete pytest suite on Linux in WSL alongside the webui "
                "checks (no output until done)...",
                flush=True,
            )
            linux = background.submit(_linux_pytest_step, checkout, linux_log)
        if packages is None:
            webui = checkout / "webui"
            npm = shutil.which("npm") or "npm"
            npx = shutil.which("npx") or "npx"
            record(_command_step("webui format", [npm, "run", "format:check"], webui, log))
            record(_command_step("webui lint", [npm, "run", "lint"], webui, log))
            record(_command_step("vitest", [npx, "vitest", "run"], webui, log))
            record(_command_step("webui build", [npm, "run", "build"], webui, log))
        if linux is not None:
            record(linux.result())
            log.write(linux_log.getvalue())
            log.flush()
    return steps


def _new_log(root: Path, commit: str) -> Path:
    """Return the path of this run's log; only the newest ``LOGS_KEPT`` logs stay."""
    common = _git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    directory = Path(common.stdout.strip()) / LOG_DIR_NAME
    directory.mkdir(parents=True, exist_ok=True)
    for old in sorted(directory.glob("*.log"))[: -(LOGS_KEPT - 1)]:
        old.unlink(missing_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return directory / f"{stamp}-{commit[:12]}.log"


def _unpushable(root: Path, commit: str) -> str | None:
    """Return why pushing *commit* to origin's main would not fast-forward it, or None.

    Returns an empty reason when origin's main already is *commit*.
    """
    remote = _git(root, "ls-remote", "--exit-code", REMOTE, f"refs/heads/{BRANCH}")
    if remote.returncode == 2:
        return None  # origin has no main yet
    if remote.returncode != 0:
        return f"origin cannot be read: {remote.stderr.strip() or 'git ls-remote failed'}"
    remote_commit = remote.stdout.split()[0]
    if remote_commit == commit:
        return ""
    known = _git(root, "cat-file", "-e", f"{remote_commit}^{{commit}}").returncode == 0
    if known and _git(root, "merge-base", "--is-ancestor", remote_commit, commit).returncode == 0:
        return None
    return (
        f"origin's main moved: it has commits local main lacks (origin is at "
        f"{remote_commit[:12]}), and they must come into main before it can be pushed"
    )


def check_and_push(commit: str, *, push: bool) -> int:
    """Check *commit* in a private checkout; push it to origin's main when everything passed."""
    root = worktree.PROJECT_ROOT
    subject = _git(root, "log", "-1", "--format=%s", commit).stdout.strip()
    print(f"Checking {commit[:12]} ({subject})", flush=True)
    if push:
        reason = _unpushable(root, commit)
        if reason == "":
            print(f"origin's main is already at {commit[:12]}; nothing to push.")
            return 0
        if reason is not None:
            print(f"Not pushed, nothing checked: {reason}.")
            return 1

    start = time.monotonic()
    log_path = _new_log(root, commit)
    worktree.sweep_private_checkouts(worktree.WORKTREES_DIR)
    with (
        log_path.open("w", encoding="utf-8") as log,
        worktree.private_checkout(worktree.PUSH_DIR_PREFIX, commit[:12]) as checkout,
    ):
        log.write(f"push check of {commit} ({subject}) in {checkout}\n\n")
        created = _git(root, "worktree", "add", "--detach", str(checkout), commit)
        if created.returncode != 0:
            print(f"error: the checkout could not be created: {created.stderr.strip()}")
            return 1
        steps = run_checks(checkout, log)

    for step in steps:
        if step.details:
            print(f"\n--- {step.label}: FAIL ---\n{step.details}")
        if step.note:
            print(f"\n--- {step.label}: note ---\n{step.note}")
    print(f"\nComplete output: {log_path}")
    elapsed = f"{time.monotonic() - start:.0f}s"
    failed = [step.label for step in steps if not step.passed]
    if failed:
        print(
            f"\nNot pushed ({elapsed}): {', '.join(failed)} failed. Fix every failure, "
            "whatever commit caused it, commit the fixes on main and run "
            f"`python scripts/push.py` again ({WORKFLOW})."
        )
        return 1
    if not push:
        print(f"\nEvery check passed ({elapsed}); not pushed (--no-push).")
        return 0

    pushed = _git(root, "push", REMOTE, f"{commit}:refs/heads/{BRANCH}")
    if pushed.returncode != 0:
        reason = _unpushable(root, commit) or f"git push failed: {pushed.stderr.strip()}"
        print(f"\nNot pushed ({elapsed}): every check passed, but {reason}.")
        return 1
    print(f"\nEvery check passed; pushed {commit[:12]} to origin's main ({elapsed}).")
    main = _git(root, "rev-parse", "-q", "--verify", f"refs/heads/{BRANCH}").stdout.strip()
    if main and main != commit:
        print(f"note: main moved on to {main[:12]} meanwhile; run again to push it.")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="push.py",
        description=(
            "Check the commit main points to completely in a private checkout and push "
            "it to origin's main only when every check passes."
        ),
    )
    parser.add_argument("--no-push", action="store_true", help="run the checks only")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    # Tool output may hold characters a legacy Windows code page cannot encode.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    commit = _git(worktree.PROJECT_ROOT, "rev-parse", "-q", "--verify", f"{BRANCH}^{{commit}}")
    if commit.returncode != 0:
        print(f"error: the repository has no {BRANCH} branch")
        return 1
    try:
        return check_and_push(commit.stdout.strip(), push=not args.no_push)
    except KeyboardInterrupt:
        print("\ninterrupted: nothing was pushed")
        return 130


if __name__ == "__main__":
    sys.exit(main())
