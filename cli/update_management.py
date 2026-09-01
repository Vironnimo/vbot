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
import shlex
import shutil
import subprocess
import sys
import tarfile
import tomllib
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

import httpx
import psutil  # type: ignore[import-untyped]

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
    has_vbot_run_context,
    probe_health,
    restart_server,
    schedule_server_restart,
    start_server,
    stop_server,
)
from core.utils.atomic import atomic_write_bytes
from core.utils.config import VBOT_ROOT

GITHUB_API_BASE = "https://api.github.com/repos/Vironnimo/vbot"
WEBUI_ASSET_NAME = "webui-dist.tar.gz"
_API_TIMEOUT_SECONDS = 30.0
_DOWNLOAD_TIMEOUT_SECONDS = 60.0
_COMMAND_TIMEOUT_SECONDS = 600.0
_WINDOWS_COMMAND_LAUNCHER_NAME = "vbot.exe"
_WINDOWS_COMMAND_SHIM_RELATIVE_PATH = Path("bin") / "vbot.cmd"
_WINDOWS_DESKTOP_LAUNCHER_NAME = "vbot-desktop.exe"
_WINDOWS_POWERSHELL = "powershell.exe"
_DESKTOP_INSTALL_SHAPES = frozenset({SERVER_DESKTOP_SHAPE, DESKTOP_CLIENT_SHAPE})

Restart = Callable[[ServerInstance], CommandResult]
ResolveInstance = Callable[..., ServerInstance]
UNKNOWN_VBOT_VERSION = "unknown"


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


def _ensure_update_session_snapshot(instance: ServerInstance) -> _Step:
    """Create the mandatory current-format Session snapshot before code changes."""

    from cli.rpc_client import rpc_call
    from core.sessions.format import read_session_store_marker

    data_dir = instance.data_dir
    database = data_dir / "sessions.db"
    try:
        marker = read_session_store_marker(data_dir)
    except Exception as exc:
        return _Step(False, f"update: Session-store marker cannot be read: {exc}")
    if marker is None:
        if database.exists():
            return _Step(
                False,
                "update: a sessions.db exists without a current-format Session marker; "
                "refusing to update without a verified Session snapshot",
            )
        return _Step(True, "")
    if marker["state"] == "bootstrap" and not database.exists():
        return _Step(True, "")
    if not database.is_file():
        return _Step(
            False, "update: current-format Session marker exists but sessions.db is missing"
        )

    health = probe_health(instance)
    if health.reachable:
        if not health.is_vbot:
            return _Step(False, "update: the target port is occupied by a non-vBot process")
        payload = rpc_call(instance, "session_store.snapshot_create", {"reason": "update"})
        if not payload.ok:
            return _Step(False, f"update: pre-update Session snapshot failed: {payload.message}")
        snapshot = payload.data.get("snapshot")
        snapshot_id = snapshot.get("snapshot_id") if isinstance(snapshot, dict) else None
        if not isinstance(snapshot_id, str) or not snapshot_id:
            return _Step(False, "update: pre-update Session snapshot response was incomplete")
        return _Step(True, f"pre-update Session snapshot: {snapshot_id}")

    from core.sessions.snapshots import create_snapshot
    from core.sessions.store import SessionStore

    store = None
    try:
        store = SessionStore(database)
        snapshot = create_snapshot(
            data_dir,
            database,
            store.backup,
            database_id=str(marker["database_id"]),
            reason="update",
        )
    except Exception as exc:
        return _Step(False, f"update: offline Session snapshot failed: {exc}")
    finally:
        if store is not None:
            store.close()
    if snapshot is None:
        return _Step(False, "update: offline Session snapshot was not verified")
    return _Step(True, f"pre-update Session snapshot: {snapshot.name}")


@dataclass(frozen=True)
class _DirtyResolution:
    ok: bool
    message: str
    stashed: _UpdateStash | None = None


@dataclass(frozen=True)
class _UpdateStash:
    """Exact temporary Git ref holding one updater-created stash object."""

    object_id: str
    reference: str


