"""WMI process creation hands the broker its request and reports every refusal as OSError."""

from __future__ import annotations

import base64
import json
import subprocess
from types import SimpleNamespace
from typing import Any

import pytest

from core.utils import outside_job


@pytest.mark.parametrize(
    ("answer", "error"),
    [
        pytest.param(SimpleNamespace(returncode=0, stdout="4242\r\n"), None, id="created"),
        pytest.param(
            SimpleNamespace(returncode=1, stdout="Access denied\n"),
            "refused: Access denied",
            id="refused",
        ),
        pytest.param(SimpleNamespace(returncode=0, stdout=""), "refused: exit code 0", id="silent"),
        pytest.param(None, "did not answer", id="timeout"),
    ],
)
def test_start_outside_job_sends_the_exact_request_and_raises_oserror_on_refusal(
    monkeypatch: pytest.MonkeyPatch, answer: SimpleNamespace | None, error: str | None
) -> None:
    requests: list[dict[str, Any]] = []

    def run(argv: list[str], **kwargs: Any) -> SimpleNamespace:
        assert argv[0] == "powershell.exe" and "-EncodedCommand" in argv
        requests.append(json.loads(base64.b64decode(kwargs["input"]).decode("utf-8")))
        if answer is None:
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        return answer

    monkeypatch.setattr(outside_job.subprocess, "run", run)
    monkeypatch.setattr(
        "psutil.Process", lambda pid: SimpleNamespace(pid=pid, create_time=lambda: 9e12)
    )

    def start() -> outside_job.OutsideJobProcess:
        return outside_job.start_outside_job(
            ["C:/vBot/vBot.Server.exe", "--data-dir", "C:/Users/Jörg/vBot data"],
            cwd="C:/vBot/app",
            creationflags=0x08000200,
            env={"VBOT_HOME": "C:/Users/Jörg"},
        )

    if error is None:
        assert start().pid == 4242
    else:
        with pytest.raises(OSError, match=error):
            start()
    # Arguments are quoted into one command line; the environment replaces the whole block.
    assert requests == [
        {
            "command": 'C:/vBot/vBot.Server.exe --data-dir "C:/Users/Jörg/vBot data"',
            "directory": "C:/vBot/app",
            "flags": 0x08000200,
            "environment": ["VBOT_HOME=C:/Users/Jörg"],
        }
    ]
