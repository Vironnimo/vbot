"""Smoke test of the exact Provider reasoning-replay probe CLI."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from scripts import probe_reasoning_replay_exact


@respx.mock
def test_probe_runs_a_scenario_through_the_configured_connection(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / ".env").write_text("OPENCODE_API_KEY=test-key\n", encoding="utf-8")
    route = respx.post("https://opencode.ai/zen/go/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 20, "completion_tokens": 1},
            },
        )
    )

    code = probe_reasoning_replay_exact.main(
        [
            "--provider",
            "opencode-go",
            "--model",
            "deepseek-flash",
            "--scenario",
            "instruction",
            "--repeats",
            "1",
            "--data-dir",
            str(tmp_path),
        ]
    )

    assert code == 0
    assert route.call_count == 4  # one turn plus three follow-ups
    assert "behavioral recall only" in capsys.readouterr().out
    # OpenCode Go needs the client and one stable session header on every probe request.
    headers = [call.request.headers for call in route.calls]
    assert {(h["user-agent"], h["x-opencode-session"]) for h in headers} == {
        ("vBot", headers[0]["x-opencode-session"])
    }
    assert headers[0]["x-opencode-session"].startswith("vbot-")
