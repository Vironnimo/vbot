"""Tests for the ``vbot debug`` commands: RPC requests and printed output."""

from __future__ import annotations

import pytest

from tests.cli.cli_test_support import FakeRpc, RunCli


@pytest.mark.parametrize(("enabled", "shown"), [(True, "yes"), (False, "no")])
def test_debug_status_prints_the_debug_state(
    rpc: FakeRpc, run_cli: RunCli, enabled: bool, shown: str
) -> None:
    state = {"enabled": enabled, "trace_limit": 50, "trace_count": 3, "data_directory": "C:/data"}
    rpc.reply("debug.status", state)

    code, out, _err = run_cli("debug", "status")

    assert code == 0
    assert rpc.calls == [("debug.status", {})]
    assert out.splitlines() == [
        f"enabled={shown} trace_limit=50 trace_count=3 data_directory=C:/data"
    ]


def test_debug_traces_prints_one_row_per_trace(rpc: FakeRpc, run_cli: RunCli) -> None:
    trace = {
        "trace_id": "abc123",
        "type": "model_probe",
        "timestamp": "2026-06-11T08:00:00+00:00",
        "duration_ms": 412,
        "provider_id": "openai",
        "model_id": "",
    }
    rpc.reply("debug.trace_list", {"traces": [trace]})

    code, out, _err = run_cli("debug", "traces")

    assert code == 0
    assert rpc.calls == [("debug.trace_list", {})]
    assert out.splitlines()[1:] == [
        "- id=abc123 type=model_probe timestamp=2026-06-11T08:00:00+00:00 "
        "duration_ms=412 provider=openai model=-"
    ]


def test_debug_trace_prints_the_trace_as_indented_json(rpc: FakeRpc, run_cli: RunCli) -> None:
    rpc.reply("debug.trace_get", {"trace": {"trace_id": "abc123", "type": "model_probe"}})

    code, out, _err = run_cli("debug", "trace", "abc123")

    assert code == 0
    assert rpc.calls == [("debug.trace_get", {"trace_id": "abc123"})]
    assert out.splitlines() == ["{", '  "trace_id": "abc123",', '  "type": "model_probe"', "}"]


def test_debug_clear_posts_the_clear_request(rpc: FakeRpc, run_cli: RunCli) -> None:
    rpc.reply("debug.trace_clear", {"cleared": True})

    code, out, _err = run_cli("debug", "clear")

    assert code == 0
    assert rpc.calls == [("debug.trace_clear", {})]
    assert out.strip()


def test_debug_probe_prints_the_model_preview_and_the_trace_hint(
    rpc: FakeRpc, run_cli: RunCli
) -> None:
    rpc.reply(
        "debug.model_probe",
        {
            "trace_id": "abc123",
            "status_code": 200,
            "duration_ms": 412,
            "raw_response": "{}",
            "model_preview": {
                "model_count": 2,
                "models": [
                    {"id": "gpt-5.2", "name": "GPT-5.2"},
                    {"id": "gpt-4o", "name": "GPT-4o"},
                ],
            },
        },
    )

    code, out, _err = run_cli("debug", "probe", "openai", "--connection", "openai:api-key")

    assert code == 0
    assert rpc.calls == [
        ("debug.model_probe", {"provider_id": "openai", "connection_id": "openai:api-key"})
    ]
    for line in (
        "probe openai: status_code=200 duration_ms=412 trace_id=abc123",
        "model_count: 2",
        "- gpt-5.2",
        "- gpt-4o",
        "debug trace abc123",
    ):
        assert line in out


def test_debug_commands_report_the_disabled_debug_mode(rpc: FakeRpc, run_cli: RunCli) -> None:
    rpc.fail("debug.trace_list", "domain_error", "debug mode is not enabled", status=200)

    code, out, err = run_cli("debug", "traces")

    assert code == 1
    assert "debug mode is not enabled" in out
    assert "rpc_method: debug.trace_list" in err
    assert "request_state: responded" in err