def read_checkout_version(root: Path = VBOT_ROOT) -> str:
    """Read the live vBot version from the checkout being updated."""

    try:
        with (root / "pyproject.toml").open("rb") as handle:
            version = tomllib.load(handle)["project"]["version"]
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError):
        return UNKNOWN_VBOT_VERSION
    return version if isinstance(version, str) and version else UNKNOWN_VBOT_VERSION


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
    resolve: ResolveInstance | None = None,
    host: str | None = None,
    port: int | None = None,
    data_dir: str | Path | None = None,
    session_snapshot_fn: Callable[[ServerInstance], _Step] = _ensure_update_session_snapshot,
) -> CommandResult:
    """Advance the installed checkout and optionally restart the server."""

    run = runner or _default_runner
    repo = root if root is not None else VBOT_ROOT
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
    inferred_state = False
    try:
        state = read_install_state(repo)
        if state is None:
            state = infer_legacy_install_state(repo, track=track, revision=before)
            inferred_state = True
    except (InstallStateError, OSError) as exc:
        return _fail(instance, f"update: installation manifest is not usable: {exc}")

    try:
        instance = _resolve_update_instance(
            instance,
            state,
            resolve=resolve,
            host=host,
            port=port,
            data_dir=data_dir,
        )
    except (OSError, ValueError) as exc:
        return _fail(instance, f"update: server target is not usable: {exc}")

    package_launcher_guard = _guard_windows_package_launcher_not_running(
        state,
        repo,
        platform_name=effective_platform,
    )
    if not package_launcher_guard.ok:
        return _fail(instance, package_launcher_guard.message)

    desktop_guard = _guard_windows_desktop_not_running(
        state,
        repo,
        platform_name=effective_platform,
        checkout_unchanged=True,
    )
    if not desktop_guard.ok:
        return _fail(instance, desktop_guard.message)

    session_snapshot = session_snapshot_fn(instance)
    if session_snapshot.message:
        lines.append(session_snapshot.message)
    if not session_snapshot.ok:
        return _fail(instance, session_snapshot.message)

    if inferred_state:
        try:
            write_install_state(repo, state)
        except (InstallStateError, OSError) as exc:
            return _fail(
                instance, f"update: saving the inferred installation manifest failed: {exc}"
            )
        lines.append(
            f"installation manifest created from the current environment "
            f"(shape={state.install_shape})"
        )
    lines.append(f"install shape: {state.install_shape}")

    dirty_result = run(["git", "status", "--porcelain", "--untracked-files=no"], repo)
    if dirty_result.returncode != 0:
        return _fail(
            instance,
            f"update: checking local changes failed: {dirty_result.stderr or dirty_result.stdout}",
        )
    dirty = bool(dirty_result.stdout.strip())
    stashed: _UpdateStash | None = None
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

    deps = _refresh_dependencies(
        run,
        repo,
        state,
        platform_name=effective_platform,
    )
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

    command_shim = _refresh_windows_command_shim(
        repo,
        state,
        platform_name=effective_platform,
    )
    if command_shim.message:
        lines.append(command_shim.message)
    if not command_shim.ok:
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

    if stashed is not None:
        restored = _restore_stash(run, repo, stashed)
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


