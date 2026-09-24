"""Version-matched development copies and verified local application variants."""

from __future__ import annotations

import hashlib
import os
import shutil
import socket
import subprocess
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from cli.application.packages import digest, validate_release
from cli.application.runtime_sqlite import RuntimeSQLiteError, provision_runtime_sqlite
from cli.application.state import (
    ApplicationError,
    Installation,
    contained,
    exclusive,
    read_json,
    safe_id,
    write_json,
)
from core.utils.ids import new_id
from core.utils.processes import subprocess_creation_flags

_TEST_ENVIRONMENT_KEYS = frozenset(
    {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
        "LOCALAPPDATA",
        "APPDATA",
        "HOME",
        "LANG",
    }
)


def _git(directory: Path, *args: str) -> str:
    environment = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    result = subprocess.run(
        ["git", *args],
        cwd=directory,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        creationflags=subprocess_creation_flags(),
    )
    if result.returncode:
        raise ApplicationError(
            f"Development Git operation failed ({args[0]}): {result.stderr.strip()}"
        )
    return result.stdout.strip()


def development_state(install: Installation) -> dict[str, Any] | None:
    path = contained(install.root, "development/state.json")
    if not path.exists():
        return None
    value = read_json(path)
    if value.get("schema_version") != 1:
        raise ApplicationError("Unsupported local customization state")
    for field in ("base_version",):
        safe_id(value.get(field))
    for field in ("base_revision", "working_revision"):
        raw = value.get(field)
        if (
            not isinstance(raw, str)
            or len(raw) != 40
            or any(c not in "0123456789abcdef" for c in raw)
        ):
            raise ApplicationError("Invalid customization source revision")
    return value


def save_state(install: Installation, value: dict[str, Any]) -> None:
    write_json(contained(install.root, "development/state.json"), value)


def _checked_candidate_state(state: dict[str, Any] | None) -> dict[str, Any]:
    if state is not None:
        pending = state.get("pending_rebase")
        if isinstance(pending, dict) and pending.get("candidate_version"):
            return pending
        if state.get("candidate_version"):
            return state
    raise ApplicationError("Check the customization before activating it")


def _source(install: Installation, state: dict[str, Any]) -> Path:
    relative = state.get("source_relative", "development/source")
    if not isinstance(relative, str) or not relative.startswith("development/"):
        raise ApplicationError("Invalid customization source location")
    return contained(install.root, relative)


def prepare(install: Installation, *, source: Path | None = None) -> Path:
    with exclusive(install.root):
        existing = development_state(install)
        working = contained(install.root, "development/source")
        if existing is not None:
            return _source(install, existing)
        if shutil.which("git") is None:
            raise ApplicationError(
                "Local customization requires Git. Install Git, then repeat vbot customize prepare"
            )
        manifest = validate_release(
            install.version(), shape=install.install_shape, remove_bytecode_caches=True
        )
        revision = manifest.get("revision")
        if (
            not isinstance(revision, str)
            or len(revision) != 40
            or any(c not in "0123456789abcdef" for c in revision)
        ):
            raise ApplicationError(
                "The installed release does not identify an exact development revision"
            )
        working.parent.mkdir(parents=True, exist_ok=True)
        if working.exists():
            raise ApplicationError(
                "An unfinished development copy exists; inspect it before preparing another"
            )
        origin = (
            str(source.expanduser().resolve())
            if source
            else "https://github.com/Vironnimo/vbot.git"
        )
        _git(working.parent, "clone", "--no-checkout", "--", origin, str(working))
        _git(working, "checkout", "--detach", revision)
        _git(working, "switch", "-c", "vbot-local")
        save_state(
            install,
            {
                "schema_version": 1,
                "base_version": install.version().name,
                "base_revision": revision,
                "working_revision": revision,
                "checked_revision": None,
                "candidate_version": None,
                "intent": "",
            },
        )
        # Always fresh, never a copy of production credentials/Session data.
        test_data = contained(install.root, "development/test-data")
        test_data.mkdir(parents=True, exist_ok=True)
        write_json(working / ".vbot-worktree", {"data_dir": str(test_data)})
        return working


