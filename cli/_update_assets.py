"""Build or atomically replace the complete installed WebUI and Extension asset trees."""

from __future__ import annotations

import io
import shutil
import sys
import tarfile
from pathlib import Path

import httpx

from cli._update_types import (
    ReleaseInfo,
    Runner,
    _Step,
)

WEBUI_ASSET_NAME = "webui-dist.tar.gz"


_DOWNLOAD_TIMEOUT_SECONDS = 60.0


def _refresh_dev_webui(
    run: Runner, repo: Path, applied_revision: str | None, target_revision: str
) -> _Step:
    """Bring a branch install's local WebUI to the target revision idempotently."""

    dist_present = (repo / "webui" / "dist" / "index.html").is_file()
    if applied_revision == target_revision and dist_present:
        return _Step(True, "")
    if applied_revision and dist_present:
        changed = run(
            [
                "git",
                "diff",
                "--quiet",
                applied_revision,
                target_revision,
                "--",
                "webui",
                ":(glob)resources/extensions/*/ui/**",
                ":(glob)tests/fixtures/extension-pages/*/ui/**",
            ],
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
    """Stage and replace built assets together, restoring all prior trees on failure.

    Release bundles use repository-relative paths. Older dist-only bundles remain
    readable. Only generated web trees are installed from bundled Extensions;
    their Python sources continue to belong to the checkout update.
    """
    webui_dir = webui_dir.absolute()
    root = webui_dir.parent.resolve()

    def checked(path: Path) -> Path:
        if path.resolve() != path or not path.is_relative_to(root) or path == root:
            raise ValueError(
                "WebUI asset destination must remain inside the installation directory."
            )
        return path

    checked(webui_dir)
    staging = webui_dir / "dist.staging"
    backup = webui_dir / "dist.backup"
    checked(staging)
    checked(backup)
    webui_dir.mkdir(parents=True, exist_ok=True)
    if staging.exists():
        shutil.rmtree(staging)
    # Preserve a retained recovery directory instead of overwriting it after
    # an interrupted filesystem rollback.
    backup.mkdir()
    changes: list[tuple[Path, Path, bool]] = []
    recovered = False
    try:
        staging.mkdir(parents=True)
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as archive:
            _extract_within(archive, staging)
        repository_layout = (staging / "webui" / "dist").is_dir()
        staged_dist = staging / "webui" / "dist" if repository_layout else staging / "dist"
        if not (staged_dist / "index.html").is_file():
            raise ValueError("WebUI archive does not contain dist/index.html")
        targets: list[tuple[Path | None, Path]] = [(staged_dist, checked(webui_dir / "dist"))]
        if repository_layout:
            staged_extensions = staging / "resources" / "extensions"
            installed_extensions = checked(root / "resources" / "extensions")
            names = {
                directory.name
                for parent in (staged_extensions, installed_extensions)
                if parent.is_dir()
                for directory in parent.iterdir()
                if directory.is_dir() and (directory / "web").exists()
            }
            for name in sorted(names):
                target = checked(installed_extensions / name / "web")
                staged_page = staged_extensions / name / "web"
                targets.append((staged_page if staged_page.is_dir() else None, target))
        try:
            for index, (source, target) in enumerate(targets):
                previous = backup / str(index)
                had_previous = target.exists()
                if had_previous:
                    target.rename(previous)
                changes.append((target, previous, had_previous))
                if source is not None:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    source.rename(target)
        except BaseException:
            for target, previous, had_previous in reversed(changes):
                if target.exists():
                    shutil.rmtree(checked(target))
                if had_previous:
                    previous.rename(checked(target))
            recovered = True
            raise
        recovered = True
    finally:
        shutil.rmtree(checked(staging), ignore_errors=True)
        if recovered or not changes:
            shutil.rmtree(checked(backup), ignore_errors=True)


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


def _npm_command(npm_args: list[str]) -> list[str]:
    if sys.platform == "win32":
        return ["cmd", "/c", "npm", *npm_args]
    return ["npm", *npm_args]
