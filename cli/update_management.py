"""Local self-update for git-based vBot installs.

``vbot update`` advances the installed checkout, refreshes dependencies and the
WebUI when they changed, and restarts the server. It is a local lifecycle
command like the ``server`` family, not an RPC call: it operates on the repo the
running ``vbot`` was installed from, and never touches the ``~/.vbot`` data dir.

Two tracks are auto-detected from the checkout: a branch (e.g. ``main``) is the
*dev* track (``git pull`` + local WebUI build); a detached checkout on a release
tag is the *release* track (fetch the latest release tag + its prebuilt WebUI
asset, so no Node is needed).
"""

from __future__ import annotations

import hashlib
import io
import os
import subprocess
import sys
import tarfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from cli.server_management import (
    DEFAULT_SERVICE_NAME,
    CommandResult,
    ServerInstance,
    restart_server,
    start_server,
    stop_server,
)

GITHUB_API_BASE = "https://api.github.com/repos/Vironnimo/vbot"
WEBUI_ASSET_NAME = "webui-dist.tar.gz"
RELEASE_EXTRAS = "server,cli"
DEV_EXTRAS = "dev"
_API_TIMEOUT_SECONDS = 30.0
_DOWNLOAD_TIMEOUT_SECONDS = 60.0
_COMMAND_TIMEOUT_SECONDS = 600.0

Restart = Callable[[ServerInstance], CommandResult]


@dataclass(frozen=True)
class CommandRun:
    """Result of one external command invocation."""

    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[list[str], Path], CommandRun]


@dataclass(frozen=True)
class ReleaseInfo:
    """Latest release tag plus the prebuilt WebUI asset URL when present."""

    tag: str
    webui_asset_url: str | None


ReleaseLookup = Callable[[], ReleaseInfo]


@dataclass(frozen=True)
class _Step:
    """Outcome of one internal update step; an empty message means 'no note'."""

    ok: bool
    message: str


def run_update(
    instance: ServerInstance,
    *,
    discard: bool = False,
    stash: bool = False,
    restart: bool = True,
    stop: Restart = stop_server,
    start: Restart = start_server,
    runner: Runner | None = None,
    root: Path | None = None,
    latest_release: ReleaseLookup | None = None,
    service_name: str = DEFAULT_SERVICE_NAME,
) -> CommandResult:
    """Advance the installed checkout and optionally restart the server."""

    run = runner or _default_runner
    repo = root if root is not None else _repo_root()
    lookup = latest_release or _fetch_latest_release

    if not (repo / ".git").is_dir():
        return _fail(
            instance,
            f"update: {repo} is not a git checkout; reinstall with the bootstrap to use update",
        )

    track = _detect_track(run, repo)

    dirty = _is_dirty(run, repo)
    if dirty:
        guard = _handle_dirty(run, repo, discard=discard, stash=stash)
        if not guard.ok:
            return _fail(instance, guard.message)

    before = _head_commit(run, repo)
    pyproject_before = _file_digest(repo / "pyproject.toml")

    advanced = (
        _advance_dev(run, repo) if track == "dev" else _advance_release(run, repo, lookup, before)
    )
    if not advanced.ok:
        return _fail(instance, advanced.message)

    after = _head_commit(run, repo)
    lines = [f"update: {track} track"]
    if before and before == after:
        lines.append(f"already up to date at {_short(after)}")
    else:
        lines.append(f"updated {_short(before)} -> {_short(after)}")

    if dirty and stash:
        popped = run(["git", "stash", "pop"], repo)
        if popped.returncode != 0:
            lines.append(
                "reapplying stashed changes hit a conflict; resolve it in the repo "
                "(see 'git stash list'), then restart the server manually"
            )
            return CommandResult(ok=False, message="\n".join(lines), instance=instance)
        lines.append("local changes reapplied")

    deps = _refresh_dependencies(run, repo, track, pyproject_before)
    if deps.message:
        lines.append(deps.message)
    if not deps.ok:
        return CommandResult(ok=False, message="\n".join(lines), instance=instance)

    if track == "dev" and before != after:
        webui = _rebuild_webui_if_changed(run, repo, before, after)
        if webui.message:
            lines.append(webui.message)
        if not webui.ok:
            return CommandResult(ok=False, message="\n".join(lines), instance=instance)

    return _finish(
        instance, lines, restart=restart, stop=stop, start=start, service_name=service_name
    )