def _checked_command(
    working: Path,
    args: list[str],
    log: Path,
    *,
    environment_override: dict[str, str] | None = None,
) -> None:
    environment = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("VBOT_RUN_")
        and k
        not in {
            "VBOT_INSTALL_ROOT",
            "VBOT_DATA_DIR",
            "VBOT_UPDATE_HANDOFF",
            "PYTHONPATH",
            "PYTHONHOME",
        }
    }
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    if environment_override is not None:
        environment = environment_override
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("ab") as output:
        result = subprocess.run(
            args,
            cwd=working,
            env=environment,
            stdout=output,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            timeout=3600,
            creationflags=subprocess_creation_flags(),
        )
    if result.returncode:
        raise ApplicationError(f"Application preparation failed. Details: {log}")


def _ensure_candidate_environment(install: Installation, source: Path) -> Path:
    """Create the private build environment without validating or changing source."""

    dev = contained(install.root, "development/environment")
    python = dev / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.is_file():
        from cli.application.dependencies import environment_creation_command

        command, environment = environment_creation_command(install, dev)
        _checked_command(
            source,
            command,
            contained(install.root, "development/source-update.log"),
            environment_override=environment,
        )
    return python


def _build_web_assets(install: Installation, source: Path) -> None:
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    if npm is None:
        raise ApplicationError("Source updates require Node.js/npm to build the WebUI")
    log = contained(install.root, "development/source-update.log")
    _checked_command(source / "webui", [npm, "ci"], log)
    _checked_command(source / "webui", [npm, "run", "build"], log)


def _working_tree_digest(working: Path) -> str:
    result = hashlib.sha256()
    names = _git(working, "ls-files", "--cached", "--others", "--exclude-standard", "-z").split(
        "\0"
    )
    for name in sorted(set(filter(None, names))):
        path = contained(working, name)
        result.update(name.encode("utf-8"))
        result.update(bytes.fromhex(digest(path)) if path.is_file() else b"deleted")
    return result.hexdigest()


def _require_revision(working: Path, expected: object, message: str) -> None:
    if not isinstance(expected, str) or _git(working, "rev-parse", "HEAD") != expected:
        raise ApplicationError(message)


def _validate(install: Installation, working: Path, *, intent: str) -> str:
    if not intent.strip():
        raise ApplicationError(
            "Describe the intended behavior with --intent before checking a customization"
        )
    # Separate dev environment: release interpreter/dependencies are never mutated.
    dev = contained(install.root, "development/environment")
    python = dev / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    log = contained(install.root, "development/check.log")
    if not python.is_file():
        from cli.application.dependencies import environment_creation_command

        command, environment = environment_creation_command(install, dev)
        _checked_command(working, command, log, environment_override=environment)
    _checked_command(
        working, [str(python), "-m", "pip", "install", "-e", ".[dev,cli,windows-app]"], log
    )
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    if npm is None:
        raise ApplicationError("Customization validation requires Node.js/npm to build the WebUI")
    _checked_command(working / "webui", [npm, "ci"], log)
    _checked_command(working, [str(python), "scripts/quality.py"], log)
    _checked_command(working, [str(python), "scripts/quality-frontend.py", "--build"], log)
    _git(working, "add", "--all")
    if _git(working, "diff", "--cached", "--name-only"):
        _git(
            working,
            "-c",
            "user.name=vBot Local",
            "-c",
            "user.email=local@vbot.invalid",
            "commit",
            "-m",
            intent.strip(),
        )
    return _git(working, "rev-parse", "HEAD")


