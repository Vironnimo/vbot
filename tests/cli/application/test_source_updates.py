"""Bound source updates preserve Git work and the active native payload."""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

import pytest

from cli.application import source_updates
from cli.application.state import ApplicationError, Installation


def _git(directory: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=directory,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip()


@pytest.fixture
def tracked_checkout(tmp_path: Path) -> tuple[Path, Path]:
    remote = tmp_path / "remote.git"
    checkout = tmp_path / "checkout"
    publisher = tmp_path / "publisher"
    remote.mkdir()
    _git(remote, "init", "--bare", "--initial-branch=main")
    checkout.mkdir()
    _git(checkout, "init", "--initial-branch=main")
    _git(checkout, "config", "user.name", "Source Test")
    _git(checkout, "config", "user.email", "source@example.invalid")
    (checkout / "tracked.txt").write_text("one\n", encoding="utf-8")
    (checkout / "pyproject.toml").write_text(
        '[project]\nname = "fixture"\nversion = "1.2.3"\n', encoding="utf-8"
    )
    locks = checkout / "scripts" / "windows"
    locks.mkdir(parents=True)
    for shape in ("server", "desktop-client"):
        (locks / f"requirements-{shape}.lock").write_text("# test-owned lock\n", encoding="utf-8")
    _git(checkout, "add", ".")
    _git(checkout, "commit", "-m", "initial")
    _git(checkout, "remote", "add", "origin", str(remote))
    _git(checkout, "push", "-u", "origin", "main")
    _git(tmp_path, "clone", str(remote), str(publisher))
    _git(publisher, "config", "user.name", "Publisher")
    _git(publisher, "config", "user.email", "publisher@example.invalid")
    return checkout, publisher


def _install(root: Path, *, shape: str = "server") -> Installation:
    install = Installation(
        root,
        shape,
        None if shape == "desktop-client" else "127.0.0.1",
        None if shape == "desktop-client" else 8420,
        None if shape == "desktop-client" else str(root / "data"),
    )
    install.save()
    (root / "versions" / "rel_old").mkdir(parents=True)
    (root / "versions" / "rel_old" / "release.json").write_text("{}", encoding="utf-8")
    (root / "active-version").write_text("rel_old\n", encoding="ascii")
    return install


def test_inspect_and_bind_record_exact_tracking_branch(
    tmp_path: Path,
    tracked_checkout: tuple[Path, Path],
    caplog: pytest.LogCaptureFixture,
):
    checkout, _publisher = tracked_checkout
    install = _install(tmp_path / "install")

    with caplog.at_level(logging.INFO, logger="vbot.application.source_updates"):
        inspected = source_updates.inspect_checkout(checkout)
        bound = source_updates.bind_checkout(install, checkout)

    assert (
        inspected
        == bound
        == {
            "schema_version": 1,
            "checkout": str(checkout.resolve()),
            "remote": "origin",
            "branch": "main",
        }
    )
    assert json.loads((install.root / "source-update.json").read_text(encoding="utf-8")) == bound
    change = next(record for record in caplog.records if record.msg.endswith("selection changed"))
    assert change.__dict__["operation"] == "source.select"
    assert change.__dict__["source_track"] == "main"
    assert change.__dict__["checkout"] == str(checkout.resolve())
    assert change.__dict__["branch"] == "main"
    assert not hasattr(change, "remote")


def test_prepare_refuses_dirty_checkout_before_fetch_or_build(
    tmp_path: Path,
    tracked_checkout: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
):
    checkout, _publisher = tracked_checkout
    install = _install(tmp_path / "install")
    source_updates.bind_checkout(install, checkout)
    (checkout / "local.txt").write_text("preserve me\n", encoding="utf-8")
    monkeypatch.setattr(
        "cli.application.customize._candidate",
        lambda *_args, **_kwargs: pytest.fail("must not build"),
    )

    with pytest.raises(ApplicationError):
        source_updates.prepare_update(install, "upd_dirty")

    assert (checkout / "local.txt").read_text(encoding="utf-8") == "preserve me\n"
    assert install.version().name == "rel_old"


def test_prepare_fast_forwards_recorded_branch_and_builds_exact_revision(
    tmp_path: Path,
    tracked_checkout: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
):
    checkout, publisher = tracked_checkout
    install = _install(tmp_path / "install")
    source_updates.bind_checkout(install, checkout)
    (publisher / "tracked.txt").write_text("two\n", encoding="utf-8")
    _git(publisher, "add", "tracked.txt")
    _git(publisher, "commit", "-m", "update")
    _git(publisher, "push", "origin", "main")
    expected_revision = _git(publisher, "rev-parse", "HEAD")
    captured: dict[str, object] = {}
    monkeypatch.setattr("cli.application.customize._ensure_candidate_environment", lambda *_: None)
    monkeypatch.setattr("cli.application.customize._build_web_assets", lambda *_: None)
    monkeypatch.setattr("cli.application.payload.native_source_digest", lambda _source: "d" * 64)

    def candidate(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return "local_candidate"

    monkeypatch.setattr("cli.application.customize._candidate", candidate)

    assert source_updates.prepare_update(install, "upd_source") == "local_candidate"
    assert _git(checkout, "rev-parse", "HEAD") == expected_revision
    assert _git(checkout, "symbolic-ref", "--short", "HEAD") == "main"
    assert captured["args"][3] == expected_revision  # type: ignore[index]
    options = captured["kwargs"]
    assert isinstance(options, dict)
    assert options["rebuild_native_hosts"] is True
    assert options["source_version"] == "1.2.3"
    assert options["native_source_digest"] == "d" * 64
    assert options["build_inputs"] == source_updates.build_inputs(checkout, "server")
    assert install.version().name == "rel_old"


def test_candidate_failure_keeps_active_payload_and_clean_tracking_state(
    tmp_path: Path,
    tracked_checkout: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
):
    checkout, _publisher = tracked_checkout
    install = _install(tmp_path / "install")
    source_updates.bind_checkout(install, checkout)
    original_branch = _git(checkout, "symbolic-ref", "--short", "HEAD")
    monkeypatch.setattr("cli.application.customize._ensure_candidate_environment", lambda *_: None)
    monkeypatch.setattr("cli.application.customize._build_web_assets", lambda *_: None)
    monkeypatch.setattr("cli.application.payload.native_source_digest", lambda _source: "d" * 64)
    monkeypatch.setattr(
        "cli.application.customize._candidate",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ApplicationError("candidate failed")),
    )

    with pytest.raises(ApplicationError, match="candidate failed"):
        source_updates.prepare_update(install, "upd_failed")

    assert install.version().name == "rel_old"
    assert _git(checkout, "symbolic-ref", "--short", "HEAD") == original_branch
    assert _git(checkout, "status", "--porcelain") == ""


def test_clean_local_commits_are_preserved_and_built(
    tmp_path: Path,
    tracked_checkout: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
):
    checkout, _publisher = tracked_checkout
    install = _install(tmp_path / "install")
    source_updates.bind_checkout(install, checkout)
    (checkout / "local-feature.txt").write_text("local commit\n", encoding="utf-8")
    _git(checkout, "add", "local-feature.txt")
    _git(checkout, "commit", "-m", "local feature")
    local_revision = _git(checkout, "rev-parse", "HEAD")
    captured: dict[str, object] = {}
    monkeypatch.setattr("cli.application.customize._ensure_candidate_environment", lambda *_: None)
    monkeypatch.setattr("cli.application.customize._build_web_assets", lambda *_: None)
    monkeypatch.setattr("cli.application.payload.native_source_digest", lambda _source: "d" * 64)

    def candidate(*args, **_kwargs):
        captured["revision"] = args[3]
        return "local_candidate"

    monkeypatch.setattr("cli.application.customize._candidate", candidate)

    assert source_updates.prepare_update(install, "upd_local") == "local_candidate"
    assert _git(checkout, "rev-parse", "HEAD") == local_revision
    assert captured["revision"] == local_revision


def test_default_main_binding_clones_into_installation_owned_source(
    tmp_path: Path,
    tracked_checkout: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
):
    checkout, _publisher = tracked_checkout
    remote = Path(_git(checkout, "remote", "get-url", "origin"))
    install = _install(tmp_path / "install")
    monkeypatch.setattr(source_updates, "CANONICAL_REPOSITORY", str(remote))

    binding = source_updates.bind_main_source(install)

    expected = install.root / "source"
    assert Path(binding["checkout"]) == expected.resolve()
    assert binding["remote"] == "origin"
    assert binding["branch"] == "main"
    assert (expected / "tracked.txt").read_text(encoding="utf-8") == "one\n"


def test_release_selection_removes_binding_but_preserves_source_checkout(
    tmp_path: Path, tracked_checkout: tuple[Path, Path]
):
    checkout, _publisher = tracked_checkout
    install = _install(tmp_path / "install")
    source_updates.select_source(install, "main", from_checkout=checkout)
    revision = _git(checkout, "rev-parse", "HEAD")
    repeated = source_updates.select_source(install, "main")

    assert Path(repeated["checkout"]) == checkout.resolve()

    result = source_updates.select_source(install, "release")

    assert result == {"source_track": "release", "checkout": None}
    assert source_updates.read_binding(install) is None
    assert checkout.is_dir()
    assert _git(checkout, "rev-parse", "HEAD") == revision
    assert install.version().name == "rel_old"


def test_failed_clone_keeps_unique_staging_evidence_for_each_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install = _install(tmp_path / "install")
    options: list[dict[str, object]] = []

    def failed_clone(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        options.append(kwargs)
        return subprocess.CompletedProcess(["git", "clone"], 1, "", "clone failed")

    monkeypatch.setattr(source_updates, "subprocess_creation_flags", lambda: 789)
    monkeypatch.setattr(
        source_updates.subprocess,
        "run",
        failed_clone,
    )

    for _attempt in range(2):
        with pytest.raises(ApplicationError, match="source-clones"):
            source_updates.bind_main_source(install)

    evidence = list((install.root / "source-clones").iterdir())
    assert len(evidence) == 2
    assert [option["creationflags"] for option in options] == [789, 789]
    assert len({path.name for path in evidence}) == 2
    assert not (install.root / "source").exists()
    assert "clone failed" in (install.root / "development" / "source-update.log").read_text(
        encoding="utf-8"
    )


def test_git_runs_windowless_and_retains_captured_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def run(arguments: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.update(kwargs)
        return subprocess.CompletedProcess(arguments, 1, "captured output", "captured error")

    monkeypatch.setattr(source_updates, "subprocess_creation_flags", lambda: 123)
    monkeypatch.setattr(source_updates.subprocess, "run", run)

    result = source_updates._git(tmp_path, "status", check=False)

    assert captured["creationflags"] == 123
    assert captured["capture_output"] is True
    assert result.stdout == "captured output"
    assert result.stderr == "captured error"


def test_desktop_client_source_update_skips_webui_build(
    tmp_path: Path,
    tracked_checkout: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
):
    checkout, _publisher = tracked_checkout
    install = _install(tmp_path / "install", shape="desktop-client")
    source_updates.bind_checkout(install, checkout)
    monkeypatch.setattr("cli.application.customize._ensure_candidate_environment", lambda *_: None)
    monkeypatch.setattr(
        "cli.application.customize._build_web_assets",
        lambda *_: pytest.fail("Desktop Client must not build WebUI assets"),
    )
    monkeypatch.setattr("cli.application.payload.native_source_digest", lambda _source: "d" * 64)
    monkeypatch.setattr(
        "cli.application.customize._candidate", lambda *_args, **_kwargs: "local_candidate"
    )

    assert source_updates.prepare_update(install, "upd_client") == "local_candidate"


def test_same_verified_revision_is_a_noop_without_build_tools(
    tmp_path, tracked_checkout, monkeypatch
):
    checkout, _ = tracked_checkout
    install = _install(tmp_path / "install")
    source_updates.bind_checkout(install, checkout)
    manifest = {
        "version": "1.2.3",
        "revision": _git(checkout, "rev-parse", "HEAD"),
        "build_inputs": source_updates.build_inputs(checkout, "server"),
        "native_source_digest": "d" * 64,
    }
    (install.version() / "release.json").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr("cli.application.payload.native_source_digest", lambda _: "d" * 64)
    validated = []
    monkeypatch.setattr(
        "cli.application.packages.validate_release", lambda *a, **kw: validated.append(a[0])
    )
    for name in ("_candidate", "_ensure_candidate_environment", "_build_web_assets"):
        monkeypatch.setattr(
            f"cli.application.customize.{name}", lambda *a, **kw: pytest.fail("no build needed")
        )
    targets = []

    assert (
        source_updates.prepare_update(
            install, "upd_current", progress=lambda message, target: targets.append(target)
        )
        == "rel_old"
    )
    assert validated == [install.version()]
    assert f"1.2.3 ({manifest['revision'][:8]})" in targets


@pytest.mark.parametrize(
    "path,changed",
    [
        ("tracked.txt", set()),
        ("webui/src/App.svelte", {"web"}),
        ("resources/extensions/demo/ui/page.html", {"web"}),
        ("resources/extensions/demo/backend.py", set()),
        ("tests/fixtures/extension-pages/demo/ui/page.html", {"web"}),
        ("scripts/windows/requirements-server.lock", {"dependencies"}),
    ],
)
def test_build_inputs_invalidate_only_affected_components(tracked_checkout, path, changed):
    checkout, _ = tracked_checkout
    before = source_updates.build_inputs(checkout, "server")
    target = checkout / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("changed\n", encoding="utf-8")
    _git(checkout, "add", ".")
    after = source_updates.build_inputs(checkout, "server")
    assert {key for key in before if before[key] != after[key]} == changed
    if "web" in changed:
        target.unlink()
        _git(checkout, "add", ".")
        assert source_updates.build_inputs(checkout, "server") == before


def test_backend_update_reuses_web_assets_and_announces_target_before_build(
    tmp_path, tracked_checkout, monkeypatch
):
    checkout, _ = tracked_checkout
    install = _install(tmp_path / "install")
    source_updates.bind_checkout(install, checkout)
    manifest = {
        "build_inputs": source_updates.build_inputs(checkout, "server"),
        "revision": "a" * 40,
    }
    (install.version() / "release.json").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr("cli.application.payload.native_source_digest", lambda _: "d" * 64)
    monkeypatch.setattr("cli.application.customize._ensure_candidate_environment", lambda *a: None)
    monkeypatch.setattr(
        "cli.application.customize._build_web_assets",
        lambda *a: pytest.fail("reuse verified assets"),
    )
    targets = []

    def candidate(*args, **kwargs):
        assert any(target and "1.2.3" in target for target in targets)
        assert kwargs["build_inputs"] == manifest["build_inputs"]
        return "local_new"

    monkeypatch.setattr("cli.application.customize._candidate", candidate)
    assert (
        source_updates.prepare_update(
            install, "upd_backend", progress=lambda message, target: targets.append(target)
        )
        == "local_new"
    )


@pytest.mark.parametrize(
    "group, changes_server_runtime",
    [("dev", False), ("desktop", False), ("server", True), ("windows-app", True)],
)
def test_uninstalled_dependency_groups_do_not_invalidate_runtime(
    tracked_checkout, group, changes_server_runtime
):
    checkout, _ = tracked_checkout
    before = source_updates.build_inputs(checkout, "server")
    project = checkout / "pyproject.toml"
    project.write_text(
        project.read_text(encoding="utf-8")
        + f'\n[project.optional-dependencies]\n{group} = ["test-owned-dependency==1.0"]\n',
        encoding="utf-8",
    )
    after = source_updates.build_inputs(checkout, "server")
    assert (before["dependencies"] != after["dependencies"]) is changes_server_runtime
    assert before["web"] == after["web"]
