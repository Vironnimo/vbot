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

import io
import os
import shutil
import subprocess
import sys
import tarfile
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

import httpx

from cli.install_state import (
    DESKTOP_CLIENT_SHAPE,
    SERVER_DESKTOP_SHAPE,
    InstallState,
    InstallStateError,
    file_digest,
    infer_legacy_install_state,
    read_install_state,
    write_install_state,
)
from cli.server_management import (
    DEFAULT_SERVICE_NAME,
    CommandResult,
    ServerInstance,
    decode_command_output,
    restart_server,
    start_server,
    stop_server,
)
from core.utils.config import APP_DIR

GITHUB_API_BASE = "https://api.github.com/repos/Vironnimo/vbot"
WEBUI_ASSET_NAME = "webui-dist.tar.gz"
_API_TIMEOUT_SECONDS = 30.0
_DOWNLOAD_TIMEOUT_SECONDS = 60.0
_COMMAND_TIMEOUT_SECONDS = 600.0
_WINDOWS_DESKTOP_LAUNCHER_NAME = "vbot-desktop.exe"
_WINDOWS_POWERSHELL = "powershell.exe"

Restart = Callable[[ServerInstance], CommandResult]
UNKNOWN_APP_VERSION = "unknown"


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


@dataclass(frozen=True)
class _DirtyResolution:
    ok: bool
    message: str
    stashed: bool = False


def read_checkout_version(root: Path = APP_DIR) -> str:
    """Read the live vBot version from the checkout being updated."""

    try:
        with (root / "pyproject.toml").open("rb") as handle:
            version = tomllib.load(handle)["project"]["version"]
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError):
        return UNKNOWN_APP_VERSION
    return version if isinstance(version, str) and version else UNKNOWN_APP_VERSION


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
    platform_name: str | None = None,
) -> CommandResult:
    """Advance the installed checkout and optionally restart the server."""

    run = runner or _default_runner
    repo = root if root is not None else APP_DIR
    lookup = latest_release or _fetch_latest_release
    effective_platform = os.name if platform_name is None else platform_name

    if not (repo / ".git").exists():
        return _fail(
            instance,
            f"update: {repo} is not a git checkout; reinstall with the bootstrap to use update",
        )

    track = _detect_track(run, repo)
    before = _head_commit(run, repo)
    if not before:
        return _fail(instance, "update: could not resolve the checkout's current commit")

    lines = [f"update: {track} track"]
    try:
        state = read_install_state(repo)
        if state is None:
            state = infer_legacy_install_state(repo, track=track, revision=before)
            write_install_state(repo, state)
            lines.append(
                f"installation manifest created from the current environment "
                f"(shape={state.install_shape})"
            )
    except (InstallStateError, OSError) as exc:
        return _fail(instance, f"update: installation manifest is not usable: {exc}")
    lines.append(f"install shape: {state.install_shape}")

    dirty_result = run(["git", "status", "--porcelain", "--untracked-files=no"], repo)
    if dirty_result.returncode != 0:
        return _fail(
            instance,
            f"update: checking local changes failed: {dirty_result.stderr or dirty_result.stdout}",
        )
    dirty = bool(dirty_result.stdout.strip())
    stashed = False
    if dirty:
        guard = _handle_dirty(run, repo, discard=discard, stash=stash)
        if not guard.ok:
            return _fail(instance, guard.message)
        stashed = guard.stashed

    release: ReleaseInfo | None = None
    if track == "dev":
        advanced = _advance_dev(run, repo)
    else:
        try:
            release = lookup()
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            return _failure_with_stash(
                instance,
                [f"update: could not query the latest release: {exc}"],
                run,
                repo,
                stashed=stashed,
            )
        if not release.tag:
            return _failure_with_stash(
                instance,
                ["update: no published release found to update to"],
                run,
                repo,
                stashed=stashed,
            )
        asset_required = state.install_shape != DESKTOP_CLIENT_SHAPE and not (
            _current_release_tag(run, repo) == release.tag
            and state.webui_revision == before
            and (repo / "webui" / "dist" / "index.html").is_file()
        )
        if asset_required and not release.webui_asset_url:
            return _failure_with_stash(
                instance,
                [
                    f"update: release {release.tag} has no {WEBUI_ASSET_NAME} asset yet; "
                    "the checkout was left unchanged"
                ],
                run,
                repo,
                stashed=stashed,
            )
        advanced = _advance_release(run, repo, release)
    if not advanced.ok:
        return _failure_with_stash(instance, [advanced.message], run, repo, stashed=stashed)

    after = _head_commit(run, repo)
    if not after:
        return _failure_with_stash(
            instance,
            ["update: could not resolve the updated checkout commit"],
            run,
            repo,
            stashed=stashed,
        )
    if before and before == after:
        lines.append(f"already up to date at {_short(after)}")
    else:
        lines.append(f"updated {_short(before)} -> {_short(after)}")

    deps = _refresh_dependencies(run, repo, state)
    if deps.message:
        lines.append(deps.message)
    if not deps.ok:
        return _failure_with_stash(instance, lines, run, repo, stashed=stashed)
    current_digest = file_digest(repo / "pyproject.toml")
    if state.dependency_digest != current_digest:
        state = replace(state, dependency_digest=current_digest)
        saved = _save_state(repo, state)
        if not saved.ok:
            lines.append(saved.message)
            return _failure_with_stash(instance, lines, run, repo, stashed=stashed)

    shortcut = _refresh_desktop_shortcut(
        run,
        repo,
        state,
        platform_name=effective_platform,
    )
    if shortcut.message:
        lines.append(shortcut.message)
    if not shortcut.ok:
        return _failure_with_stash(instance, lines, run, repo, stashed=stashed)

    if state.install_shape != DESKTOP_CLIENT_SHAPE:
        if track == "dev":
            webui = _refresh_dev_webui(run, repo, state.webui_revision, after)
        else:
            assert release is not None
            webui = _refresh_release_webui(release, repo, state.webui_revision, after)
        if webui.message:
            lines.append(webui.message)
        if not webui.ok:
            return _failure_with_stash(instance, lines, run, repo, stashed=stashed)
        if state.webui_revision != after:
            state = replace(state, webui_revision=after)
            saved = _save_state(repo, state)
            if not saved.ok:
                lines.append(saved.message)
                return _failure_with_stash(instance, lines, run, repo, stashed=stashed)

    state = replace(state, source_track=track, applied_revision=after)
    saved = _save_state(repo, state)
    if not saved.ok:
        lines.append(saved.message)
        return _failure_with_stash(instance, lines, run, repo, stashed=stashed)

    if stashed:
        restored = _restore_stash(run, repo)
        if not restored.ok:
            lines.append(restored.message)
            lines.append(
                "the code, dependencies, and WebUI are updated, but the server was not "
                "restarted; resolve the conflicts and restart it manually"
            )
            return CommandResult(ok=False, message="\n".join(lines), instance=instance)
        lines.append(restored.message)

    return _finish(
        instance,
        lines,
        restart=restart,
        stop=stop,
        start=start,
        service_name=service_name,
        install_shape=state.install_shape,
    )