def _candidate(
    install: Installation,
    source: Path,
    base_version: str,
    revision: str,
    *,
    rebuild_native_hosts: bool = False,
    source_version: str | None = None,
    native_source_digest: str | None = None,
    build_inputs: dict[str, str] | None = None,
    progress: Callable[[str], None] | None = None,
) -> str:
    """Build application sources over a fresh copy of the exact selected runtime."""
    from cli.application.payload import copy_application

    base = install.version(base_version)
    old = validate_release(base, shape=install.install_shape, remove_bytecode_caches=True)
    candidate_id = new_id("local")
    candidate = install.version(candidate_id)
    candidate.mkdir(parents=True)
    try:
        previous_inputs = old.get("build_inputs", {})
        reuse_runtime = (
            build_inputs is not None
            and previous_inputs.get("dependencies") == build_inputs["dependencies"]
        )
        if progress:
            progress(
                "Copying the verified runtime"
                if reuse_runtime
                else "Installing the application dependencies"
            )
        shutil.copytree(
            base / "runtime",
            candidate / "runtime",
            ignore=None if reuse_runtime else shutil.ignore_patterns("site-packages"),
        )
        try:
            provision_runtime_sqlite(candidate / "runtime", source)
        except (OSError, RuntimeSQLiteError) as error:
            raise ApplicationError(
                f"The runtime SQLite library could not be prepared: {error}"
            ) from error
        if build_inputs is not None and previous_inputs.get("web") == build_inputs["web"]:
            copy_application(source, candidate / "app", install.install_shape, assets=base / "app")
        else:
            copy_application(source, candidate / "app", install.install_shape)
        # Resolve this source's requirements into the candidate, never the base.
        site = candidate / "runtime" / "Lib" / "site-packages"
        python = contained(install.root, "development/environment") / (
            "Scripts/python.exe" if os.name == "nt" else "bin/python"
        )
        extras = {
            "server": "server,windows-app",
            "server-desktop": "server,windows-app,desktop",
            "desktop-client": "cli,windows-app,desktop",
        }[install.install_shape]
        requirements = (
            [
                "--require-hashes",
                "--only-binary=:all:",
                "--no-binary=proxy-tools",
                "-r",
                str(source / "scripts" / "windows" / f"requirements-{install.install_shape}.lock"),
            ]
            if build_inputs is not None
            else [f"{source}[{extras}]"]
        )
        if not reuse_runtime:
            _checked_command(
                source,
                [
                    str(python),
                    "-m",
                    "pip",
                    "install",
                    "--no-input",
                    "--no-compile",
                    "--target",
                    str(site),
                    *requirements,
                ],
                contained(install.root, "development/check.log"),
            )
        for package in ("core", "server", "cli", "desktop"):
            if (site / package).exists():
                shutil.rmtree(site / package)
        for metadata in site.glob("vbot-*.dist-info"):
            shutil.rmtree(metadata)
        from importlib.metadata import distributions

        inventory = sorted(
            (
                {"name": dist.metadata["Name"], "version": dist.version}
                for dist in distributions(path=[str(site)])
            ),
            key=lambda item: item["name"].lower(),
        )
        write_json(
            candidate / "runtime" / "vbot-runtime-inventory.json",
            {"schema_version": 1, "packages": inventory},
        )
        if rebuild_native_hosts:
            if progress:
                progress("Building the Windows launchers")
            if source_version is None:
                raise ApplicationError("Native source candidates require a source version")
            native_script = (
                "import sys\n"
                "from pathlib import Path\n"
                "from scripts.build_windows import HOSTS, compile_host\n"
                "source, runtime, version = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]\n"
                "for filename, role in HOSTS.items():\n"
                "    compile_host(source, runtime / filename, role=role, version=version, "
                "stable=role in {'host', 'gui'})\n"
            )
            _checked_command(
                source,
                [
                    str(python),
                    "-c",
                    native_script,
                    str(source),
                    str(candidate / "runtime"),
                    source_version,
                ],
                contained(install.root, "development/source-update.log"),
            )
        if progress:
            progress("Verifying the prepared application")
        manifest = {
            key: value for key, value in old.items() if key not in {"files", "build_inputs"}
        }
        manifest.update(version_id=candidate_id, revision=revision, official_base=base_version)
        if build_inputs is not None:
            manifest["build_inputs"] = build_inputs
        if source_version is not None:
            manifest["version"] = source_version
        if native_source_digest is not None:
            if len(native_source_digest) != 64 or any(
                character not in "0123456789abcdef" for character in native_source_digest
            ):
                raise ApplicationError("Invalid native source digest")
            manifest["native_source_digest"] = native_source_digest
        manifest["files"] = {
            path.relative_to(candidate).as_posix(): digest(path)
            for path in candidate.rglob("*")
            if path.is_file()
        }
        write_json(candidate / "release.json", manifest)
        validate_release(candidate, shape=install.install_shape)
    except Exception:
        contained(install.root, f"versions/{candidate_id}")
        shutil.rmtree(candidate)
        raise
    return candidate_id


