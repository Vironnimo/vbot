"""Shared fixtures and fakes for grep behavior tests."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest

import core.tools.grep as grep_module
from core.tools.grep import (
    GREP_TOOL_NAME,
)
from core.tools.tools import ToolContext, is_tool_result_envelope


def make_context(
    workspace: Path,
    tool_name: str = GREP_TOOL_NAME,
    *,
    cwd: Path | None = None,
    user_cancelled: bool = False,
) -> ToolContext:
    return ToolContext(
        agent_id="agent-1",
        session_id="session-1",
        run_id="run-1",
        tool_call_id="call-1",
        tool_name=tool_name,
        tool_call_index=0,
        workspace=workspace,
        vbot_root=workspace.parent,
        data_root=workspace.parent / "data",
        cwd=cwd,
        cancel_check_hook=(lambda: True) if user_cancelled else None,
    )


def force_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(grep_module.shutil, "which", lambda _name: None)


class FakeRgProcess:
    """Streaming stand-in for the ripgrep Popen handle."""

    def __init__(
        self, command: list[str], cwd: str, stdout_text: str, stderr_text: str, returncode: int
    ) -> None:
        self.command = command
        self.cwd = cwd
        self.stdout = io.StringIO(stdout_text)
        self.stderr = io.StringIO(stderr_text)
        self.returncode = returncode
        self.killed = False
        self._finished = False
        self.creationflags = 0

    def poll(self) -> int | None:
        return self.returncode if self._finished else None

    def kill(self) -> None:
        self.killed = True
        self._finished = True

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        self._finished = True
        return ("", self.stderr.read())


def install_fake_rg(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stdout_text: str = "",
    stderr_text: str = "",
    returncode: int = 0,
) -> list[FakeRgProcess]:
    monkeypatch.setattr(grep_module.shutil, "which", lambda _name: "rg")
    created: list[FakeRgProcess] = []

    def fake_popen(command: list[str], cwd: str | None = None, **_kwargs: Any) -> FakeRgProcess:
        process = FakeRgProcess(command, cwd or "", stdout_text, stderr_text, returncode)
        process.creationflags = int(_kwargs.get("creationflags", 0))
        created.append(process)
        return process

    monkeypatch.setattr(grep_module.subprocess, "Popen", fake_popen)
    return created


def get_success_content(result: dict[str, object]) -> str:
    data = assert_success_envelope(result)
    content = data["content"]
    assert isinstance(content, str)
    return content


def assert_success_envelope(result: dict[str, object]) -> dict[str, object]:
    assert is_tool_result_envelope(result) is True
    assert result["ok"] is True
    assert result["error"] is None
    assert result["artifacts"] == []
    data = result["data"]
    assert isinstance(data, dict)
    assert set(data) == {"content"}
    return data


def assert_failure_envelope(result: dict[str, object], code: str) -> dict[str, str]:
    assert is_tool_result_envelope(result) is True
    assert result["ok"] is False
    assert result["data"] is None
    assert result["artifacts"] == []
    error = result["error"]
    assert isinstance(error, dict)
    assert error["code"] == code
    assert isinstance(error["message"], str)
    assert error["message"]
    return error  # type: ignore[return-value]
