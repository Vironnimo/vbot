"""Customization keeps local development work separate from released versions."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from cli.application import customize
from cli.application.state import ApplicationError, Installation


def _server_install(root: Path) -> Installation:
    install = Installation(root, "server", "127.0.0.1", 8420, str((root / "data").resolve()))
    install.save()
    (root / "active-version").write_text("rel_base\n", encoding="ascii")
    return install


def _git(directory: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments], cwd=directory, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def test_development_git_runs_windowless_and_retains_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    options: dict[str, object] = {}

    def run(arguments: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        options.update(kwargs)
        return subprocess.CompletedProcess(arguments, 0, "revision\n", "")

    monkeypatch.setattr(customize, "subprocess_creation_flags", lambda: 654)
    monkeypatch.setattr(customize.subprocess, "run", run)

    assert customize._git(tmp_path, "rev-parse", "HEAD") == "revision"
    assert options["creationflags"] == 654
    assert options["capture_output"] is True


def _repository(path: Path) -> str:
    path.mkdir()
    _git(path, "init")
    _git(path, "config", "user.name", "Test User")
    _git(path, "config", "user.email", "test@example.invalid")
    (path / "tracked.txt").write_text("base\n", encoding="utf-8")
    _git(path, "add", "tracked.txt")
    _git(path, "commit", "-m", "base")
    return _git(path, "rev-parse", "HEAD")


def test_prepare_checks_out_the_exact_release_revision_and_uses_fresh_test_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install = _server_install(tmp_path / "install")
    revision = _repository(tmp_path / "source")
    monkeypatch.setattr(
        customize,
        "validate_release",
        lambda _path, *, shape: {"revision": revision},
    )

    working = customize.prepare(install, source=tmp_path / "source")

    state = customize.development_state(install)
    assert state is not None
    assert state["base_version"] == "rel_base"
    assert state["base_revision"] == revision
    assert state["working_revision"] == revision
    assert _git(working, "rev-parse", "HEAD") == revision
    assert _git(working, "branch", "--show-current") == "vbot-local"
    test_data = install.root / "development" / "test-data"
    marker = json.loads((working / ".vbot-worktree").read_text(encoding="utf-8"))
    assert marker == {"data_dir": str(test_data)}
    assert not (test_data / "settings.json").exists()


def test_activation_archive_rejects_a_source_changed_after_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install = _server_install(tmp_path / "install")
    working = tmp_path / "working"
    revision = _repository(working)
    candidate = install.version("local_checked")
    candidate.mkdir(parents=True)
    (candidate / "release.json").write_text("{}", encoding="utf-8")
    state = {
        "schema_version": 1,
        "base_version": "rel_base",
        "base_revision": "a" * 40,
        "working_revision": revision,
        "checked_revision": revision,
        "candidate_version": "local_checked",
        "source_relative": "development/source",
        "checked_digest": customize._working_tree_digest(working),
    }
    target = install.root / "development" / "source"
    target.parent.mkdir(parents=True)
    subprocess.run(["git", "clone", "--quiet", str(working), str(target)], check=True)
    customize.save_state(install, state)
    monkeypatch.setattr(customize, "validate_release", lambda *_args, **_kwargs: {})
    (target / "tracked.txt").write_text("changed\n", encoding="utf-8")

    with pytest.raises(ApplicationError, match="Source changed after validation"):
        customize.activation_archive(install)


@pytest.mark.parametrize("mode", ["custom", "locked_changed", "locked_unchanged"])
def test_candidate_copies_source_and_resolves_dependencies_only_into_new_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    install = _server_install(tmp_path / "install")
    base = install.version()
    base_site = base / "runtime" / "Lib" / "site-packages"
    base_site.mkdir(parents=True)
    (base_site / "base_dependency.txt").write_text("base", encoding="utf-8")
    source = tmp_path / "source"
    source.mkdir()
    (source / "custom_source.txt").write_text("custom", encoding="utf-8")
    python = (
        install.root
        / "development"
        / "environment"
        / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    )
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    commands: list[list[str]] = []

    def copy_application(source_root: Path, destination: Path, _shape: str, **kwargs) -> None:
        destination.mkdir(parents=True)
        (destination / "custom_source.txt").write_text(
            (source_root / "custom_source.txt").read_text(encoding="utf-8"), encoding="utf-8"
        )

    def checked(_working: Path, arguments: list[str], _log: Path) -> None:
        commands.append(arguments)
        target = Path(arguments[arguments.index("--target") + 1])
        target.mkdir(parents=True)
        (target / "fresh_dependency.txt").write_text("fresh", encoding="utf-8")

    monkeypatch.setattr("cli.application.payload.copy_application", copy_application)
    monkeypatch.setattr(customize, "_checked_command", checked)
    monkeypatch.setattr(
        customize,
        "validate_release",
        lambda path, *, shape: {
            "schema_version": 1,
            "bootstrap_protocol": 1,
            "version_id": path.name,
            "install_shape": shape,
            "platform": "windows-x86_64",
            "build_inputs": {"dependencies": "old", "web": "web"},
            "files": {"runtime/placeholder": "0" * 64},
        },
    )

    inputs = (
        None
        if mode == "custom"
        else {"dependencies": "old" if mode == "locked_unchanged" else "new", "web": "web"}
    )
    candidate_id = customize._candidate(install, source, "rel_base", "c" * 40, build_inputs=inputs)
    candidate = install.version(candidate_id)

    assert (candidate / "app" / "custom_source.txt").read_text(encoding="utf-8") == "custom"
    candidate_site = candidate / "runtime" / "Lib" / "site-packages"
    assert (candidate_site / "fresh_dependency.txt").is_file() is (mode != "locked_unchanged")
    assert (base_site / "base_dependency.txt").is_file()
    assert not (base_site / "fresh_dependency.txt").exists()
    if mode == "locked_unchanged":
        assert not commands
        (candidate_site / "base_dependency.txt").write_text("candidate change", encoding="utf-8")
        assert (base_site / "base_dependency.txt").read_text(encoding="utf-8") == "base"
    else:
        assert commands
        assert Path(commands[0][commands[0].index("--target") + 1]).is_relative_to(candidate)
        assert not (candidate_site / "base_dependency.txt").exists()
        if mode == "locked_changed":
            assert "--require-hashes" in commands[0]
            assert Path(commands[0][-1]).name == "requirements-server.lock"
    manifest = json.loads((candidate / "release.json").read_text(encoding="utf-8"))
    assert manifest.get("build_inputs") == inputs


def test_checked_command_runs_windowless_and_retains_failure_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    options: dict[str, object] = {}

    def run(_arguments: list[str], **kwargs: object) -> SimpleNamespace:
        options.update(kwargs)
        output = kwargs["stdout"]
        assert hasattr(output, "write")
        output.write(b"preserved failure\n")
        return SimpleNamespace(returncode=7)

    monkeypatch.setattr(customize, "subprocess_creation_flags", lambda: 321)
    monkeypatch.setattr(customize.subprocess, "run", run)
    log = tmp_path / "validation.log"

    with pytest.raises(ApplicationError) as failure:
        customize._checked_command(tmp_path, ["failing-command"], log)

    assert options["creationflags"] == 321
    assert options["stderr"] is subprocess.STDOUT
    assert str(log) in str(failure.value)
    assert log.read_text(encoding="utf-8") == "preserved failure\n"


def test_pending_rebase_promotes_only_after_its_exact_candidate_is_active(tmp_path: Path) -> None:
    install = _server_install(tmp_path / "install")
    state = {
        "schema_version": 1,
        "base_version": "rel_base",
        "base_revision": "a" * 40,
        "working_revision": "b" * 40,
        "candidate_version": "local_old",
        "pending_rebase": {
            "source_relative": "development/rebase-rel_new",
            "base_version": "rel_new",
            "base_revision": "c" * 40,
            "working_revision": "d" * 40,
            "checked_revision": "d" * 40,
            "checked_digest": "digest",
            "candidate_version": "local_new",
            "intent": "Keep local change",
        },
    }
    customize.save_state(install, state)

    customize.finalize_activation(install, "local_new")
    retained = customize.development_state(install)
    assert retained is not None and retained["pending_rebase"]["candidate_version"] == "local_new"
    assert "active_local_version" not in retained

    (install.root / "active-version").write_text("local_new\n", encoding="ascii")
    customize.finalize_activation(install, "local_new")
    promoted = customize.development_state(install)
    assert promoted is not None
    assert promoted["base_version"] == "rel_new"
    assert promoted["active_local_version"] == "local_new"
    assert "pending_rebase" not in promoted


def test_pending_checked_rebase_is_activatable_without_an_initial_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install = _server_install(tmp_path / "install")
    source = install.root / "development" / "rebase-rel_new"
    source.parent.mkdir(parents=True)
    revision = _repository(source)
    candidate = install.version("local_rebased")
    candidate.mkdir(parents=True)
    (candidate / "release.json").write_text("{}", encoding="utf-8")
    (candidate / "payload.txt").write_text("rebased", encoding="utf-8")
    customize.save_state(
        install,
        {
            "schema_version": 1,
            "base_version": "rel_base",
            "base_revision": "a" * 40,
            "working_revision": "b" * 40,
            "candidate_version": None,
            "pending_rebase": {
                "source_relative": "development/rebase-rel_new",
                "base_version": "rel_new",
                "base_revision": "c" * 40,
                "working_revision": revision,
                "checked_revision": revision,
                "checked_digest": customize._working_tree_digest(source),
                "candidate_version": "local_rebased",
            },
        },
    )
    monkeypatch.setattr(customize, "validate_release", lambda *_args, **_kwargs: {})

    archive = customize.activation_archive(install)

    assert archive.name == "local_rebased.zip"


def test_test_instance_prefers_the_checked_pending_rebase_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install = _server_install(tmp_path / "install")
    pending_source = install.root / "development" / "rebase-rel_new"
    pending_source.mkdir(parents=True)
    for candidate in ("local_old", "local_rebased"):
        version = install.version(candidate)
        version.mkdir(parents=True)
        (version / "release.json").write_text("{}", encoding="utf-8")
    customize.save_state(
        install,
        {
            "schema_version": 1,
            "base_version": "rel_base",
            "base_revision": "a" * 40,
            "working_revision": "b" * 40,
            "checked_revision": "b" * 40,
            "checked_digest": "old",
            "candidate_version": "local_old",
            "pending_rebase": {
                "source_relative": "development/rebase-rel_new",
                "base_version": "rel_new",
                "base_revision": "c" * 40,
                "working_revision": "d" * 40,
                "checked_revision": "d" * 40,
                "checked_digest": "pending",
                "candidate_version": "local_rebased",
            },
        },
    )
    selected: list[str] = []
    monkeypatch.setattr(customize, "_working_tree_digest", lambda path: "pending")
    monkeypatch.setattr(customize, "_git", lambda *_args: "d" * 40)
    monkeypatch.setattr(customize, "validate_release", lambda *_args, **_kwargs: {})

    def interpreter(_install, version_id=None, role="Python"):
        selected.append(version_id)
        return Path("test-server.exe")

    monkeypatch.setattr(Installation, "interpreter", interpreter)
    monkeypatch.setattr(
        customize.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(returncode=0)
    )

    customize.run_test_instance(install)

    assert selected == ["local_rebased"]


def test_rebase_conflict_keeps_source_and_records_reconciliation_location(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install = _server_install(tmp_path / "install")
    source = install.root / "development" / "source"
    source.mkdir(parents=True)
    state = {
        "schema_version": 1,
        "base_version": "rel_base",
        "base_revision": "a" * 40,
        "working_revision": "b" * 40,
        "candidate_version": "local_old",
        "intent": "Keep local change",
    }
    customize.save_state(install, state)
    calls: list[tuple[Path, tuple[str, ...]]] = []

    def fake_git(directory: Path, *arguments: str) -> str:
        calls.append((directory, arguments))
        if arguments[:2] == ("rev-parse", "HEAD"):
            return "b" * 40
        if arguments[:2] == ("worktree", "add"):
            directory = Path(arguments[-2])
            directory.mkdir(parents=True)
            return ""
        if arguments and arguments[0] == "rebase":
            raise ApplicationError("conflict")
        return ""

    monkeypatch.setattr(customize, "_git", fake_git)
    monkeypatch.setattr(
        customize, "validate_release", lambda *_args, **_kwargs: {"revision": "c" * 40}
    )

    with pytest.raises(ApplicationError, match="Previous version remains active; resolve changes"):
        customize.carry_forward(install, "rel_new")

    saved = customize.development_state(install)
    assert saved is not None
    assert saved["base_version"] == "rel_base"
    assert saved["working_revision"] == "b" * 40
    pending = saved["pending_rebase"]
    assert pending["source_relative"] == "development/rebase-rel_new"
    assert pending["base_version"] == "rel_new"
    assert pending["base_revision"] == "c" * 40
    assert any(arguments and arguments[0] == "rebase" for _, arguments in calls)


def test_update_rejects_clean_user_commit_created_after_last_check(tmp_path: Path) -> None:
    install = _server_install(tmp_path / "install")
    source = install.root / "development" / "source"
    source.parent.mkdir(parents=True)
    checked = _repository(source)
    customize.save_state(
        install,
        {
            "schema_version": 1,
            "base_version": "rel_base",
            "base_revision": checked,
            "working_revision": checked,
            "checked_revision": checked,
            "candidate_version": "local_checked",
        },
    )
    (source / "tracked.txt").write_text("new committed behavior\n", encoding="utf-8")
    _git(source, "add", "tracked.txt")
    _git(source, "commit", "-m", "manual user commit")
    assert not _git(source, "status", "--porcelain")

    with pytest.raises(ApplicationError, match="commits changed after the last check"):
        customize.carry_forward(install, "rel_new")


def test_test_instance_uses_fresh_data_explicit_flag_and_selected_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install = _server_install(tmp_path / "install")
    source = install.root / "development" / "source"
    source.mkdir(parents=True)
    state = {
        "schema_version": 1,
        "base_version": "rel_base",
        "base_revision": "a" * 40,
        "working_revision": "b" * 40,
        "checked_revision": "b" * 40,
        "candidate_version": "local_checked",
        "checked_digest": "stable",
    }
    customize.save_state(install, state)
    launched: list[tuple[list[str], dict[str, object]]] = []
    monkeypatch.setattr(customize, "_working_tree_digest", lambda _path: "stable")
    monkeypatch.setattr(customize, "_git", lambda *_args: "b" * 40)
    monkeypatch.setattr(customize, "validate_release", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        Installation, "interpreter", lambda *_args, **_kwargs: Path("test-server.exe")
    )
    monkeypatch.setenv("PATH", "test-path")
    monkeypatch.setenv("TEMP", "test-temp")
    monkeypatch.setenv("OPENAI_API_KEY", "not-for-test-instance")
    monkeypatch.setenv("CUSTOM_SECRET", "not-for-test-instance")
    monkeypatch.setenv("VBOT_RUN_TOKEN", "not-for-test-instance")

    def fake_run(arguments: list[str], **kwargs: object) -> SimpleNamespace:
        launched.append((arguments, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(customize.subprocess, "run", fake_run)

    customize.run_test_instance(install, port=19001)
    customize.run_test_instance(install, port=19002)

    assert len(launched) == 2
    data_paths = []
    for (arguments, _options), expected_port in zip(launched, ("19001", "19002"), strict=True):
        assert arguments[arguments.index("--port") + 1] == expected_port
        assert "--test-instance" in arguments
        data_paths.append(Path(arguments[arguments.index("--data-dir") + 1]))
    assert data_paths[0] != data_paths[1]
    assert all(path.is_dir() for path in data_paths)
    assert all(
        path.is_relative_to(install.root / "development" / "test-instances") for path in data_paths
    )
    environment_value = launched[-1][1]["env"]
    assert isinstance(environment_value, dict)
    environment = {key.upper(): value for key, value in environment_value.items()}
    assert environment["PATH"] == "test-path"
    assert environment["TEMP"] == "test-temp"
    assert environment["VBOT_INSTALL_ROOT"] == str(install.root)
    assert environment["PYTHONDONTWRITEBYTECODE"] == "1"
    assert environment["PYTHONNOUSERSITE"] == "1"
    assert "OPENAI_API_KEY" not in environment
    assert "CUSTOM_SECRET" not in environment
    assert "VBOT_RUN_TOKEN" not in environment


def test_desktop_client_rejects_server_test_instance(tmp_path: Path) -> None:
    install = Installation(tmp_path, "desktop-client", None, None, None)

    with pytest.raises(ApplicationError, match="have no local server"):
        customize.run_test_instance(install)
