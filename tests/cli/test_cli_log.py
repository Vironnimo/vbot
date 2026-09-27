"""Tests for the ``vbot log`` commands."""

from __future__ import annotations

from tests.cli.cli_test_support import FakeRpc, RunCli


def test_log_list_prints_the_daily_files_and_the_default(rpc: FakeRpc, run_cli: RunCli) -> None:
    rpc.reply("log.list", {"files": ["2026-05-11", "2026-05-10"], "default_file": "2026-05-11"})

    code, out, _err = run_cli("log", "list")

    assert code == 0
    assert rpc.calls == [("log.list", {})]
    assert out.count("2026-05-11") == 2
    assert "2026-05-10" in out


def test_log_read_prints_every_entry_field_and_its_continuation(
    rpc: FakeRpc, run_cli: RunCli
) -> None:
    entry = {
        "timestamp": "2026-05-11 09:00:00",
        "level": "info",
        "logger_name": "vbot.server.app",
        "message": "Ready",
        "continuation": "trace line",
    }
    rpc.reply("log.read", {"file": "2026-05-11", "cursor": "cursor-1", "entries": [entry]})

    code, out, _err = run_cli("log", "read", "2026-05-11")

    assert code == 0
    assert rpc.calls == [("log.read", {"file": "2026-05-11"})]
    for value in ("2026-05-11", *entry.values()):
        assert value in out


def test_log_read_filters_the_level_before_keeping_the_last_entries(
    rpc: FakeRpc, run_cli: RunCli
) -> None:
    entries: list[dict[str, str]] = [
        {"level": "error", "message": f"sentinel-{i}", "continuation": "traceback-sentinel"}
        for i in range(120)
    ]
    entries.append({"level": "info", "message": "excluded-info"})
    rpc.reply("log.read", {"entries": entries, "file": "test.log", "cursor": "unused-handoff"})

    code, out, _err = run_cli("log", "read", "test.log", "--limit", "2", "--level", "error")

    assert code == 0
    assert "sentinel-118" in out and "sentinel-119" in out
    assert "sentinel-117" not in out and "excluded-info" not in out
    assert "traceback-sentinel" in out
    assert "unused-handoff" not in out