def _handle_dirty(run: Runner, repo: Path, *, discard: bool, stash: bool) -> _DirtyResolution:
    """Resolve a dirty checkout per the override flags, or refuse."""

    if discard:
        reset = run(["git", "reset", "--hard", "HEAD"], repo)
        if reset.returncode != 0:
            return _DirtyResolution(
                False, f"update: discarding local changes failed: {reset.stderr}"
            )
        return _DirtyResolution(True, "")
    if stash:
        stashed = run(["git", "stash", "push", "-m", "vbot update"], repo)
        if stashed.returncode != 0:
            return _DirtyResolution(
                False, f"update: stashing local changes failed: {stashed.stderr}"
            )
        return _DirtyResolution(True, "", stashed=True)
    return _DirtyResolution(
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


def _advance_release(run: Runner, repo: Path, release: ReleaseInfo) -> _Step:
    """Fetch and check out a release whose required assets passed preflight."""

    fetch = run(["git", "fetch", "--depth", "1", "origin", "tag", release.tag], repo)
    if fetch.returncode != 0:
        return _Step(False, f"update: fetching release {release.tag} failed: {fetch.stderr}")
    checkout = run(["git", "checkout", "--force", release.tag], repo)
    if checkout.returncode != 0:
        return _Step(False, f"update: checking out {release.tag} failed: {checkout.stderr}")

    return _Step(True, "")


def _refresh_dependencies(run: Runner, repo: Path, state: InstallState) -> _Step:
    """Apply the manifest's exact dependency groups until their digest is current."""

    if file_digest(repo / "pyproject.toml") == state.dependency_digest:
        return _Step(True, "")
    extras = ",".join(state.dependency_groups)
    pip = run([state.python_executable, "-m", "pip", "install", "-e", f".[{extras}]"], repo)
    if pip.returncode != 0:
        return _Step(False, f"dependency update failed: {pip.stderr}")
    return _Step(True, f"dependencies reinstalled ([{extras}])")


def _refresh_desktop_shortcut(
    run: Runner,
    repo: Path,
    state: InstallState,
    *,
    platform_name: str,
) -> _Step:
    """Point installer-owned Windows Desktop shortcuts at the GUI launcher."""

    desktop_shapes = {SERVER_DESKTOP_SHAPE, DESKTOP_CLIENT_SHAPE}
    if platform_name != "nt" or state.install_shape not in desktop_shapes:
        return _Step(True, "")

    desktop_launcher = (
        Path(state.python_executable).parent / _WINDOWS_DESKTOP_LAUNCHER_NAME
    ).resolve()
    if not desktop_launcher.is_file():
        return _Step(False, f"desktop shortcut update failed: {desktop_launcher} is missing")

    setup_script = (repo / "scripts" / "setup.ps1").resolve()
    if not setup_script.is_file():
        return _Step(False, f"desktop shortcut update failed: {setup_script} is missing")

    refreshed = run(
        [
            _WINDOWS_POWERSHELL,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(setup_script),
            "-DesktopShortcutTarget",
            str(desktop_launcher),
        ],
        repo,
    )
    if refreshed.returncode != 0:
        detail = refreshed.stderr or refreshed.stdout
        return _Step(False, f"desktop shortcut update failed: {detail}")
    return _Step(True, "desktop shortcut refreshed")


def _refresh_dev_webui(
    run: Runner, repo: Path, applied_revision: str | None, target_revision: str
) -> _Step:
    """Bring a branch install's local WebUI to the target revision idempotently."""

    dist_present = (repo / "webui" / "dist" / "index.html").is_file()
    if applied_revision == target_revision and dist_present:
        return _Step(True, "")
    if applied_revision and dist_present:
        changed = run(
            ["git", "diff", "--quiet", applied_revision, target_revision, "--", "webui"],
            repo,
        )
        if changed.returncode == 0:
            return _Step(True, "webui unchanged")
    webui_dir = repo / "webui"
    install = run(_npm_command(["ci"]), webui_dir)
    if install.returncode != 0:
        return _Step(False, f"webui dependency install failed: {install.stderr}")
    build = run(_npm_command(["run", "build"]), webui_dir)
    if build.returncode != 0:
        return _Step(False, f"webui build failed: {build.stderr}")
    return _Step(True, "webui rebuilt")


def _refresh_release_webui(
    release: ReleaseInfo, repo: Path, applied_revision: str | None, target_revision: str
) -> _Step:
    """Apply the release asset until the manifest and on-disk bundle match HEAD."""

    dist_present = (repo / "webui" / "dist" / "index.html").is_file()
    if applied_revision == target_revision and dist_present:
        return _Step(True, "")
    if not release.webui_asset_url:
        return _Step(False, f"update: release {release.tag} has no {WEBUI_ASSET_NAME} asset")
    downloaded = _download_webui(release.webui_asset_url, repo)
    if downloaded.ok:
        return _Step(True, "prebuilt webui installed")
    return downloaded


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
    """Unpack the WebUI tarball, replacing webui/dist wholesale.

    Extraction goes into a staging directory first and the finished dist is
    swapped in afterwards: a corrupt archive never costs the existing dist, and
    replacing (rather than overlaying) keeps hashed bundles from older releases
    from accumulating across updates.

    The extraction filter (PEP 706) only exists on CPython >= 3.12 and the
    3.11.4+/3.10.12+ backports; the deployment target (Raspberry Pi OS can ship
    3.11.2) may lack it. Feature-detect rather than passing an unknown keyword,
    and fall back to a same-tree guard so unpacking never escapes webui/.
    """

    staging = webui_dir / "dist.staging"
    backup = webui_dir / "dist.backup"
    if staging.exists():
        shutil.rmtree(staging)
    if backup.exists():
        shutil.rmtree(backup)
    staging.mkdir(parents=True)
    try:
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as archive:
            if hasattr(tarfile, "data_filter"):
                archive.extractall(staging, filter="data")  # type: ignore[call-arg]
            else:
                _extract_within(archive, staging)
        staged_dist = staging / "dist"
        if not staged_dist.is_dir():
            raise ValueError("WebUI archive does not contain dist/")
        dist_dir = webui_dir / "dist"
        if dist_dir.exists():
            dist_dir.rename(backup)
        try:
            staged_dist.rename(dist_dir)
        except OSError:
            if backup.exists() and not dist_dir.exists():
                backup.rename(dist_dir)
            raise
        shutil.rmtree(backup, ignore_errors=True)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        dist_dir = webui_dir / "dist"
        if backup.exists() and not dist_dir.exists():
            backup.rename(dist_dir)
        elif backup.exists():
            shutil.rmtree(backup, ignore_errors=True)


def _extract_within(archive: tarfile.TarFile, destination: Path) -> None:
    """Extract every member, refusing links and paths that escape the destination tree."""

    root = destination.resolve()
    for member in archive.getmembers():
        # A link member can redirect later members outside the tree after this
        # pre-check has passed (the TOCTOU the stdlib data filter guards
        # against), so refuse links outright — the WebUI bundle contains none.
        if not (member.isdir() or member.isfile()):
            raise tarfile.TarError(f"unsafe member type in WebUI archive: {member.name}")
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
    install_shape: str,
) -> CommandResult:
    """Restart the resolved server target (unless suppressed) and report.

    The restart is systemd-aware: on a unit-managed install it goes through the
    unit rather than fighting it with an out-of-band terminate/start.
    """

    if install_shape == DESKTOP_CLIENT_SHAPE:
        lines.append("server: not applicable (desktop-client install)")
        return CommandResult(ok=True, message="\n".join(lines), instance=instance)

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
            timeout=_COMMAND_TIMEOUT_SECONDS,
            env=environment,
        )
    except subprocess.TimeoutExpired:
        return CommandRun(
            returncode=124,
            stdout="",
            stderr=f"command timed out after {_COMMAND_TIMEOUT_SECONDS:.0f}s: {' '.join(command)}",
        )
    except OSError as exc:
        return CommandRun(returncode=127, stdout="", stderr=f"could not run {command[0]}: {exc}")
    return CommandRun(
        returncode=completed.returncode,
        stdout=decode_command_output(completed.stdout).strip(),
        stderr=decode_command_output(completed.stderr).strip(),
    )


