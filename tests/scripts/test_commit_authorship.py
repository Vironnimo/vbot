from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from scripts import commit_authorship

TOOL = "Claude <noreply@anthropic.com>"
USER = "Julian B. <virojb@web.de>"


def _git(root: Path, *args: str, env: dict[str, str] | None = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True, env=env
    ).stdout


@pytest.mark.parametrize(
    ("ident", "tool"),
    [
        (TOOL, True),
        ("Claude Opus 5.5 <noreply@anthropic.com>", True),
        ("Codex <codex@openai.com>", True),
        (USER, False),
        ("Claudia Meyer <claudia@example.com>", False),
    ],
)
def test_tool_identity(ident: str, tool: bool) -> None:
    assert commit_authorship.tool_identity(ident) is tool


def test_clean_message_strips_attribution_lines() -> None:
    message = (
        "fix(chat): keep the context window\n\nBody line.\n\n"
        f"Co-Authored-By: {TOOL}\n"
        "Claude-Session: https://claude.ai/code/session_x\n"
        "\U0001f916 Generated with [Claude Code](https://claude.com/claude-code)\n"
    )
    assert commit_authorship.clean_message(message) == (
        "fix(chat): keep the context window\n\nBody line.\n"
    )
    assert commit_authorship.clean_message("docs: plain\n") == "docs: plain\n"


def test_history_reports_tool_commits_and_passes_clean_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _git(tmp_path, "init", "-q")
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")} | {
        "GIT_COMMITTER_NAME": "Julian B.",
        "GIT_COMMITTER_EMAIL": "virojb@web.de",
    }
    _git(tmp_path, "commit", "-q", "--allow-empty", "--author", USER, "-m", "a", env=env)
    monkeypatch.chdir(tmp_path)
    assert commit_authorship.main(["history"]) == 0

    _git(tmp_path, "commit", "-q", "--allow-empty", "--author", TOOL, "-m", "b", env=env)
    _git(
        tmp_path, "commit", "-q", "--allow-empty", "--author", USER,
        "-m", f"c\n\nCo-Authored-By: {TOOL}", env=env,
    )  # fmt: skip
    assert commit_authorship.main(["history"]) == 1
    report = capsys.readouterr().err
    assert f"author {TOOL}" in report
    assert f"message line 'Co-Authored-By: {TOOL}'" in report
