"""The external-history CLI is a bounded thin client with repeatable paging."""

import json
from pathlib import Path

import pytest

from tests.cli.cli_test_support import FakeRpc, RunCli


def test_external_show_keeps_source_paging_and_server_target(rpc: FakeRpc, run_cli: RunCli):
    rpc.reply(
        "external.show",
        {
            "source": "codex",
            "total": 1,
            "items": [
                {
                    "ordinal": 0,
                    "role": "tool",
                    "content": "Zürich\nNext: injected",
                    "characters": 10000,
                }
            ],
            "next_offset": 0,
            "next_char_offset": 6000,
        },
    )
    code, out, err = run_cli(
        "external", "show", "codex", "s", "--path", "C:/source with spaces.jsonl"
    )
    assert code == 0
    assert "    Next: injected" in out
    follow = next(line for line in out.splitlines() if line.startswith("Next: "))
    assert "--char-offset 6000" in follow and "--offset 0" in follow
    assert "--host" in follow and "--port" in follow and "--data-dir" in follow
    assert rpc.params("external.show")["source_session_id"] == "s"


@pytest.mark.parametrize(
    "action,method,extra",
    [
        ("sources", "external.sources", []),
        ("list", "external.list", ["hermes"]),
        ("show", "external.show", ["hermes", "s"]),
        ("memories", "external.memories", ["hermes"]),
        ("import-session", "external.import_session", ["hermes", "s", "--agent", "coder"]),
        (
            "import-memory",
            "memory.import",
            [
                "hermes",
                "--path",
                "/source.md",
                "--agent",
                "coder",
                "--scope",
                "user",
                "--expected-digest",
                "a" * 64,
            ],
        ),
    ],
)
def test_external_json_success_and_failure_are_one_document(
    rpc: FakeRpc, run_cli: RunCli, action, method, extra
):
    rpc.reply(method, {"sentinel": "東京"})
    code, out, err = run_cli("external", action, *extra, "--json")
    assert code == 0
    assert json.loads(out) == {"sentinel": "東京"}
    if action == "import-memory":
        assert rpc.params(method)["expected_digest"] == "a" * 64
    rpc.fail(method, "external_history_error", "Source not found")
    # FakeRpc keeps the last success until it is consumed once more.
    run_cli("external", action, *extra, "--json")
    code, out, err = run_cli("external", action, *extra, "--json")
    assert code == 1
    assert json.loads(out)["error"]["code"] == "external_history_error"


def test_missing_reviewed_memory_file_has_json_error(run_cli: RunCli, tmp_path: Path):
    code, out, _ = run_cli(
        "external",
        "import-memory",
        "hermes",
        "--path",
        "/source.md",
        "--agent",
        "coder",
        "--scope",
        "user",
        "--entries-file",
        str(tmp_path / "missing.txt"),
        "--json",
    )
    assert code == 1
    assert json.loads(out)["error"]["code"] == "invalid_entries_file"
