from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from scripts import commit_check

UNFORMATTED = "value  =  {'a':1}\n"
FORMATTED = 'value = {"a": 1}\n'


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "config", "core.autocrlf", "false")
    return tmp_path


def _staged_content(root: Path, path: str) -> str:
    return _git(root, "show", f":{path}")


def test_fixes_are_restaged_only_for_fully_staged_files(
    repo: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Another session's unstaged edit of a committed file must stay as it is.
    # mypy, not under test here, starts without a cache: tens of seconds under load.
    monkeypatch.setattr(commit_check, "check_types", lambda *_arguments: [])
    _write(repo, "other.py", FORMATTED)
    _git(repo, "add", "other.py")
    _git(repo, "commit", "-q", "-m", "other", "--no-verify")
    _write(repo, "other.py", UNFORMATTED)
    _write(repo, "staged.py", UNFORMATTED)
    _git(repo, "add", "staged.py")

    assert commit_check.main(repo) == 0

    assert (repo / "staged.py").read_text() == FORMATTED
    assert _staged_content(repo, "staged.py") == FORMATTED
    assert (repo / "other.py").read_text() == UNFORMATTED
    assert _git(repo, "diff", "--cached", "--name-only").split() == ["staged.py"]
    assert "FIXED and re-staged" in capsys.readouterr().out

    # Checking the fixed commit again finds nothing to fix.
    assert commit_check.main(repo) == 0
    assert "FIXED" not in capsys.readouterr().out


def test_partially_staged_file_is_left_alone_and_blocks(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(commit_check, "check_types", lambda *_arguments: [])
    (repo / "module.py").write_text(UNFORMATTED)
    _git(repo, "add", "module.py")
    work_in_progress = UNFORMATTED + "other  =  2\n"
    (repo / "module.py").write_text(work_in_progress)

    assert commit_check.main(repo) == 1

    assert (repo / "module.py").read_text() == work_in_progress
    assert _staged_content(repo, "module.py") == UNFORMATTED


def test_mypy_errors_block_unless_in_unstaged_work_in_progress() -> None:
    output = "\n".join(
        [
            # A note without an error, as mypy prints for committed files, never blocks.
            "tests/committed.py:7: note: By default the bodies of untyped functions are not"
            " checked, consider using --check-untyped-defs  [annotation-unchecked]",
            "core\\staged.py:3: error: Incompatible return value  [return-value]",
            "core/caller.py:9: error: Missing positional argument  [call-arg]",
            "core/wip.py:1:5: error: Name 'x' is not defined  [name-defined]",
            "core/wip.py:1: note: See https://mypy.readthedocs.io",
            "Found 3 errors in 3 files (checked 4 source files)",
        ]
    )

    blocking, in_progress = commit_check.split_mypy_output(
        output, staged={"core/staged.py"}, dirty={"core/wip.py"}
    )

    assert [line.split(":")[0] for line in blocking] == ["core\\staged.py", "core/caller.py"]
    assert [line.split(":")[0] for line in in_progress] == ["core/wip.py", "core/wip.py"]


@pytest.mark.parametrize(
    ("path", "source", "all_styles"),
    [
        ("webui/src/lib/i18n.js", True, False),
        ("webui/scripts/build-extension-pages.mjs", True, False),
        ("resources/extensions/swarm/ui/SwarmPage.svelte", True, False),
        ("tests/fixtures/extension-pages/alpha/ui/main.js", True, False),
        ("webui/package.json", False, True),
        ("webui/package-lock.json", False, True),
        ("webui/eslint.config.js", False, True),
        ("webui/prettier.config.js", False, True),
        ("webui/vite.config.js", False, False),
        ("resources/extensions/swarm/web/index.js", False, False),
        ("tests/e2e/tests/chat.spec.js", False, False),
    ],
)
def test_webui_changes_select_the_checks_they_can_affect(
    path: str, source: bool, all_styles: bool
) -> None:
    scope = commit_check.webui_scope([path])

    assert (scope.sources == [path], scope.all_styles) == (source, all_styles)


LOCKED_PACKAGES: dict[str, dict[str, object]] = {
    "node_modules/vite": {
        "version": "8.0.3",
        "resolved": "https://registry.npmjs.org/vite/-/vite-8.0.3.tgz",
        "integrity": "sha512-vite",
        "dev": True,
    },
    # npm installs an optional package only on the platforms it names.
    "node_modules/fsevents": {
        "version": "2.3.3",
        "resolved": "https://registry.npmjs.org/fsevents/-/fsevents-2.3.3.tgz",
        "integrity": "sha512-fsevents",
        "optional": True,
        "os": ["darwin"],
    },
}
INSTALLED_PACKAGES = {"node_modules/vite": LOCKED_PACKAGES["node_modules/vite"]}
# npm copies the manifest's dependency maps into the lock's root entry.
DEPENDENCIES = {"devDependencies": {"vite": "^8.0.0"}, "optionalDependencies": {"fsevents": "^2"}}


def _webui_project(
    root: Path, monkeypatch: pytest.MonkeyPatch, installed: Mapping[str, object] | None
) -> list[str]:
    """Give *root* a WebUI with *installed* packages; return the commands the checks start.

    ``installed`` None means npm never installed into node_modules. The checks run
    nothing: each command is recorded and succeeds.
    """
    webui = root / "webui"
    for package in ("prettier", "eslint"):
        manifest = webui / "node_modules" / package / "package.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({"bin": {package: f"bin/{package}.js"}}), encoding="utf-8")
    root_entry = {"name": "vbot-webui", **DEPENDENCIES}
    lock = {"lockfileVersion": 3, "packages": {"": root_entry, **LOCKED_PACKAGES}}
    (webui / "package-lock.json").write_text(json.dumps(lock), encoding="utf-8")
    (webui / "package.json").write_text(json.dumps(root_entry), encoding="utf-8")
    if installed is not None:
        record = {"lockfileVersion": 3, "packages": installed}
        (webui / "node_modules" / ".package-lock.json").write_text(
            json.dumps(record), encoding="utf-8"
        )
    (webui / "src" / "lib").mkdir(parents=True)
    _write(root, "webui/src/lib/i18n.js", "export const locale = 'en';\n")
    commands: list[str] = []

    def run(command: list[str], cwd: Path) -> Any:
        # "node <root>/webui/node_modules/eslint/bin/eslint.js ..." -> "node eslint ..."
        shown = [
            Path(part).stem if Path(part).suffix in {".js", ".cmd"} else part for part in command
        ]
        commands.append(" ".join(shown))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(commit_check, "_run", run)
    monkeypatch.setattr(commit_check.shutil, "which", lambda name: name)
    return commands


@pytest.mark.parametrize(
    ("installed", "runs"),
    [
        (INSTALLED_PACKAGES, True),
        (
            {"node_modules/vite": {**INSTALLED_PACKAGES["node_modules/vite"], "version": "8.0.1"}},
            False,
        ),
        (
            {"node_modules/vite": {**INSTALLED_PACKAGES["node_modules/vite"], "dev": False}},
            False,
        ),
        ({}, False),
        ({**INSTALLED_PACKAGES, "node_modules/left-pad": {"version": "1.3.0"}}, False),
        (None, False),
    ],
    ids=[
        "as locked",
        "other version",
        "other entry",
        "missing package",
        "extra package",
        "never installed",
    ],
)
def test_webui_checks_refuse_packages_that_differ_from_the_lock(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    installed: Mapping[str, object] | None,
    runs: bool,
) -> None:
    commands = _webui_project(repo, monkeypatch, installed)

    results = commit_check.check_frontend(repo, ["webui/src/lib/i18n.js"], set())

    if runs:
        assert not any(result.blocking for result in results)
        assert any(command.startswith("node prettier") for command in commands)
    else:
        # The WebUI checks refuse until `npm ci` installs the locked packages.
        assert [(result.label, result.blocking) for result in results] == [("webui deps", True)]
        assert commands == []


@pytest.mark.parametrize("path", ["webui/eslint.config.js", "webui/package-lock.json"])
def test_webui_tool_changes_check_every_source(
    repo: Path, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    commands = _webui_project(repo, monkeypatch, INSTALLED_PACKAGES)

    results = commit_check.check_frontend(repo, [path], set())

    assert commands == ["npm run format:check", "npm run lint"]
    assert not any(result.blocking for result in results)


@pytest.mark.parametrize(
    ("manifest", "runs"),
    [
        (DEPENDENCIES, True),
        # npm leaves out of the lock a dependency that optionalDependencies lists too.
        ({**DEPENDENCIES, "dependencies": {"fsevents": "^2"}}, True),
        ({**DEPENDENCIES, "devDependencies": {"vite": "^8.1.0"}}, False),
    ],
    ids=["as locked", "optional listed twice", "edited without npm install"],
)
def test_webui_checks_refuse_a_package_manifest_the_lock_does_not_record(
    repo: Path, monkeypatch: pytest.MonkeyPatch, manifest: Mapping[str, object], runs: bool
) -> None:
    commands = _webui_project(repo, monkeypatch, INSTALLED_PACKAGES)
    _write(repo, "webui/package.json", json.dumps({"name": "vbot-webui", **manifest}))

    results = commit_check.check_frontend(repo, ["webui/package.json"], set())

    if runs:
        assert not any(result.blocking for result in results)
        assert "npm run lint" in commands
    else:
        # `npm ci`, as CI runs it, would refuse the lock; `npm install` updates it.
        assert [(result.label, result.blocking) for result in results] == [("webui deps", True)]
        assert commands == []


def _write(root: Path, path: str, content: str) -> None:
    (root / path).write_bytes(content.encode("utf-8"))
