"""Bound source updates preserve Git work and the active native payload.

Preparation runs real Git, about fifteen processes per update, because fetch,
fast-forward, divergence and cleanliness semantics are the contract under test.
"""

from __future__ import annotations

import json
import logging
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any, NoReturn

import pytest

from cli.application import source_updates
from cli.application.state import ApplicationError, Installation
from tests.cli.application.git_repositories import SourceRepositories
from tests.cli.application.git_repositories import git as _git

NATIVE_DIGEST = "d" * 64
BUILD_STEPS = ("_ensure_candidate_environment", "_build_native_hosts", "_build_web_assets")


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


def _bound_install(tmp_path: Path, checkout: Path, *, shape: str = "server") -> Installation:
    install = _install(tmp_path / "install", shape=shape)
    source_updates.bind_checkout(install, checkout)
    return install


def _forbidden(reason: str) -> Callable[..., NoReturn]:
    def fail(*_args: object, **_kwargs: object) -> NoReturn:
        pytest.fail(reason)

    return fail


def _stub_build(monkeypatch: pytest.MonkeyPatch, **steps: Callable[..., Any]) -> None:
    """Replace the candidate build; steps not named succeed without doing work."""

    monkeypatch.setattr("cli.application.payload.native_source_digest", lambda _: NATIVE_DIGEST)
    for name in BUILD_STEPS:
        monkeypatch.setattr(
            f"cli.application.customize.{name}", steps.get(name, lambda *_a, **_k: None)
        )
    monkeypatch.setattr(
        "cli.application.customize._candidate",
        steps.get("_candidate", lambda *_a, **_k: "local_candidate"),
    )


def _publish(repositories: SourceRepositories, content: str) -> str:
    publisher = repositories.publisher
    (publisher / "tracked.txt").write_text(content, encoding="utf-8")
    _git(publisher, "commit", "--quiet", "-am", "upstream change")
    _git(publisher, "push", "--quiet", "origin", "main")
    return _git(publisher, "rev-parse", "HEAD")


def _commit_locally(checkout: Path) -> str:
    (checkout / "local-feature.txt").write_text("local commit\n", encoding="utf-8")
    _git(checkout, "add", "local-feature.txt")
    _git(checkout, "commit", "--quiet", "-m", "local feature")
    return _git(checkout, "rev-parse", "HEAD")


def test_inspect_and_bind_record_exact_tracking_branch(
    tmp_path: Path, source_repositories: SourceRepositories, caplog: pytest.LogCaptureFixture
) -> None:
    checkout = source_repositories.checkout
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
    tmp_path: Path, source_repositories: SourceRepositories, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = source_repositories.checkout
    install = _bound_install(tmp_path, checkout)
    (checkout / "local.txt").write_text("preserve me\n", encoding="utf-8")
    must_not_build = _forbidden("a dirty checkout must not build")
    _stub_build(monkeypatch, **dict.fromkeys((*BUILD_STEPS, "_candidate"), must_not_build))

    with pytest.raises(ApplicationError, match="uncommitted changes"):
        source_updates.prepare_update(install, "upd_dirty")

    assert (checkout / "local.txt").read_text(encoding="utf-8") == "preserve me\n"
    assert install.version().name == "rel_old"


