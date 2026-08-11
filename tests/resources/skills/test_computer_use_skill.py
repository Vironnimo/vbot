"""Tests for the bundled script-backed Computer Use Skill."""

from __future__ import annotations

import base64
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
SKILL_ROOT = SKILLS_ROOT / "computer-use"
SCRIPT_PATH = SKILL_ROOT / "scripts" / "computer_use.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("computer_use_skill_script", SCRIPT_PATH)
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


COMPUTER_USE = _load_script()


class FakeClient:
    def __init__(self, responses: list[Any] | None = None) -> None:
        self.responses = iter(responses or [])
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def version(self) -> str:
        return "cua-driver 1.2.3"

    def call(self, tool: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((tool, arguments))
        return next(self.responses, {})


def test_skill_requires_cua_driver_and_exposes_script(tmp_path: Path) -> None:
    missing_registry = SkillRegistry.load(SKILLS_ROOT, environment={"PATH": ""})
    missing = missing_registry.availability_for("computer-use", ["*"])
    assert missing.state == "unavailable"
    assert missing.missing

    executable = tmp_path / ("cua-driver.cmd" if sys.platform == "win32" else "cua-driver")
    executable.write_text("", encoding="utf-8")
    executable.chmod(0o755)
    registry = SkillRegistry.load(SKILLS_ROOT, environment={"PATH": str(tmp_path)})
    assert registry.availability_for("computer-use", ["*"]).state == "available"

    loaded = load_skill_content("computer-use", SKILL_ROOT / "SKILL.md")
    activated = loaded["activation_content"]
    assert SCRIPT_PATH.as_posix() in activated
    assert activated.startswith('<skill_content name="computer-use">')
    assert "<skill_content" not in loaded["content"]
    assert loaded["resource_files"]["files"] == [SCRIPT_PATH.as_posix()]


def test_capture_normalizes_screenshot_and_accessibility_data(tmp_path: Path) -> None:
    screenshot = base64.b64encode(b"fake-png").decode("ascii")
    client = FakeClient(
        [
            {
                "structuredContent": {
                    "screenshot_png_b64": screenshot,
                    "tree_markdown": "- button Save",
                    "elements": [{"index": 14, "role": "button", "name": "Save"}],
                }
            }
        ]
    )

    result = COMPUTER_USE._execute(
        [
            "--session",
            "desktop-test",
            "capture",
            "--pid",
            "1234",
            "--window-id",
            "5678",
            "--mode",
            "som",
        ],
        client=client,
        cwd=tmp_path,
    )

    screenshot_path = Path(result["screenshot"])
    assert screenshot_path.read_bytes() == b"fake-png"
    assert screenshot_path.is_relative_to(tmp_path / "tmp" / "computer-use" / "desktop-test")
    assert result["tree"] == "- button Save"
    assert result["elements"][0]["index"] == 14
    tool, arguments = client.calls[0]
    assert tool == "get_window_state"
    assert arguments["pid"] == 1234
    assert arguments["window_id"] == 5678


def test_click_is_dry_run_without_apply(tmp_path: Path) -> None:
    client = FakeClient()

    result = COMPUTER_USE._execute(
        [
            "--session",
            "desktop-test",
            "click",
            "--pid",
            "1234",
            "--window-id",
            "5678",
            "--element",
            "14",
        ],
        client=client,
        cwd=tmp_path,
    )

    assert result["applied"] is False
    assert result["dry_run"]["element_index"] == 14
    assert client.calls == []


def test_applied_click_calls_exact_window_and_element(tmp_path: Path) -> None:
    client = FakeClient([{"ok": True}])

    result = COMPUTER_USE._execute(
        [
            "--session",
            "desktop-test",
            "click",
            "--pid",
            "1234",
            "--window-id",
            "5678",
            "--element",
            "14",
            "--apply",
        ],
        client=client,
        cwd=tmp_path,
    )

    assert result["applied"] is True
    assert client.calls == [
        (
            "click",
            {
                "session": "desktop-test",
                "pid": 1234,
                "window_id": 5678,
                "element_index": 14,
                "button": "left",
            },
        )
    ]


def test_type_does_not_echo_text_in_result(tmp_path: Path) -> None:
    client = FakeClient([{"typed": True, "text": "backend echo"}])

    result = COMPUTER_USE._execute(
        [
            "--session",
            "desktop-test",
            "type",
            "--pid",
            "1234",
            "--window-id",
            "5678",
            "private draft",
            "--apply",
        ],
        client=client,
        cwd=tmp_path,
    )

    serialized = json.dumps(result)
    assert "private draft" not in serialized
    assert "backend echo" not in serialized
    assert result["applied"] is True


def test_dangerous_system_shortcut_is_blocked(tmp_path: Path) -> None:
    client = FakeClient()

    with pytest.raises(COMPUTER_USE.ComputerUseError):
        COMPUTER_USE._execute(
            [
                "--session",
                "desktop-test",
                "key",
                "--pid",
                "1234",
                "--window-id",
                "5678",
                "ctrl+alt+delete",
                "--apply",
            ],
            client=client,
            cwd=tmp_path,
        )

    assert client.calls == []


def test_click_requires_one_exact_target_kind(tmp_path: Path) -> None:
    client = FakeClient()

    with pytest.raises(COMPUTER_USE.ComputerUseError):
        COMPUTER_USE._execute(
            [
                "--session",
                "desktop-test",
                "click",
                "--pid",
                "1234",
                "--window-id",
                "5678",
            ],
            client=client,
            cwd=tmp_path,
        )

    assert client.calls == []


def test_missing_backend_uses_stable_json_error_contract(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(COMPUTER_USE.shutil, "which", lambda _: None)

    exit_code = COMPUTER_USE.main(["--session", "desktop-test", "doctor"])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.out == ""
    assert json.loads(captured.err)["ok"] is False