def _handle_dirty(run: Runner, repo: Path, *, discard: bool, stash: bool) -> _Step:
    """Resolve a dirty checkout per the override flags, or refuse."""

    if discard:
        reset = run(["git", "reset", "--hard", "HEAD"], repo)
        if reset.returncode != 0:
            return _Step(False, f"update: discarding local changes failed: {reset.stderr}")
        return _Step(True, "")
    if stash:
        stashed = run(["git", "stash", "push", "-m", "vbot update"], repo)
        if stashed.returncode != 0:
            return _Step(False, f"update: stashing local changes failed: {stashed.stderr}")
        return _Step(True, "")
    return _Step(
        False,
        "update: the checkout has local changes. Commit them, or re-run with "
        "--discard (drop them) or --stash (keep them).",
    )


def _advance_dev(run: Runner, repo: Path) -> _Step:
    """Fast-forward the current branch from its upstream."""

    pull = run(["git", "pull", "--ff-only"], repo)
    if pull.returncode != 0:
        detail = pull.stderr or pull.stdout
        return _Step(
            False,
            f"update: 'git pull --ff-only' failed (branch diverged or offline): {detail}".strip(),
        )
    return _Step(True, "")


def _advance_release(run: Runner, repo: Path, lookup: ReleaseLookup, before: str) -> _Step:
    """Check out the latest release tag and refresh its prebuilt WebUI."""

    try:
        release = lookup()
    except (httpx.HTTPError, ValueError) as exc:
        return _Step(False, f"update: could not query the latest release: {exc}")
    if not release.tag:
        return _Step(False, "update: no published release found to update to")

    fetch = run(["git", "fetch", "--depth", "1", "origin", "tag", release.tag], repo)
    if fetch.returncode != 0:
        return _Step(False, f"update: fetching release {release.tag} failed: {fetch.stderr}")
    checkout = run(["git", "checkout", "--force", release.tag], repo)
    if checkout.returncode != 0:
        return _Step(False, f"update: checking out {release.tag} failed: {checkout.stderr}")

    # Already on the latest tag with an intact WebUI: nothing to re-download.
    after = _head_commit(run, repo)
    dist_present = (repo / "webui" / "dist" / "index.html").is_file()
    if before and before == after and dist_present:
        return _Step(True, "")

    if not release.webui_asset_url:
        return _Step(False, f"update: release {release.tag} has no {WEBUI_ASSET_NAME} asset")
    return _download_webui(release.webui_asset_url, repo)


def _refresh_dependencies(run: Runner, repo: Path, track: str, pyproject_before: str) -> _Step:
    """Reinstall the editable package only when pyproject.toml changed."""

    if _file_digest(repo / "pyproject.toml") == pyproject_before:
        return _Step(True, "")
    extras = DEV_EXTRAS if track == "dev" else RELEASE_EXTRAS
    pip = run([sys.executable, "-m", "pip", "install", "-e", f".[{extras}]"], repo)
    if pip.returncode != 0:
        return _Step(False, f"dependency update failed: {pip.stderr}")
    return _Step(True, f"dependencies reinstalled ([{extras}])")


def _rebuild_webui_if_changed(run: Runner, repo: Path, before: str, after: str) -> _Step:
    """Rebuild the WebUI locally only when the update touched webui/."""

    changed = run(["git", "diff", "--quiet", before, after, "--", "webui"], repo)
    if changed.returncode == 0:
        return _Step(True, "")
    webui_dir = repo / "webui"
    install = run(_npm_command(["install"]), webui_dir)
    if install.returncode != 0:
        return _Step(False, f"webui dependency install failed: {install.stderr}")
    build = run(_npm_command(["run", "build"]), webui_dir)
    if build.returncode != 0:
        return _Step(False, f"webui build failed: {build.stderr}")
    return _Step(True, "webui rebuilt")


