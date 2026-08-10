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
        self.calls: list[tuple[list[str], list[str]]] = []

    def version(self) -> str:
        return "agent-browser 1.2.3"

    def call(
        self,
        arguments: list[str],
        *,
        launch_options: list[str] | None = None,
    ) -> Any:
        self.calls.append((arguments, launch_options or []))
        if arguments[0] == "screenshot":
            filename = next(value for value in arguments if value.endswith(".png"))
            Path(filename).write_bytes(b"fake-png")
        return next(self.responses, {})


def test_skill_requires_agent_browser(tmp_path: Path) -> None:
    missing_registry = SkillRegistry.load(SKILLS_ROOT, environment={"PATH": ""})
    missing = missing_registry.availability_for("browser-use", ["*"])
    assert missing.state == "unavailable"
    assert missing.missing

    executable = tmp_path / ("agent-browser.cmd" if sys.platform == "win32" else "agent-browser")
    executable.write_text("", encoding="utf-8")
    executable.chmod(0o755)
    registry = SkillRegistry.load(SKILLS_ROOT, environment={"PATH": str(tmp_path)})
    assert registry.availability_for("browser-use", ["*"]).state == "available"


def test_start_opens_isolated_session_and_returns_fresh_snapshot(tmp_path: Path) -> None:
    client = FakeClient(
        [
            {"success": True, "data": {"url": "https://example.com"}},
            {"success": True, "data": {"snapshot": "- heading Example"}},
        ]
    )

    result = BROWSER_USE._execute(
        ["--session", "browser-test", "start", "https://example.com"],
        client=client,
        cwd=tmp_path,
    )

    assert result["ok"] is True
    assert result["snapshot"] == "- heading Example"
    assert client.calls == [
        (["open", "https://example.com"], []),
        (["snapshot", "-i"], []),
    ]


def test_navigate_rejects_non_web_and_credential_urls(tmp_path: Path) -> None:
    client = FakeClient()

    with pytest.raises(BROWSER_USE.BrowserUseError):
        BROWSER_USE._execute(
            ["--session", "browser-test", "navigate", "file:///etc/passwd"],
            client=client,
            cwd=tmp_path,
        )
    with pytest.raises(BROWSER_USE.BrowserUseError):
        BROWSER_USE._execute(
            ["--session", "browser-test", "navigate", "https://user:pass@example.com"],
            client=client,
            cwd=tmp_path,
        )

    assert client.calls == []


def test_oversized_snapshot_is_bounded_and_written_as_artifact(tmp_path: Path) -> None:
    snapshot = "x" * (BROWSER_USE.MAX_SNAPSHOT_CHARS + 1)
    client = FakeClient([{"success": True, "data": {"snapshot": snapshot}}])

    result = BROWSER_USE._execute(
        ["--session", "browser-test", "snapshot"], client=client, cwd=tmp_path
    )

    assert result["snapshot_truncated"] is True
    artifact = Path(result["snapshot_file"])
    assert artifact.read_text(encoding="utf-8") == snapshot
    assert artifact.is_relative_to(tmp_path / "tmp" / "browser-use" / "browser-test")


def test_full_snapshot_includes_noninteractive_accessibility_tree(tmp_path: Path) -> None:
    client = FakeClient([{"success": True, "data": {"snapshot": "- heading Result"}}])

    result = BROWSER_USE._execute(
        ["--session", "browser-test", "snapshot", "--full"],
        client=client,
        cwd=tmp_path,
    )

    assert result["snapshot"] == "- heading Result"
    assert client.calls == [(["snapshot"], [])]


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
    assert client.calls[0][0][0] == "screenshot"
    assert "--full" in client.calls[0][0]


def test_fill_submit_uses_agent_browser_ref_and_press(tmp_path: Path) -> None:
    client = FakeClient([{}, {}, {"data": {"snapshot": "- textbox Search [ref=e7]"}}])

    result = BROWSER_USE._execute(
        [
            "--session",
            "browser-test",
            "fill",
            "@e7",
            "query",
            "--submit",
        ],
        client=client,
        cwd=tmp_path,
    )

    assert result["ok"] is True
    assert [call[0] for call in client.calls] == [
        ["fill", "@e7", "query"],
        ["press", "Enter"],
        ["snapshot", "-i"],
    ]


def test_click_rejects_non_ref_targets(tmp_path: Path) -> None:
    client = FakeClient()

    with pytest.raises(BROWSER_USE.BrowserUseError):
        BROWSER_USE._execute(
            ["--session", "browser-test", "click", "#submit"],
            client=client,
            cwd=tmp_path,
        )

    assert client.calls == []


def test_backend_invocation_uses_vbot_namespace_json_and_sanitized_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_run(command: list[str], **kwargs: Any) -> Any:
        captured["command"] = command
        captured["kwargs"] = kwargs
        kwargs["stdout"].write('{"success":true,"data":{"snapshot":"ok"}}')
        kwargs["stdout"].flush()
        kwargs["stderr"].flush()
        return BROWSER_USE.subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(BROWSER_USE.subprocess, "run", fake_run)
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    client = BROWSER_USE.AgentBrowserCli(
        "agent-browser", "browser-test", 45, Path("agent-browser.json")
    )

    result = client.call(["open", "https://example.com"], launch_options=["--engine", "chrome"])

    assert result["success"] is True
    assert captured["command"] == [
        "agent-browser",
        "--namespace",
        "vbot",
        "--session",
        "browser-test",
        "--config",
        "agent-browser.json",
        "--content-boundaries",
        "--no-auto-dialog",
        "--engine",
        "chrome",
        "open",
        "https://example.com",
        "--json",
    ]
    assert "OPENAI_API_KEY" not in captured["kwargs"]["env"]
    assert captured["kwargs"]["env"]["NO_COLOR"] == "1"
    assert captured["kwargs"]["stdout"] is not BROWSER_USE.subprocess.PIPE
    assert captured["kwargs"]["stderr"] is not BROWSER_USE.subprocess.PIPE
    assert "capture_output" not in captured["kwargs"]
    assert "shell" not in captured["kwargs"]


def test_invalid_session_is_rejected_before_backend_use(tmp_path: Path) -> None:
    client = FakeClient()

    with pytest.raises(BROWSER_USE.BrowserUseError):
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


def test_main_emits_ascii_safe_json_for_arbitrary_page_text(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        BROWSER_USE,
        "_execute",
        lambda _: {"ok": True, "snapshot": "non‑breaking ümlaut"},
    )

    assert BROWSER_USE.main(["--session", "browser-test", "snapshot"]) == 0

    output = capsys.readouterr().out
    output.encode("ascii")
    assert json.loads(output)["snapshot"] == "non‑breaking ümlaut"
