"""WebUI and Extension page assets of ``vbot update``: release archives and dev builds."""

from __future__ import annotations

import io
import shutil
import subprocess
import tarfile
from pathlib import Path

import httpx
import pytest
import respx

from cli import _update_assets
from cli._update_assets import _extract_within
from cli.update_management import CommandRun, _default_runner
from tests.cli.update_management_test_support import _err, _ok, _webui_tar_bytes

NPM_CI = _update_assets._npm_command(["ci"])
NPM_BUILD = _update_assets._npm_command(["run", "build"])


@respx.mock
def test_release_download_installs_bundled_pages_without_overwriting_sources(
    tmp_path: Path,
) -> None:
    extension = tmp_path / "resources" / "extensions" / "alpha"
    extension.mkdir(parents=True)
    (extension / "extension.py").write_text("checkout source", encoding="utf-8")
    old_web = extension / "web"
    old_web.mkdir()
    (old_web / "obsolete.js").write_text("old asset", encoding="utf-8")
    removed = tmp_path / "resources" / "extensions" / "removed" / "web"
    removed.mkdir(parents=True)
    (removed / "page.html").write_text("removed page", encoding="utf-8")
    asset_url = "https://example.com/webui-dist.tar.gz"
    respx.get(asset_url).mock(
        return_value=httpx.Response(
            200,
            content=_webui_tar_bytes(
                {
                    "webui/dist/index.html": b"new app",
                    "resources/extensions/alpha/extension.py": b"archived source",
                    "resources/extensions/alpha/web/page.html": b"alpha page",
                    "resources/extensions/alpha/web/assets/new.js": b"alpha script",
                    "resources/extensions/beta/web/page.html": b"beta page",
                }
            ),
        )
    )

    result = _update_assets._download_webui(asset_url, tmp_path)

    assert result.ok
    assert (tmp_path / "webui" / "dist" / "index.html").read_bytes() == b"new app"
    assert (old_web / "assets" / "new.js").read_bytes() == b"alpha script"
    assert not (old_web / "obsolete.js").exists()
    assert not removed.exists()
    assert (extension / "extension.py").read_text(encoding="utf-8") == "checkout source"
    assert (extension.parent / "beta" / "web" / "page.html").read_bytes() == b"beta page"


