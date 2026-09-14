"""Bound Git source updates for one native application installation."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from cli.application.state import (
    ApplicationError,
    Installation,
    contained,
    read_json,
    safe_id,
    write_json,
)
from core.utils.atomic import atomic_write_text
from core.utils.ids import new_id
from core.utils.processes import subprocess_creation_flags

CANONICAL_REPOSITORY = "https://github.com/Vironnimo/vbot.git"
MAIN_BRANCH = "main"
_LOGGER = logging.getLogger("vbot.application.source_updates")


def _git(checkout: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=checkout,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
            creationflags=subprocess_creation_flags(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ApplicationError(f"Source update Git operation could not run ({args[0]})") from exc
    if check and result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise ApplicationError(f"Source update Git operation failed ({args[0]}): {detail}")
    return result


def inspect_checkout(checkout: Path) -> dict[str, Any]:
    """Return the exact root and same-name upstream binding for a checkout."""

    checkout = checkout.expanduser().resolve()
    root = Path(_git(checkout, "rev-parse", "--show-toplevel").stdout.strip()).resolve()
    if root != checkout:
        raise ApplicationError("Source update checkout must be the exact Git repository root")
    branch = _git(checkout, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    if branch.returncode or not branch.stdout.strip():
        raise ApplicationError("Source update checkout must be on a named branch")
    branch_name = branch.stdout.strip()
    remote = _git(checkout, "config", "--get", f"branch.{branch_name}.remote", check=False)
    merge = _git(checkout, "config", "--get", f"branch.{branch_name}.merge", check=False)
    if (
        remote.returncode
        or merge.returncode
        or not remote.stdout.strip()
        or not merge.stdout.strip()
    ):
        raise ApplicationError("Source update branch must have an upstream")
    remote_name = remote.stdout.strip()
    if remote_name == "." or merge.stdout.strip() != f"refs/heads/{branch_name}":
        raise ApplicationError("Source update branch must track its same-named remote branch")
    return {
        "schema_version": 1,
        "checkout": str(checkout),
        "remote": remote_name,
        "branch": branch_name,
    }


def _save_binding(install: Installation, binding: dict[str, Any]) -> dict[str, Any]:
    previous = read_binding(install)
    write_json(contained(install.root, "source-update.json"), binding)
    if binding != previous:
        _LOGGER.info(
            "Application source selection changed",
            extra={
                "operation": "source.select",
                "source_track": "main",
                "checkout": binding["checkout"],
                "branch": binding["branch"],
            },
        )
    return binding


def bind_checkout(install: Installation, checkout: Path) -> dict[str, Any]:
    return _save_binding(install, inspect_checkout(checkout))


def bind_main_source(install: Installation, checkout: Path | None = None) -> dict[str, Any]:
    """Bind canonical main, cloning an installation-owned checkout by default."""

    if checkout is not None:
        binding = inspect_checkout(checkout)
        _require_clean(Path(binding["checkout"]))
        return _save_binding(install, binding)
    destination = contained(install.root, "source")
    existing = read_binding(install)
    if existing is not None:
        existing_checkout = Path(existing["checkout"])
        if inspect_checkout(existing_checkout) != existing:
            raise ApplicationError("The bound source no longer matches its recorded branch")
        _require_clean(existing_checkout)
        return existing
    if destination.exists():
        binding = inspect_checkout(destination)
        _require_clean(destination)
        if binding["branch"] != MAIN_BRANCH:
            raise ApplicationError("Installation-owned source must use the main branch")
        remote_url = _git(destination, "remote", "get-url", binding["remote"]).stdout.strip()
        if remote_url.rstrip("/") != CANONICAL_REPOSITORY.rstrip("/"):
            raise ApplicationError("Installation-owned main source is not the canonical repository")
        return bind_checkout(install, destination)
    staging = contained(install.root, "source-clones")
    staging.mkdir(parents=True, exist_ok=True)

    def claim_clone(candidate: str) -> bool:
        temporary = contained(install.root, f"source-clones/{candidate}")
        try:
            temporary.mkdir()
        except FileExistsError:
            return False
        return True

    clone_id = new_id("clone", claim=claim_clone)
    temporary = contained(install.root, f"source-clones/{clone_id}")
    environment = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    try:
        result = subprocess.run(
            [
                "git",
                "clone",
                "--branch",
                MAIN_BRANCH,
                "--single-branch",
                "--",
                CANONICAL_REPOSITORY,
                str(temporary),
            ],
            cwd=staging,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
            creationflags=subprocess_creation_flags(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log = contained(install.root, "development/source-update.log")
        atomic_write_text(log, f"Canonical main source clone could not run: {exc}\n")
        raise ApplicationError(
            f"Canonical main source clone could not run. Inspect {log} and {temporary}"
        ) from exc
    log = contained(install.root, "development/source-update.log")
    atomic_write_text(log, result.stdout + result.stderr)
    if result.returncode:
        raise ApplicationError(f"Canonical main source clone failed. Inspect {log} and {temporary}")
    try:
        temporary.rename(destination)
    except OSError as exc:
        raise ApplicationError(
            f"Canonical main source was cloned but could not be published. Inspect {temporary}"
        ) from exc
    return bind_checkout(install, destination)


def select_source(
    install: Installation,
    mode: str,
    *,
    from_checkout: Path | None = None,
) -> dict[str, Any]:
    """Select the release stream or bind a main source for later durable updates."""

    if mode == "release":
        if from_checkout is not None:
            raise ApplicationError("A checkout can only be supplied for the main source track")
        binding_path = contained(install.root, "source-update.json")
        changed = binding_path.exists()
        try:
            binding_path.unlink(missing_ok=True)
        except OSError as exc:
            raise ApplicationError("Could not clear the source update binding") from exc
        if changed:
            _LOGGER.info(
                "Application source selection changed",
                extra={"operation": "source.select", "source_track": "release"},
            )
        return {"source_track": "release", "checkout": None}
    if mode != "main":
        raise ApplicationError("Source track must be release or main")
    binding = bind_main_source(install, from_checkout)
    _require_clean(Path(binding["checkout"]))
    return {"source_track": "main", **binding}


def read_binding(install: Installation) -> dict[str, Any] | None:
    path = contained(install.root, "source-update.json")
    if not path.exists():
        return None
    binding = read_json(path, limit=16 * 1024)
    if set(binding) != {"schema_version", "checkout", "remote", "branch"}:
        raise ApplicationError("Invalid source update binding fields")
    checkout, remote, branch = (
        binding.get("checkout"),
        binding.get("remote"),
        binding.get("branch"),
    )
    if (
        binding.get("schema_version") != 1
        or not isinstance(checkout, str)
        or not Path(checkout).is_absolute()
        or not isinstance(remote, str)
        or not remote
        or not isinstance(branch, str)
        or not branch
    ):
        raise ApplicationError("Invalid source update binding")
    return binding


def _require_clean(checkout: Path) -> None:
    status = _git(checkout, "status", "--porcelain", "--untracked-files=all").stdout
    if status.strip():
        raise ApplicationError(
            f"The source checkout has uncommitted changes: {checkout}. "
            "Commit or resolve them before running vbot update again; "
            "the running version is unchanged."
        )


def _require_head(checkout: Path, revision: str) -> None:
    if _git(checkout, "rev-parse", "HEAD").stdout.strip() != revision:
        raise ApplicationError("Source update checkout changed while its candidate was prepared")


def build_inputs(checkout: Path, shape: str) -> dict[str, str]:
    """Record the locked runtime recipe and all tracked WebUI build inputs."""
    lock = checkout / "scripts" / "windows" / f"requirements-{shape}.lock"
    if not lock.is_file():
        raise ApplicationError(f"The Windows runtime dependency lock is missing: {lock}")
    project = tomllib.loads((checkout / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    groups = {
        "server": ("server", "windows-app"),
        "server-desktop": ("server", "windows-app", "desktop"),
        "desktop-client": ("cli", "windows-app", "desktop"),
    }[shape]
    optional = project.get("optional-dependencies", {})
    requirements = json.dumps(
        [project.get("dependencies", []), {group: optional.get(group, []) for group in groups}],
        sort_keys=True,
    )
    dependencies = hashlib.sha256(
        b"windows-runtime-recipe-1\0"
        + shape.encode()
        + b"\0"
        + lock.read_bytes().replace(b"\r\n", b"\n")
        + requirements.encode()
    ).hexdigest()
    assets = _git(
        checkout,
        "ls-files",
        "--stage",
        "-z",
        "--",
        "webui",
        ":(glob)resources/extensions/*/ui/**",
        ":(glob)tests/fixtures/extension-pages/*/ui/**",
    ).stdout
    return {"dependencies": dependencies, "web": hashlib.sha256(assets.encode()).hexdigest()}


def prepare_update(
    install: Installation,
    operation_id: str,
    *,
    progress: Callable[[str, str | None], None] | None = None,
) -> str:
    """Fast-forward a bound checkout and build an immutable version candidate."""

    safe_id(operation_id)

    def report(message: str, target: str | None = None) -> None:
        if progress is not None:
            progress(message, target)

    report("Checking the source branch for updates")
    binding = read_binding(install)
    if binding is None:
        raise ApplicationError("This installation has no bound source update checkout")
    checkout = Path(binding["checkout"])
    if inspect_checkout(checkout) != binding:
        raise ApplicationError("Source update checkout no longer matches its recorded binding")
    _require_clean(checkout)
    remote, branch = binding["remote"], binding["branch"]
    _git(checkout, "fetch", "--no-tags", "--", remote, f"refs/heads/{branch}")
    head = _git(checkout, "rev-parse", "HEAD").stdout.strip()
    upstream = _git(checkout, "rev-parse", "--verify", "FETCH_HEAD").stdout.strip()
    if head != upstream:
        upstream_ahead = _git(checkout, "merge-base", "--is-ancestor", head, upstream, check=False)
        local_ahead = _git(checkout, "merge-base", "--is-ancestor", upstream, head, check=False)
        if upstream_ahead.returncode == 0:
            _git(checkout, "merge", "--ff-only", "--", upstream)
        elif local_ahead.returncode != 0:
            raise ApplicationError(
                f"The source branch at {checkout} has diverged from {remote}/{branch}. "
                "Reconcile the branch while preserving local changes "
                "before running vbot update again."
            )
    _require_clean(checkout)
    revision = _git(checkout, "rev-parse", "HEAD").stdout.strip()
    try:
        project = tomllib.loads((checkout / "pyproject.toml").read_text(encoding="utf-8")).get(
            "project", {}
        )
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ApplicationError("Source pyproject.toml is invalid") from exc
    source_version = project.get("version")
    if not isinstance(source_version, str) or not source_version:
        raise ApplicationError("Source pyproject.toml has no project version")
    from cli.application.customize import (
        _build_web_assets,
        _candidate,
        _ensure_candidate_environment,
        development_state,
    )
    from cli.application.packages import validate_release, version_label
    from cli.application.payload import native_source_digest

    native_digest = native_source_digest(checkout)
    inputs = build_inputs(checkout, install.install_shape)
    base_manifest = read_json(install.version() / "release.json", limit=32 * 1024**2)
    report(
        "Checking which components need updating",
        version_label({"version": source_version, "revision": revision}),
    )
    if (
        base_manifest.get("revision") == revision
        and base_manifest.get("build_inputs") == inputs
        and base_manifest.get("native_source_digest") == native_digest
        and development_state(install) is None
    ):
        report("Verifying the installed version")
        validate_release(install.version(), shape=install.install_shape)
        _require_head(checkout, revision)
        _require_clean(checkout)
        return install.version().name
    _ensure_candidate_environment(install, checkout)
    reuse_web = base_manifest.get("build_inputs", {}).get("web") == inputs["web"]
    if install.install_shape != "desktop-client" and not reuse_web:
        report("Building the WebUI and Extension pages")
        _build_web_assets(install, checkout)
    _require_head(checkout, revision)
    _require_clean(checkout)
    rebuild_native_hosts = base_manifest.get("native_source_digest") != native_digest
    report("Preparing the application and its runtime")
    candidate = _candidate(
        install,
        checkout,
        install.version().name,
        revision,
        rebuild_native_hosts=rebuild_native_hosts,
        source_version=source_version,
        native_source_digest=native_digest,
        build_inputs=inputs,
        progress=lambda message: report(message),
    )
    _require_head(checkout, revision)
    _require_clean(checkout)
    return candidate