def _detect_track(run: Runner, repo: Path) -> str:
    branch = run(["git", "symbolic-ref", "-q", "--short", "HEAD"], repo)
    if branch.returncode == 0 and branch.stdout:
        return "dev"
    return "release"


def _head_commit(run: Runner, repo: Path) -> str:
    result = run(["git", "rev-parse", "HEAD"], repo)
    return result.stdout.strip() if result.returncode == 0 else ""


def _current_release_tag(run: Runner, repo: Path) -> str:
    result = run(["git", "describe", "--tags", "--exact-match", "HEAD"], repo)
    return result.stdout.strip() if result.returncode == 0 else ""


def _save_state(repo: Path, state: InstallState) -> _Step:
    try:
        write_install_state(repo, state)
    except (InstallStateError, OSError) as exc:
        return _Step(False, f"update: saving the installation manifest failed: {exc}")
    return _Step(True, "")


def _restore_stash(run: Runner, repo: Path) -> _Step:
    popped = run(["git", "stash", "pop", "stash@{0}"], repo)
    if popped.returncode == 0:
        return _Step(True, "local changes reapplied")
    detail = popped.stderr or popped.stdout
    return _Step(
        False,
        "reapplying stashed changes hit a conflict; resolve it in the repo "
        f"(the stash was kept; see 'git stash list'): {detail}",
    )


def _failure_with_stash(
    instance: ServerInstance,
    lines: list[str],
    run: Runner,
    repo: Path,
    *,
    stashed: bool,
) -> CommandResult:
    messages = list(lines)
    if stashed:
        restored = _restore_stash(run, repo)
        messages.append(restored.message)
    return CommandResult(ok=False, message="\n".join(messages), instance=instance)


def _npm_command(npm_args: list[str]) -> list[str]:
    if sys.platform == "win32":
        return ["cmd", "/c", "npm", *npm_args]
    return ["npm", *npm_args]


def _short(commit: str) -> str:
    return commit[:9] if commit else "(unknown)"


def _fail(instance: ServerInstance, message: str) -> CommandResult:
    return CommandResult(ok=False, message=message, instance=instance)