def check(install: Installation, *, intent: str) -> dict[str, Any]:
    with exclusive(install.root):
        state = development_state(install)
        if state is None:
            raise ApplicationError("Run vbot customize prepare first")
        working = _source(install, state)
        revision = _validate(install, working, intent=intent)
        candidate = _candidate(install, working, state["base_version"], revision)
        state.update(
            working_revision=revision,
            checked_revision=revision,
            candidate_version=candidate,
            intent=intent,
            checked_digest=_working_tree_digest(working),
        )
        save_state(install, state)
        return state


def activation_archive(install: Installation) -> Path:
    with exclusive(install.root):
        state = _checked_candidate_state(development_state(install))
        working = _source(install, state)
        _require_revision(
            working,
            state.get("checked_revision"),
            "Source revision changed after validation. Run vbot customize check again",
        )
        if state.get("checked_digest") != _working_tree_digest(working):
            raise ApplicationError(
                "Source changed after validation. Run vbot customize check again"
            )
        candidate = install.version(state["candidate_version"])
        validate_release(candidate, shape=install.install_shape, remove_bytecode_caches=True)
        archive = contained(install.root, f"development/{candidate.name}.zip")
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            for path in candidate.rglob("*"):
                if path.is_file():
                    bundle.write(path, path.relative_to(candidate).as_posix())
        return archive


def carry_forward(install: Installation, candidate_version: str) -> str:
    state = development_state(install)
    if not state or state.get("candidate_version") == candidate_version:
        return candidate_version
    pending = state.get("pending_rebase")
    if pending and pending.get("candidate_version") == candidate_version:
        return candidate_version
    working = _source(install, state)
    if _git(working, "status", "--porcelain"):
        raise ApplicationError(
            "Uncommitted local customization changes exist; check them before updating"
        )
    _require_revision(
        working,
        state.get("working_revision"),
        "Local customization commits changed after the last check; "
        "run vbot customize check before updating",
    )
    release = validate_release(
        install.version(candidate_version), shape=install.install_shape, remove_bytecode_caches=True
    )
    revision = release.get("revision")
    if not isinstance(revision, str) or len(revision) != 40:
        raise ApplicationError("New release has no exact source revision for local changes")
    _git(working, "fetch", "origin", revision)
    rebased = contained(install.root, f"development/rebase-{candidate_version}")
    if rebased.exists():
        raise ApplicationError(f"A prior update reconciliation needs inspection at {rebased}")
    _git(working, "worktree", "add", "--detach", str(rebased), state["working_revision"])
    state["pending_rebase"] = {
        "source_relative": rebased.relative_to(install.root).as_posix(),
        "base_version": candidate_version,
        "base_revision": revision,
        "working_revision": state["working_revision"],
        "candidate_version": None,
        "intent": state.get("intent") or "Preserve local application behavior",
    }
    save_state(install, state)
    try:
        _git(rebased, "rebase", "--onto", revision, state["base_revision"])
    except ApplicationError as exc:
        raise ApplicationError(
            "Local changes conflict with the update. Previous version remains active; "
            f"resolve changes in {rebased}, then run vbot customize rebase --intent <description>"
        ) from exc
    checked = _validate(
        install, rebased, intent=state.get("intent") or "Preserve local application behavior"
    )
    new_candidate = _candidate(install, rebased, candidate_version, checked)
    # Do not repoint the user's source before successful activation. Save a staged
    # reconciliation whose worktree and base remain available after any failure.
    state["pending_rebase"].update(
        working_revision=checked,
        checked_revision=checked,
        candidate_version=new_candidate,
        checked_digest=_working_tree_digest(rebased),
    )
    save_state(install, state)
    return new_candidate