def test_prepare_fast_forwards_recorded_branch_and_builds_exact_revision(
    tmp_path: Path, source_repositories: SourceRepositories, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = source_repositories.checkout
    install = _bound_install(tmp_path, checkout)
    expected_revision = _publish(source_repositories, "two\n")
    captured: dict[str, Any] = {}
    steps: list[str] = []

    def build_native_hosts(_install: object, source: Path, output: Path, *, version: str) -> None:
        steps.append("native")
        assert source == checkout.resolve()
        assert version == "1.2.3"
        (output / "vBot.Server.exe").write_bytes(b"compiled")

    def candidate(*args: object, **kwargs: Any) -> str:
        steps.append("candidate")
        captured["args"] = args
        captured["kwargs"] = kwargs
        captured["hosts"] = sorted(path.name for path in kwargs["native_hosts"].iterdir())
        return "local_candidate"

    _stub_build(
        monkeypatch,
        _build_native_hosts=build_native_hosts,
        _build_web_assets=lambda *_: steps.append("web"),
        _candidate=candidate,
    )

    assert source_updates.prepare_update(install, "upd_source") == "local_candidate"
    assert _git(checkout, "rev-parse", "HEAD") == expected_revision
    assert _git(checkout, "symbolic-ref", "--short", "HEAD") == "main"
    assert captured["args"][3] == expected_revision
    options = captured["kwargs"]
    assert steps == ["native", "web", "candidate"]
    assert captured["hosts"] == ["vBot.Server.exe"]
    assert not options["native_hosts"].exists()
    assert options["source_version"] == "1.2.3"
    assert options["native_source_digest"] == NATIVE_DIGEST
    assert options["build_inputs"] == source_updates.build_inputs(checkout, "server")
    assert install.version().name == "rel_old"


def _launchers_need_llvm(*_args: object, **_kwargs: object) -> NoReturn:
    raise ApplicationError("launchers need LLVM")


def _candidate_fails(*_args: object, **_kwargs: object) -> NoReturn:
    raise ApplicationError("candidate failed")


@pytest.mark.parametrize(
    ("failing_steps", "message"),
    [
        pytest.param(
            {
                "_build_native_hosts": _launchers_need_llvm,
                # A missing compiler fails before the WebUI and dependency work.
                "_build_web_assets": _forbidden("the launcher build must fail first"),
                "_candidate": _forbidden("the launcher build must fail first"),
            },
            "launchers need LLVM",
            id="launcher-build",
        ),
        pytest.param({"_candidate": _candidate_fails}, "candidate failed", id="candidate"),
    ],
)
def test_a_failed_build_keeps_the_active_version_and_a_clean_checkout(
    tmp_path: Path,
    source_repositories: SourceRepositories,
    monkeypatch: pytest.MonkeyPatch,
    failing_steps: dict[str, Callable[..., Any]],
    message: str,
) -> None:
    checkout = source_repositories.checkout
    install = _bound_install(tmp_path, checkout)
    _stub_build(monkeypatch, **failing_steps)

    with pytest.raises(ApplicationError, match=message):
        source_updates.prepare_update(install, "upd_failed")

    assert install.version().name == "rel_old"
    assert _git(checkout, "symbolic-ref", "--short", "HEAD") == "main"
    assert _git(checkout, "status", "--porcelain") == ""


@pytest.mark.parametrize("upstream_changed", [False, True], ids=["local-ahead", "diverged"])
def test_local_commits_are_built_and_never_discarded(
    tmp_path: Path,
    source_repositories: SourceRepositories,
    monkeypatch: pytest.MonkeyPatch,
    upstream_changed: bool,
) -> None:
    checkout = source_repositories.checkout
    install = _bound_install(tmp_path, checkout)
    local_revision = _commit_locally(checkout)
    if upstream_changed:
        _publish(source_repositories, "upstream\n")
    built: list[object] = []

    def candidate(*args: object, **_kwargs: object) -> str:
        built.append(args[3])
        return "local_candidate"

    _stub_build(monkeypatch, _candidate=candidate)

    if upstream_changed:
        with pytest.raises(ApplicationError, match="has diverged from origin/main"):
            source_updates.prepare_update(install, "upd_local")
    else:
        assert source_updates.prepare_update(install, "upd_local") == "local_candidate"
    assert _git(checkout, "rev-parse", "HEAD") == local_revision
    assert built == ([] if upstream_changed else [local_revision])


def test_default_main_binding_clones_into_installation_owned_source(
    tmp_path: Path, source_repositories: SourceRepositories, monkeypatch: pytest.MonkeyPatch
) -> None:
    install = _install(tmp_path / "install")
    monkeypatch.setattr(source_updates, "CANONICAL_REPOSITORY", str(source_repositories.remote))

    binding = source_updates.bind_main_source(install)

    expected = install.root / "source"
    assert Path(binding["checkout"]) == expected.resolve()
    assert binding["remote"] == "origin"
    assert binding["branch"] == "main"
    assert (expected / "tracked.txt").read_text(encoding="utf-8") == "one\n"


def test_release_selection_removes_binding_but_preserves_source_checkout(
    tmp_path: Path, source_repositories: SourceRepositories
) -> None:
    checkout = source_repositories.checkout
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
) -> None:
    install = _install(tmp_path / "install")
    options: list[dict[str, object]] = []

    def failed_clone(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        options.append(kwargs)
        return subprocess.CompletedProcess(["git", "clone"], 1, "", "clone failed")

    monkeypatch.setattr(source_updates, "subprocess_creation_flags", lambda: 789)
    monkeypatch.setattr(source_updates.subprocess, "run", failed_clone)

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
    tmp_path: Path, source_repositories: SourceRepositories, monkeypatch: pytest.MonkeyPatch
) -> None:
    install = _bound_install(tmp_path, source_repositories.checkout, shape="desktop-client")
    _stub_build(
        monkeypatch, _build_web_assets=_forbidden("Desktop Client must not build WebUI assets")
    )

    assert source_updates.prepare_update(install, "upd_client") == "local_candidate"


