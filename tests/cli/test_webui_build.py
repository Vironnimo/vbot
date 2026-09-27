"""Tests for reusing installed WebUI npm packages across builds."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from cli.webui_build import build_webui


class _Npm:
    """Records npm commands; ``ci`` recreates node_modules like the real command."""

    def __init__(self, webui: Path) -> None:
        self._webui = webui
        self.commands: list[str] = []
        self.failing_builds = 0

    def __call__(self, arguments: list[str]) -> None:
        self.commands.append(" ".join(arguments))
        if arguments == ["ci"]:
            shutil.rmtree(self._webui / "node_modules", ignore_errors=True)
            (self._webui / "node_modules").mkdir()
        elif self.failing_builds:
            self.failing_builds -= 1
            raise RuntimeError("build failed")


def _webui(root: Path) -> Path:
    webui = root / "webui"
    webui.mkdir()
    (webui / "package.json").write_text('{"name": "webui"}', encoding="utf-8")
    (webui / "package-lock.json").write_text('{"lock": 1}', encoding="utf-8")
    return webui


def test_packages_are_reinstalled_only_when_their_inputs_change(tmp_path: Path) -> None:
    webui = _webui(tmp_path)
    npm = _Npm(webui)
    node = "v22.0.0"

    def build() -> list[str]:
        npm.commands.clear()
        build_webui(webui, node_version=node, npm=npm)
        return list(npm.commands)

    assert build() == ["ci", "run build"]
    assert build() == ["run build"]
    (webui / "package-lock.json").write_text('{"lock": 2}', encoding="utf-8")
    assert build() == ["ci", "run build"]
    (webui / "package.json").write_text('{"name": "renamed"}', encoding="utf-8")
    assert build() == ["ci", "run build"]
    node = "v24.0.0"
    assert build() == ["ci", "run build"]
    shutil.rmtree(webui / "node_modules")
    assert build() == ["ci", "run build"]
    assert build() == ["run build"]


def test_an_unknown_node_version_never_reuses_packages(tmp_path: Path) -> None:
    webui = _webui(tmp_path)
    npm = _Npm(webui)

    build_webui(webui, node_version=None, npm=npm)
    build_webui(webui, node_version=None, npm=npm)

    assert npm.commands == ["ci", "run build", "ci", "run build"]


@pytest.mark.parametrize("installed", [False, True])
def test_a_build_failing_with_reused_packages_retries_after_a_clean_install(
    tmp_path: Path, installed: bool
) -> None:
    webui = _webui(tmp_path)
    npm = _Npm(webui)
    if installed:
        build_webui(webui, node_version="v22.0.0", npm=npm)
        npm.commands.clear()
    npm.failing_builds = 1

    if installed:
        build_webui(webui, node_version="v22.0.0", npm=npm)
        assert npm.commands == ["run build", "ci", "run build"]
    else:
        with pytest.raises(RuntimeError):
            build_webui(webui, node_version="v22.0.0", npm=npm)
        assert npm.commands == ["ci", "run build"]
