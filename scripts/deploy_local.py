"""Install the committed HEAD into a local Windows installation, without a push.

Builds the same package CI builds for the installation's shape from an exact
export of HEAD (``git archive``, so ignored and untracked files of the
checkout never ship), then installs it with ``vBot.exe update --package``: the
installation's own transactional update, with server stop, data snapshot,
verification start and rollback. The package reports channel ``main``; once CI
publishes the same commit, the installation's ``vbot update`` finds it already
installed. Uncommitted changes to tracked files are refused, because they would
not be part of the package.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tarfile
import time
import tomllib
from collections.abc import Sequence
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cli.application.state import ApplicationError, load_installation, package_name
from scripts.package_build import version_id

CHECKOUT = Path(__file__).resolve().parent.parent
PLATFORM = "windows-x86_64"
#: Outside the checkout, so the exported source tree never meets Ruff, mypy or pytest.
WORK_ROOT = Path.home() / ".cache" / "vbot-deploy"
#: Kept across runs: preparing the runtime's locked dependencies is the slowest build step.
RUNTIME_CACHE = WORK_ROOT / "runtime-cache"


class DeployError(RuntimeError):
    """The package cannot be built or installed."""


def _default_install_root() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "Programs/vBot"


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(CHECKOUT), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if result.returncode != 0:
        raise DeployError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def _committed_revision() -> str:
    changed = _git("status", "--porcelain", "--untracked-files=no").splitlines()
    if changed:
        listed = "\n  ".join(changed[:10])
        more = f"\n  ... and {len(changed) - 10} more" if len(changed) > 10 else ""
        raise DeployError(
            "uncommitted changes would not be part of the package; commit them first:\n  "
            + listed
            + more
        )
    return _git("rev-parse", "HEAD").strip()


def _export_head(destination: Path) -> None:
    destination.mkdir(parents=True)
    with subprocess.Popen(
        ["git", "-C", str(CHECKOUT), "archive", "--format=tar", "HEAD"], stdout=subprocess.PIPE
    ) as process:
        assert process.stdout is not None
        with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
            archive.extractall(destination, filter="data")
    if process.returncode != 0:
        raise DeployError("git archive HEAD failed")
    # The digest-locked search engine is verified again when the package collects it;
    # reusing the checkout's copy only saves the download.
    native = CHECKOUT / "resources" / "native"
    if native.is_dir():
        shutil.copytree(native, destination / "resources" / "native")


def _run(command: Sequence[str | Path], *, cwd: Path) -> None:
    result = subprocess.run([str(part) for part in command], cwd=cwd, check=False)
    if result.returncode != 0:
        raise DeployError(f"{Path(str(command[0])).name} exited with {result.returncode}")


def _build_webui(source: Path) -> None:
    npm = shutil.which("npm")
    if npm is None:
        raise DeployError("npm is required to build the WebUI")
    webui = source / "webui"
    _run([npm, "ci", "--prefer-offline", "--no-audit", "--no-fund"], cwd=webui)
    _run([npm, "run", "build"], cwd=webui)


def _remove(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)


def deploy(install_root: Path, *, keep_build: bool) -> int:
    started = time.monotonic()

    def step(message: str) -> None:
        print(f"[{time.monotonic() - started:6.1f}s] {message}", flush=True)

    try:
        install = load_installation(install_root)
    except (OSError, ApplicationError) as error:
        raise DeployError(f"no vBot installation at {install_root}: {error}") from error
    revision = _committed_revision()

    source = WORK_ROOT / "source"
    output = WORK_ROOT / "build"
    _remove(source)
    _remove(output)
    step(f"Exporting HEAD {revision[:12]}")
    _export_head(source)
    with (source / "pyproject.toml").open("rb") as stream:
        version = str(tomllib.load(stream)["project"]["version"])
    target = version_id(version, revision)
    active = (install.root / "active-version").read_text(encoding="utf-8").strip()
    if target == active:
        _remove(source)
        step(f"{target} is already the active version of {install.root}")
        return 0

    if install.install_shape != "desktop-client":
        step("Building the WebUI")
        _build_webui(source)
    step(f"Building the {install.install_shape} package {target}")
    _run(
        [
            sys.executable,
            source / "scripts" / "build_windows.py",
            "--source",
            source,
            "--runtime",
            sys.base_prefix,
            "--output",
            output,
            "--shape",
            install.install_shape,
            "--version",
            version,
            "--revision",
            revision,
            "--channel",
            "main",
            "--runtime-cache",
            RUNTIME_CACHE,
        ],
        cwd=source,
    )
    archive = output / "artifacts" / package_name(install.install_shape, PLATFORM)

    step(f"Installing into {install.root}")
    # Outside the checkout, so nothing of the development checkout applies to the installation.
    result = subprocess.run(
        [str(install.root / "vBot.exe"), "update", "--package", str(archive)],
        cwd=Path.home(),
        check=False,
    )
    if result.returncode != 0:
        step(f"Update failed with exit code {result.returncode}; build kept in {WORK_ROOT}")
        return result.returncode
    if not keep_build:
        _remove(source)
        _remove(output)
    step(f"Installed {target}")
    return 0


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    value.add_argument(
        "--install",
        type=Path,
        default=_default_install_root(),
        help="Installation root (default: %%LOCALAPPDATA%%\\Programs\\vBot)",
    )
    value.add_argument(
        "--keep-build", action="store_true", help=f"Keep the export and package in {WORK_ROOT}"
    )
    return value


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if sys.platform != "win32":
        print("deploy_local: only Windows installations are supported", file=sys.stderr)
        return 1
    try:
        return deploy(args.install, keep_build=args.keep_build)
    except DeployError as error:
        print(f"deploy_local: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