def _download_webui(asset_url: str, repo: Path) -> _Step:
    """Download and unpack the prebuilt WebUI asset into webui/dist."""

    try:
        response = httpx.get(
            asset_url,
            follow_redirects=True,
            timeout=_DOWNLOAD_TIMEOUT_SECONDS,
            trust_env=False,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        return _Step(False, f"update: downloading the prebuilt WebUI failed: {exc}")

    webui_dir = repo / "webui"
    webui_dir.mkdir(parents=True, exist_ok=True)
    try:
        _unpack_webui_archive(response.content, webui_dir)
    except (tarfile.TarError, OSError, ValueError) as exc:
        return _Step(False, f"update: unpacking the prebuilt WebUI failed: {exc}")
    if not (webui_dir / "dist" / "index.html").is_file():
        return _Step(False, "update: prebuilt WebUI did not unpack to webui/dist")
    return _Step(True, "")


def _unpack_webui_archive(content: bytes, webui_dir: Path) -> None:
    """Unpack the WebUI tarball, using tarfile's data filter where available.

    The extraction filter (PEP 706) only exists on CPython >= 3.12 and the
    3.11.4+/3.10.12+ backports; the deployment target (Raspberry Pi OS can ship
    3.11.2) may lack it. Feature-detect rather than passing an unknown keyword,
    and fall back to a same-tree guard so unpacking never escapes webui/.
    """

    with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as archive:
        if hasattr(tarfile, "data_filter"):
            archive.extractall(webui_dir, filter="data")  # type: ignore[call-arg]
        else:
            _extract_within(archive, webui_dir)


def _extract_within(archive: tarfile.TarFile, destination: Path) -> None:
    """Extract every member, refusing any path that escapes the destination tree."""

    root = destination.resolve()
    for member in archive.getmembers():
        target = (destination / member.name).resolve()
        if not target.is_relative_to(root):
            raise tarfile.TarError(f"unsafe path in WebUI archive: {member.name}")
    archive.extractall(destination)


def _finish(
    instance: ServerInstance,
    lines: list[str],
    *,
    restart: bool,
    stop: Restart,
    start: Restart,
    service_name: str,
) -> CommandResult:
    """Restart the resolved server target (unless suppressed) and report.

    The restart is systemd-aware: on a unit-managed install it goes through the
    unit rather than fighting it with an out-of-band terminate/start.
    """

    if not restart:
        lines.append("server: not restarted (--no-restart)")
        return CommandResult(ok=True, message="\n".join(lines), instance=instance)

    restarted = restart_server(instance, service_name=service_name, stop=stop, start=start)
    lines.append(f"server: {restarted.message}")
    return CommandResult(ok=restarted.ok, message="\n".join(lines), instance=instance)


def _fetch_latest_release() -> ReleaseInfo:
    """Query the GitHub API for the latest release tag and WebUI asset URL."""

    response = httpx.get(
        f"{GITHUB_API_BASE}/releases/latest",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "vbot-update"},
        follow_redirects=True,
        timeout=_API_TIMEOUT_SECONDS,
        trust_env=False,
    )
    response.raise_for_status()
    payload = response.json()
    tag = str(payload.get("tag_name") or "")
    asset_url: str | None = None
    for asset in payload.get("assets", []):
        if asset.get("name") == WEBUI_ASSET_NAME:
            asset_url = asset.get("browser_download_url")
            break
    return ReleaseInfo(tag=tag, webui_asset_url=asset_url)


def _default_runner(command: list[str], cwd: Path) -> CommandRun:
    # Disable git's interactive credential prompt so a private/auth'd remote
    # fails fast instead of hanging a headless update forever, and cap every
    # command so a stuck git/pip/npm cannot block the update indefinitely.
    environment = dict(os.environ)
    environment["GIT_TERMINAL_PROMPT"] = "0"
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=_COMMAND_TIMEOUT_SECONDS,
            env=environment,
        )
    except subprocess.TimeoutExpired:
        return CommandRun(
            returncode=124,
            stdout="",
            stderr=f"command timed out after {_COMMAND_TIMEOUT_SECONDS:.0f}s: {' '.join(command)}",
        )
    return CommandRun(
        returncode=completed.returncode,
        stdout=(completed.stdout or "").strip(),
        stderr=(completed.stderr or "").strip(),
    )


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _detect_track(run: Runner, repo: Path) -> str:
    branch = run(["git", "symbolic-ref", "-q", "--short", "HEAD"], repo)
    if branch.returncode == 0 and branch.stdout:
        return "dev"
    return "release"


def _is_dirty(run: Runner, repo: Path) -> bool:
    status = run(["git", "status", "--porcelain", "--untracked-files=no"], repo)
    return bool(status.stdout.strip())


def _head_commit(run: Runner, repo: Path) -> str:
    result = run(["git", "rev-parse", "HEAD"], repo)
    return result.stdout.strip() if result.returncode == 0 else ""


def _file_digest(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _npm_command(npm_args: list[str]) -> list[str]:
    if sys.platform == "win32":
        return ["cmd", "/c", "npm", *npm_args]
    return ["npm", *npm_args]


def _short(commit: str) -> str:
    return commit[:9] if commit else "(unknown)"


def _fail(instance: ServerInstance, message: str) -> CommandResult:
    return CommandResult(ok=False, message=message, instance=instance)