def _resolve_update_instance(
    current: ServerInstance,
    state: InstallState,
    *,
    resolve: ResolveInstance | None,
    host: str | None,
    port: int | None,
    data_dir: str | Path | None,
) -> ServerInstance:
    """Apply explicit target fields over the Installer-recorded server target."""

    if resolve is None:
        return current
    return resolve(
        host=(
            host
            if host is not None
            else state.server_host
            if state.server_host is not None
            else current.host
        ),
        port=(
            port
            if port is not None
            else state.server_port
            if state.server_port is not None
            else current.port
        ),
        data_dir=(
            data_dir
            if data_dir is not None
            else state.server_data_directory
            if state.server_data_directory is not None
            else current.data_dir
        ),
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
        created = run(["git", "stash", "create", "vbot update"], repo)
        if created.returncode != 0:
            return _DirtyResolution(
                False, f"update: snapshotting local changes failed: {created.stderr}"
            )
        object_id = created.stdout.strip()
        if not object_id:
            return _DirtyResolution(True, "")
        reference = f"refs/vbot/update-stashes/{uuid.uuid4().hex}"
        retained = run(["git", "update-ref", reference, object_id], repo)
        if retained.returncode != 0:
            return _DirtyResolution(
                False,
                "update: retaining the local-change snapshot failed: "
                f"{retained.stderr or retained.stdout}",
            )
        cleared = run(["git", "reset", "--hard", "HEAD"], repo)
        if cleared.returncode != 0:
            return _DirtyResolution(
                False,
                "update: preparing the checkout after snapshotting local changes failed; "
                f"the exact recovery snapshot remains at {reference}: "
                f"{cleared.stderr or cleared.stdout}",
            )
        return _DirtyResolution(
            True,
            "",
            stashed=_UpdateStash(object_id=object_id, reference=reference),
        )
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


def _guard_windows_desktop_not_running(
    state: InstallState,
    repo: Path,
    *,
    platform_name: str,
    checkout_unchanged: bool,
) -> _Step:
    """Refuse a Windows update while this installation's Desktop launcher is running."""

    if platform_name != "nt" or state.install_shape not in _DESKTOP_INSTALL_SHAPES:
        return _Step(True, "")

    desktop_launcher = _windows_desktop_launcher(state)
    process_id = _running_process_id(desktop_launcher)
    if process_id is None:
        return _Step(True, "")

    recovery = _resume_update_command(repo, state, platform_name=platform_name)
    unchanged_detail = (
        "no checkout or installation files were changed"
        if checkout_unchanged
        else "dependency installation was not started"
    )
    return _Step(
        False,
        f"update: vBot Desktop is running from {desktop_launcher} (process {process_id}). "
        f"Close every vBot Desktop window before updating; {unchanged_detail}.\n"
        f"resume update: {recovery}",
    )


def _guard_windows_package_launcher_not_running(
    state: InstallState,
    repo: Path,
    *,
    platform_name: str,
) -> _Step:
    """Keep pip away from an active Windows package launcher."""

    if platform_name != "nt":
        return _Step(True, "")

    package_launcher = (
        Path(state.python_executable).parent / _WINDOWS_COMMAND_LAUNCHER_NAME
    ).resolve()
    process_id = _running_process_id(package_launcher, include_current=True)
    if process_id is None:
        return _Step(True, "")

    recovery = _resume_update_command(repo, state, platform_name=platform_name)
    return _Step(
        False,
        f"update: the Windows package launcher is active at {package_launcher} "
        f"(process {process_id}). It cannot update itself while Windows holds the file; "
        "no checkout or installation files were changed. Run this one-time recovery "
        "command, which also migrates the installer-owned command shim:\n"
        f"resume update: {recovery}",
    )


def _running_process_id(executable: Path, *, include_current: bool = False) -> int | None:
    """Return a process id only for the exact Windows executable path."""

    target = _normalized_windows_path(executable)
    try:
        for process in psutil.process_iter(["pid", "exe"]):
            try:
                process_id = int(process.info["pid"])
                process_executable = process.info["exe"]
            except (KeyError, TypeError, ValueError, psutil.Error):
                continue
            if (process_id == os.getpid() and not include_current) or not process_executable:
                continue
            if _normalized_windows_path(Path(process_executable)) == target:
                return process_id
    except psutil.Error:
        return None
    return None


def _refresh_windows_command_shim(
    repo: Path,
    state: InstallState,
    *,
    platform_name: str,
) -> _Step:
    """Migrate the public Windows Installer's command shim away from vbot.exe."""

    if platform_name != "nt":
        return _Step(True, "")

    shim = repo / _WINDOWS_COMMAND_SHIM_RELATIVE_PATH
    if not shim.is_file():
        return _Step(True, "")

    escaped_python = state.python_executable.replace("%", "%%")
    content = f'@echo off\r\n"{escaped_python}" -m cli.main %*\r\n'
    encoded_content = content.encode("utf-8")
    try:
        if shim.read_bytes() == encoded_content:
            return _Step(True, "")
        atomic_write_bytes(shim, encoded_content)
    except OSError as exc:
        return _Step(False, f"command launcher update failed: {exc}")
    return _Step(True, "command launcher refreshed")


def _normalized_windows_path(path: Path) -> str:
    """Normalize an executable path with Windows' case-insensitive semantics."""

    return str(path.resolve()).replace("/", "\\").casefold()


def _refresh_dependencies(
    run: Runner,
    repo: Path,
    state: InstallState,
    *,
    platform_name: str,
) -> _Step:
    """Apply the manifest's exact dependency groups until their digest is current."""

    if file_digest(repo / "pyproject.toml") == state.dependency_digest:
        return _Step(True, "")

    desktop_guard = _guard_windows_desktop_not_running(
        state,
        repo,
        platform_name=platform_name,
        checkout_unchanged=False,
    )
    if not desktop_guard.ok:
        return desktop_guard

    extras = ",".join(state.dependency_groups)
    pip = run([state.python_executable, "-m", "pip", "install", "-e", f".[{extras}]"], repo)
    if pip.returncode != 0:
        detail = pip.stderr or pip.stdout
        recovery = _resume_update_command(repo, state, platform_name=platform_name)
        desktop_instruction = (
            " and closing vBot Desktop"
            if platform_name == "nt" and state.install_shape in _DESKTOP_INSTALL_SHAPES
            else ""
        )
        return _Step(
            False,
            f"dependency update failed: {detail}\n"
            f"After resolving the error{desktop_instruction}, resume without relying on "
            f"the package launcher:\nresume update: {recovery}",
        )
    return _Step(True, f"dependencies reinstalled ([{extras}])")


def _refresh_desktop_shortcut(
    run: Runner,
    repo: Path,
    state: InstallState,
    *,
    platform_name: str,
) -> _Step:
    """Point installer-owned Windows Desktop shortcuts at the GUI launcher."""

    if platform_name != "nt" or state.install_shape not in _DESKTOP_INSTALL_SHAPES:
        return _Step(True, "")

    desktop_launcher = _windows_desktop_launcher(state)
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


def _windows_desktop_launcher(state: InstallState) -> Path:
    """Return the Desktop GUI launcher owned by the recorded Python environment."""

    return (Path(state.python_executable).parent / _WINDOWS_DESKTOP_LAUNCHER_NAME).resolve()


def _resume_update_command(
    repo: Path,
    state: InstallState,
    *,
    platform_name: str,
) -> str:
    """Build a recovery command that works even when the package launcher was removed."""

    if platform_name == "nt":
        root = _powershell_literal(str(repo.resolve()))
        python = _powershell_literal(state.python_executable)
        return f"Set-Location -LiteralPath {root}; & {python} -m cli.main update"

    return (
        f"cd {shlex.quote(str(repo.resolve()))} && "
        f"{shlex.quote(state.python_executable)} -m cli.main update"
    )


def _powershell_literal(value: str) -> str:
    """Quote one PowerShell literal without allowing interpolation."""

    return "'" + value.replace("'", "''") + "'"


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

    if has_vbot_run_context():
        restarted = schedule_server_restart(instance, service_name=service_name)
    else:
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


def _restore_stash(run: Runner, repo: Path, stash: _UpdateStash) -> _Step:
    applied = run(["git", "stash", "apply", "--index", stash.reference], repo)
    if applied.returncode == 0:
        released = run(
            ["git", "update-ref", "-d", stash.reference, stash.object_id],
            repo,
        )
        if released.returncode == 0:
            return _Step(True, "local changes reapplied")
        return _Step(
            False,
            "local changes were reapplied, but the updater could not remove its recovery "
            f"snapshot at {stash.reference}: {released.stderr or released.stdout}",
        )
    detail = applied.stderr or applied.stdout
    return _Step(
        False,
        "reapplying stashed changes hit a conflict; resolve it in the repo "
        f"(the exact snapshot remains at {stash.reference}): {detail}",
    )


def _failure_with_stash(
    instance: ServerInstance,
    lines: list[str],
    run: Runner,
    repo: Path,
    *,
    stashed: _UpdateStash | None,
) -> CommandResult:
    messages = list(lines)
    if stashed is not None:
        restored = _restore_stash(run, repo, stashed)
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