def test_asset_swap_failure_restores_app_and_every_previous_extension(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    webui = tmp_path / "webui"
    originals = {
        "webui/dist/index.html": b"old app",
        "resources/extensions/alpha/web/page.html": b"old alpha",
        "resources/extensions/beta/web/page.html": b"old beta",
    }
    for name, content in originals.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    rename = Path.rename

    def fail_last_asset(source: Path, target: Path) -> Path:
        if "dist.staging" in source.parts and source.parts[-2:] == ("beta", "web"):
            raise OSError("injected asset swap failure")
        return rename(source, target)

    monkeypatch.setattr(Path, "rename", fail_last_asset)
    with pytest.raises(OSError, match="injected asset swap failure"):
        _update_assets._unpack_webui_archive(
            _webui_tar_bytes(dict.fromkeys(originals, b"replacement")),
            webui,
        )

    for name, content in originals.items():
        assert (tmp_path / name).read_bytes() == content
    assert not (webui / "dist.staging").exists()
    assert not (webui / "dist.backup").exists()


def test_legacy_asset_archive_keeps_existing_extension_assets(tmp_path: Path) -> None:
    page = tmp_path / "resources" / "extensions" / "alpha" / "web" / "page.html"
    page.parent.mkdir(parents=True)
    page.write_bytes(b"retained page")

    _update_assets._unpack_webui_archive(_webui_tar_bytes(), tmp_path / "webui")

    assert page.read_bytes() == b"retained page"
    assert (tmp_path / "webui" / "dist" / "index.html").is_file()


def test_incomplete_new_asset_archive_keeps_all_installed_assets(tmp_path: Path) -> None:
    index = tmp_path / "webui" / "dist" / "index.html"
    index.parent.mkdir(parents=True)
    index.write_bytes(b"retained app")
    with pytest.raises(ValueError, match="dist/index.html"):
        _update_assets._unpack_webui_archive(
            _webui_tar_bytes(
                {
                    "webui/dist/assets/bundle.js": b"no entry",
                    "resources/extensions/alpha/web/page.html": b"new page",
                }
            ),
            tmp_path / "webui",
        )

    assert index.read_bytes() == b"retained app"
    assert not (tmp_path / "resources").exists()


def test_extract_within_extracts_benign_archive(tmp_path: Path) -> None:
    # The same-tree fallback path used on Pythons without tarfile's data filter.
    destination = tmp_path / "webui"
    destination.mkdir()

    with tarfile.open(fileobj=io.BytesIO(_webui_tar_bytes()), mode="r:gz") as archive:
        _extract_within(archive, destination)

    assert (destination / "dist" / "index.html").is_file()


def _member(name: str, kind: bytes = tarfile.REGTYPE, *, linkname: str = "") -> tarfile.TarInfo:
    member = tarfile.TarInfo(name)
    member.type = kind
    member.linkname = linkname
    member.size = 1 if kind == tarfile.REGTYPE else 0
    return member


@pytest.mark.parametrize(
    ("member", "leftover"),
    [
        pytest.param(_member("../escape.txt"), "../escape.txt", id="path-escape"),
        # A symlink could redirect later members outside the tree after the name-based
        # pre-check has passed, so the fallback refuses links outright.
        pytest.param(
            _member("dist/evil", tarfile.SYMTYPE, linkname="../../outside"),
            "dist/evil",
            id="link",
        ),
        pytest.param(_member("dist/special", tarfile.FIFOTYPE), "dist/special", id="fifo"),
    ],
)
def test_extract_within_rejects_members_that_could_leave_the_tree(
    tmp_path: Path, member: tarfile.TarInfo, leftover: str
) -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        archive.addfile(member, io.BytesIO(b"x") if member.isfile() else None)
    buffer.seek(0)
    destination = tmp_path / "webui"
    destination.mkdir()

    with tarfile.open(fileobj=buffer, mode="r:gz") as archive, pytest.raises(tarfile.TarError):
        _extract_within(archive, destination)
    assert not (destination / leftover).exists()


BUILD_INPUT_CHANGES = [
    ("resources/extensions/swarm/ui/ProfileEditor.svelte", "edit", True),
    ("resources/extensions/other/ui/nested/component.js", "edit", True),
    ("resources/extensions/new/ui/page.html", "add", True),
    ("resources/extensions/old/ui/page.html", "delete", True),
    ("webui/src/lib/shared.js", "edit", True),
    ("tests/fixtures/extension-pages/alpha/ui/page.html", "edit", True),
    ("resources/extensions/swarm/backend.py", "edit", False),
    ("core/example.py", "edit", False),
]


@pytest.fixture(scope="module")
def build_input_commits(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[Path, str, dict[str, str]]:
    """One Git repository with a base commit and one commit per single-path change."""

    repo = tmp_path_factory.mktemp("webui-inputs")
    marks = repo / ".git" / "test-marks"
    # One fast-import stream writes every commit: blob :1 is "before", blob :2 is
    # "after", commit :10 is the base, and each change commit starts from it.
    stream = ["blob", "mark :1", "data 6", "before", "blob", "mark :2", "data 5", "after"]

    def commit(mark: int, *operations: str) -> None:
        header = [f"commit refs/heads/c{mark}", f"mark :{mark}"]
        stream.extend([*header, "committer Test <test@example.com> 0 +0000", "data 0"])
        stream.extend(operations)

    commit(
        10, *(f"M 100644 :1 {path}" for path, change, _ in BUILD_INPUT_CHANGES if change != "add")
    )
    for mark, (path, change, _rebuild) in enumerate(BUILD_INPUT_CHANGES, start=11):
        commit(mark, "from :10", f"D {path}" if change == "delete" else f"M 100644 :2 {path}")
    subprocess.run(["git", "init", "--quiet"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "fast-import", "--quiet", f"--export-marks={marks}"],
        cwd=repo,
        input=("\n".join(stream) + "\n").encode(),
        check=True,
        capture_output=True,
    )
    commits = dict(line.split() for line in marks.read_text(encoding="utf-8").splitlines())
    changes = {
        path: commits[f":{mark}"] for mark, (path, _, _) in enumerate(BUILD_INPUT_CHANGES, 11)
    }
    return repo, commits[":10"], changes


@pytest.mark.parametrize(("changed_path", "change", "rebuild"), BUILD_INPUT_CHANGES)
def test_dev_webui_detects_build_inputs_with_git(
    tmp_path: Path,
    build_input_commits: tuple[Path, str, dict[str, str]],
    changed_path: str,
    change: str,
    rebuild: bool,
) -> None:
    repo, before, changes = build_input_commits
    dist = tmp_path / "webui" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("existing build", encoding="utf-8")
    builds: list[list[str]] = []

    def runner(command: list[str], cwd: Path) -> CommandRun:
        if command[0] == "git":
            # Real Git decides which paths are build inputs.
            return _default_runner(command, repo)
        assert cwd == tmp_path / "webui"
        if command[0] != "node":
            builds.append(command)
        return _ok()

    result = _update_assets._refresh_dev_webui(runner, tmp_path, before, changes[changed_path])

    assert result.ok
    assert builds == ([NPM_CI, NPM_BUILD] if rebuild else [])


def test_dev_webui_reuses_installed_packages_while_their_inputs_are_unchanged(
    tmp_path: Path,
) -> None:
    webui = tmp_path / "webui"
    webui.mkdir()
    (webui / "package.json").write_text('{"name": "webui"}', encoding="utf-8")
    (webui / "package-lock.json").write_text('{"lock": 1}', encoding="utf-8")
    npm_calls: list[list[str]] = []

    def runner(command: list[str], cwd: Path) -> CommandRun:
        if command[:3] == ["git", "diff", "--quiet"]:
            return _err()
        if command == ["node", "--version"]:
            return _ok("v22.0.0")
        npm_calls.append(command)
        if command == NPM_CI:
            (webui / "node_modules").mkdir(exist_ok=True)
        else:
            (webui / "dist").mkdir(exist_ok=True)
            (webui / "dist" / "index.html").write_text("built", encoding="utf-8")
        return _ok()

    first = _update_assets._refresh_dev_webui(runner, tmp_path, None, "first")
    second = _update_assets._refresh_dev_webui(runner, tmp_path, "first", "second")

    assert first.ok, first.message
    assert second.ok, second.message
    assert npm_calls == [NPM_CI, NPM_BUILD, NPM_BUILD]


def _write_build(root: Path, content: str) -> None:
    """Write a WebUI build and the build of one Extension page."""

    dist = root / "webui" / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    (dist / "index.html").write_text(content, encoding="utf-8")
    page = root / "resources" / "extensions" / "swarm"
    (page / "ui").mkdir(parents=True, exist_ok=True)
    (page / "ui" / "page.html").write_text("page source", encoding="utf-8")
    (page / "web").mkdir(exist_ok=True)
    (page / "web" / "index.html").write_text(content, encoding="utf-8")


def _break_build(root: Path) -> None:
    """Leave every output tree the way a build failing part-way does."""

    pages = (root / "resources" / "extensions").glob("*/ui/page.html")
    for tree in [root / "webui" / "dist", *(page.parent.parent / "web" for page in pages)]:
        shutil.rmtree(tree, ignore_errors=True)
        tree.mkdir(parents=True)
        (tree / "partial.js").write_text("partial", encoding="utf-8")


def _checkout_files(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
        for path in root.rglob("*")
        if path.is_file() and ".previous-build" not in path.parts
    }


# The dependency install fails before any output; the build fails part-way.
@pytest.mark.parametrize(
    ("failing", "partial_output", "message"),
    [
        pytest.param(NPM_CI, False, "webui dependency install failed: lock mismatch", id="npm-ci"),
        pytest.param(NPM_BUILD, True, "webui build failed: render failed", id="npm-build"),
    ],
)
def test_dev_webui_failure_names_the_npm_step_and_keeps_the_previous_build(
    tmp_path: Path, failing: list[str], partial_output: bool, message: str
) -> None:
    _write_build(tmp_path, "old")
    # A page without a previous build: the failed build's partial tree must go too.
    added = tmp_path / "resources" / "extensions" / "added" / "ui"
    added.mkdir(parents=True)
    (added / "page.html").write_text("new page source", encoding="utf-8")
    before = _checkout_files(tmp_path)

    def runner(command: list[str], cwd: Path) -> CommandRun:
        if command[:3] == ["git", "diff", "--quiet"]:
            return _err()
        if command == failing:
            if partial_output:
                _break_build(tmp_path)
            return _err(message.rpartition(": ")[2])
        return _ok()

    result = _update_assets._refresh_dev_webui(runner, tmp_path, "old", "new")

    assert not result.ok
    assert result.message == f"{message}\nthe previous WebUI was kept"
    assert _checkout_files(tmp_path) == before
    assert not (tmp_path / "webui" / ".previous-build").exists()


def test_dev_webui_interrupted_build_is_restored_before_the_next_build(tmp_path: Path) -> None:
    _write_build(tmp_path, "old")
    before = _checkout_files(tmp_path)

    def interrupted(command: list[str], cwd: Path) -> CommandRun:
        if command[:3] == ["git", "diff", "--quiet"]:
            return _err()
        if command == NPM_BUILD:
            _break_build(tmp_path)
            raise KeyboardInterrupt
        return _ok()

    with pytest.raises(KeyboardInterrupt):
        _update_assets._refresh_dev_webui(interrupted, tmp_path, "old", "new")
    assert _checkout_files(tmp_path) != before
    seen_by_build: list[dict[str, str]] = []

    def resumed(command: list[str], cwd: Path) -> CommandRun:
        if command[:3] == ["git", "diff", "--quiet"]:
            return _err()
        if command == NPM_BUILD:
            seen_by_build.append(_checkout_files(tmp_path))
            _write_build(tmp_path, "new")
        return _ok()

    result = _update_assets._refresh_dev_webui(resumed, tmp_path, "old", "new")

    assert result.ok, result.message
    assert seen_by_build == [before]
    assert (tmp_path / "webui" / "dist" / "index.html").read_text(encoding="utf-8") == "new"
    assert not (tmp_path / "webui" / ".previous-build").exists()


@pytest.mark.parametrize("leftover", ["other_revision", "incomplete"])
def test_dev_webui_discards_a_copy_it_cannot_trust(tmp_path: Path, leftover: str) -> None:
    _write_build(tmp_path, "older")
    _update_assets._save_previous_build(
        tmp_path, "older" if leftover == "other_revision" else "current"
    )
    if leftover == "incomplete":
        (tmp_path / "webui" / ".previous-build" / "trees.json").unlink()
    _write_build(tmp_path, "current")

    result = _update_assets._refresh_dev_webui(
        lambda _command, _cwd: _ok(), tmp_path, "current", "current"
    )

    assert result.ok, result.message
    assert (tmp_path / "webui" / "dist" / "index.html").read_text(encoding="utf-8") == "current"
    assert not (tmp_path / "webui" / ".previous-build").exists()
