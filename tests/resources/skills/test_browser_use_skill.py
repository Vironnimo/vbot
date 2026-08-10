"""Tests for the bundled script-backed Browser Use Skill."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from core.skills.skills import SkillRegistry
from core.tools.skill import load_skill_content

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SKILLS_ROOT = PROJECT_ROOT / "resources" / "skills"
SKILL_ROOT = SKILLS_ROOT / "browser-use"
SCRIPT_PATH = SKILL_ROOT / "scripts" / "browser_use.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("browser_use_skill_script", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError(f"Unable to load {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


BROWSER_USE = _load_script()


class FakeClient:
    def __init__(self, responses: list[Any] | None = None) -> None:
        self.responses = iter(responses or [])
        self.calls: list[list[str]] = []

    def version(self) -> str:
        return "playwright-cli 1.2.3"

    def call(self, arguments: list[str]) -> Any:
        self.calls.append(arguments)
        if arguments[0] == "screenshot":
            filename = arguments[arguments.index("--filename") + 1]
            Path(filename).write_bytes(b"fake-png")
        return next(self.responses, {})


def test_skill_requires_playwright_cli_and_exposes_script(tmp_path: Path) -> None:
    missing_registry = SkillRegistry.load(SKILLS_ROOT, environment={"PATH": ""})
    missing = missing_registry.availability_for("browser-use", ["*"])
    assert missing.state == "unavailable"
    assert missing.missing == ("missing binary 'playwright-cli'",)

    executable = tmp_path / ("playwright-cli.cmd" if sys.platform == "win32" else "playwright-cli")
    executable.write_text("", encoding="utf-8")
    executable.chmod(0o755)
    registry = SkillRegistry.load(SKILLS_ROOT, environment={"PATH": str(tmp_path)})
    assert registry.availability_for("browser-use", ["*"]).state == "available"

    activated = load_skill_content("browser-use", SKILL_ROOT / "SKILL.md")["content"]
    assert SCRIPT_PATH.as_posix() in activated
    assert "# Browser Use" in activated


def test_start_opens_isolated_session_and_returns_fresh_snapshot(tmp_path: Path) -> None:
    client = FakeClient([{"result": "opened"}, {"snapshot": "- heading Example"}])

    result = BROWSER_USE._execute(
        ["--session", "browser-test", "start", "https://example.com"],
        client=client,
        cwd=tmp_path,
    )

    assert result["ok"] is True
    assert result["snapshot"] == "- heading Example"
    assert client.calls == [["open", "https://example.com"], ["snapshot"]]


def test_navigate_rejects_non_web_and_credential_urls(tmp_path: Path) -> None:
    client = FakeClient()

    with pytest.raises(BROWSER_USE.BrowserUseError, match="http or https"):
        BROWSER_USE._execute(
            ["--session", "browser-test", "navigate", "file:///etc/passwd"],
            client=client,
            cwd=tmp_path,
        )
    with pytest.raises(BROWSER_USE.BrowserUseError, match="credentials"):
        BROWSER_USE._execute(
            ["--session", "browser-test", "navigate", "https://user:pass@example.com"],
            client=client,
            cwd=tmp_path,
        )

    assert client.calls == []


def test_oversized_snapshot_is_bounded_and_written_as_artifact(tmp_path: Path) -> None:
    snapshot = "x" * (BROWSER_USE.MAX_SNAPSHOT_CHARS + 1)
    client = FakeClient([{"snapshot": snapshot}])

    result = BROWSER_USE._execute(
        ["--session", "browser-test", "snapshot"], client=client, cwd=tmp_path
    )

    assert result["snapshot_truncated"] is True
    artifact = Path(result["snapshot_file"])
    assert artifact.read_text(encoding="utf-8") == snapshot
    assert artifact.is_relative_to(tmp_path / "tmp" / "browser-use" / "browser-test")


def test_screenshot_uses_controlled_output_path(tmp_path: Path) -> None:
    client = FakeClient([{"result": "captured"}])

    result = BROWSER_USE._execute(
        ["--session", "browser-test", "screenshot", "--full-page"],
        client=client,
        cwd=tmp_path,
    )

    screenshot = Path(result["screenshot"])
    assert screenshot.read_bytes() == b"fake-png"
    assert screenshot.is_relative_to(tmp_path / "tmp" / "browser-use" / "browser-test")
    assert client.calls[0][0] == "screenshot"
    assert "--full-page" in client.calls[0]


def test_invalid_session_is_rejected_before_backend_use(tmp_path: Path) -> None:
    client = FakeClient()

    with pytest.raises(BROWSER_USE.BrowserUseError, match="session must be"):
        BROWSER_USE._execute(["--session", "../escape", "doctor"], client=client, cwd=tmp_path)

    assert client.calls == []


def test_missing_backend_uses_stable_json_error_contract(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(BROWSER_USE.shutil, "which", lambda _: None)

    exit_code = BROWSER_USE.main(["--session", "browser-test", "doctor"])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.out == ""
    assert json.loads(captured.err)["ok"] is False