def test_same_verified_revision_is_a_noop_without_build_tools(
    tmp_path: Path, source_repositories: SourceRepositories, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = source_repositories.checkout
    install = _bound_install(tmp_path, checkout)
    revision = _git(checkout, "rev-parse", "HEAD")
    manifest = {
        "version": "1.2.3",
        "revision": revision,
        "build_inputs": source_updates.build_inputs(checkout, "server"),
        "native_source_digest": NATIVE_DIGEST,
    }
    (install.version() / "release.json").write_text(json.dumps(manifest), encoding="utf-8")
    validated: list[Path] = []
    monkeypatch.setattr(
        "cli.application.packages.validate_release", lambda *a, **kw: validated.append(a[0])
    )
    no_build = _forbidden("no build needed")
    _stub_build(monkeypatch, **dict.fromkeys((*BUILD_STEPS, "_candidate"), no_build))
    targets: list[str | None] = []

    assert (
        source_updates.prepare_update(
            install, "upd_current", progress=lambda message, target: targets.append(target)
        )
        == "rel_old"
    )
    assert validated == [install.version()]
    assert f"1.2.3 ({revision[:8]})" in targets


def _optional_group(group: str) -> str:
    return f'\n[project.optional-dependencies]\n{group} = ["test-owned-dependency==1.0"]\n'


@pytest.mark.parametrize(
    ("path", "addition", "changed"),
    [
        pytest.param("tracked.txt", "changed\n", set(), id="unrelated-file"),
        pytest.param("webui/src/App.svelte", "changed\n", {"web"}, id="webui"),
        pytest.param(
            "resources/extensions/demo/ui/page.html", "changed\n", {"web"}, id="extension-page"
        ),
        pytest.param(
            "resources/extensions/demo/backend.py", "changed\n", set(), id="extension-backend"
        ),
        pytest.param(
            "tests/fixtures/extension-pages/demo/ui/page.html",
            "changed\n",
            {"web"},
            id="fixture-extension-page",
        ),
        pytest.param(
            "scripts/windows/requirements-server.lock",
            "changed\n",
            {"dependencies"},
            id="runtime-lock",
        ),
        # Only the dependency groups a server runtime installs count.
        pytest.param("pyproject.toml", _optional_group("dev"), set(), id="dev-group"),
        pytest.param("pyproject.toml", _optional_group("desktop"), set(), id="desktop-group"),
        pytest.param(
            "pyproject.toml", _optional_group("server"), {"dependencies"}, id="server-group"
        ),
        pytest.param(
            "pyproject.toml",
            _optional_group("windows-app"),
            {"dependencies"},
            id="windows-app-group",
        ),
    ],
)
def test_build_inputs_change_only_for_the_components_they_affect(
    tmp_path: Path,
    plain_repository: Callable[[Path], str],
    path: str,
    addition: str,
    changed: set[str],
) -> None:
    checkout = tmp_path / "checkout"
    plain_repository(checkout)
    before = source_updates.build_inputs(checkout, "server")
    target = checkout / path
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as file:
        file.write(addition)
    _git(checkout, "add", ".")

    after = source_updates.build_inputs(checkout, "server")

    assert {key for key in before if before[key] != after[key]} == changed
    if "web" in changed:
        # The WebUI fingerprint follows content, not history.
        target.unlink()
        _git(checkout, "add", ".")
        assert source_updates.build_inputs(checkout, "server") == before


def test_backend_update_reuses_web_assets_and_announces_target_before_build(
    tmp_path: Path, source_repositories: SourceRepositories, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = source_repositories.checkout
    install = _bound_install(tmp_path, checkout)
    manifest = {
        "build_inputs": source_updates.build_inputs(checkout, "server"),
        "revision": "a" * 40,
        "native_source_digest": NATIVE_DIGEST,
    }
    (install.version() / "release.json").write_text(json.dumps(manifest), encoding="utf-8")
    targets: list[str | None] = []

    def candidate(*args: object, **kwargs: Any) -> str:
        assert any(target and "1.2.3" in target for target in targets)
        assert kwargs["build_inputs"] == manifest["build_inputs"]
        assert kwargs["native_hosts"] is None
        return "local_new"

    _stub_build(
        monkeypatch,
        _build_native_hosts=_forbidden("unchanged launchers need no compiler"),
        _build_web_assets=_forbidden("reuse verified assets"),
        _candidate=candidate,
    )

    assert (
        source_updates.prepare_update(
            install, "upd_backend", progress=lambda message, target: targets.append(target)
        )
        == "local_new"
    )