def finish_rebase(install: Installation, *, intent: str) -> dict[str, Any]:
    """Validate a manually reconciled update without choosing conflict resolutions."""
    with exclusive(install.root):
        state = development_state(install)
        if not state or not state.get("pending_rebase"):
            raise ApplicationError("No update reconciliation is pending")
        pending = state["pending_rebase"]
        working = _source(install, pending)
        if _git(working, "diff", "--name-only", "--diff-filter=U"):
            raise ApplicationError(f"Resolve and stage the remaining conflicts in {working} first")
        for marker in ("rebase-merge", "rebase-apply"):
            location = Path(_git(working, "rev-parse", "--git-path", marker))
            if not location.is_absolute():
                location = working / location
            if location.exists():
                _git(working, "-c", "core.editor=true", "rebase", "--continue")
                break
        revision = _validate(install, working, intent=intent)
        candidate = _candidate(install, working, pending["base_version"], revision)
        pending.update(
            working_revision=revision,
            checked_revision=revision,
            candidate_version=candidate,
            checked_digest=_working_tree_digest(working),
            intent=intent,
        )
        save_state(install, state)
        return dict(pending)


def finalize_activation(install: Installation, candidate_version: str) -> None:
    """Promote reconciliation metadata only after the exact candidate is active.

    Called while the operation worker owns the installation lock. Older source
    worktrees are retained, so neither failure nor activation destroys local work.
    """
    state = development_state(install)
    if not state or install.version().name != candidate_version:
        return
    pending = state.get("pending_rebase")
    if pending and pending.get("candidate_version") == candidate_version:
        state.update(pending)
        state.pop("pending_rebase", None)
    state["active_local_version"] = candidate_version
    save_state(install, state)


def _test_environment(install: Installation) -> dict[str, str]:
    """Build the isolated environment for a fresh local test instance."""

    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in _TEST_ENVIRONMENT_KEYS or key.startswith("LC_")
    }
    environment.update(
        VBOT_INSTALL_ROOT=str(install.root),
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONNOUSERSITE="1",
    )
    return environment


def run_test_instance(install: Installation, *, port: int = 0) -> None:
    if not install.owns_server:
        raise ApplicationError("Desktop Client customizations have no local server to test")
    try:
        state = _checked_candidate_state(development_state(install))
    except ApplicationError as exc:
        raise ApplicationError("Run vbot customize check before testing its candidate") from exc
    if state.get("checked_digest") != _working_tree_digest(_source(install, state)):
        raise ApplicationError("Source changed after validation. Run vbot customize check again")
    _require_revision(
        _source(install, state),
        state.get("checked_revision"),
        "Source revision changed after validation. Run vbot customize check again",
    )
    candidate = safe_id(state["candidate_version"])
    validate_release(
        install.version(candidate), shape=install.install_shape, remove_bytecode_caches=True
    )
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", port))
        selected_port = reservation.getsockname()[1]
    data = contained(install.root, f"development/test-instances/{new_id('test')}")
    # The test server initializes this fresh data directory itself. Creating the
    # root here would leave it without the Session store's bootstrap marker, and
    # the server refuses an existing root without that marker.
    data.parent.mkdir(parents=True, exist_ok=True)
    print(f"Test instance: http://127.0.0.1:{selected_port}; fresh data: {data}", flush=True)
    result = subprocess.run(
        [
            str(install.interpreter(candidate, "Server")),
            "-m",
            "server.main",
            "--host",
            "127.0.0.1",
            "--port",
            str(selected_port),
            "--data-dir",
            str(data),
            "--test-instance",
        ],
        cwd=install.version(candidate) / "app",
        env=_test_environment(install),
        check=False,
    )
    if result.returncode:
        raise ApplicationError(f"Test instance exited with code {result.returncode}; logs: {data}")
