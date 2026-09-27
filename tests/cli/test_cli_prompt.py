"""Tests for the ``vbot prompt`` commands: block requests, scopes and printed output."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.cli.cli_test_support import FakeRpc, RunCli

AGENT_SCOPE = {"type": "agent", "agent_id": "librarian"}


def test_prompt_list_prints_one_row_per_block_and_the_scopes(rpc: FakeRpc, run_cli: RunCli) -> None:
    text_block = {"owner": "always", "kind": "text", "editable": True, "enabled": True}
    rpc.reply(
        "prompt.list",
        {
            "blocks": [
                {
                    **text_block,
                    "id": "core:tools",
                    "rank": 0,
                    "source": "core",
                    "is_modified": False,
                },
                {
                    **text_block,
                    "id": "user:my-rules",
                    "rank": 1,
                    "source": "user",
                    "is_modified": True,
                },
                {
                    "id": "memory:guidance",
                    "owner": "memory",
                    "kind": "data",
                    "editable": False,
                    "enabled": False,
                    "rank": 2,
                    "source": "memory",
                },
            ],
            "scopes": [{"type": "default", "label": "Default"}],
        },
    )

    code, out, _err = run_cli("prompt", "list")

    assert code == 0
    assert rpc.calls == [("prompt.list", {})]
    assert out.splitlines()[3:] == [
        "- core:tools owner=always kind=text enabled=yes editable=yes source=core modified=no",
        "- user:my-rules owner=always kind=text enabled=yes editable=yes source=user modified=yes",
        "- memory:guidance owner=memory kind=data enabled=no editable=no source=memory modified=-",
    ]
    assert "default" in out


def test_prompt_list_reports_an_empty_block_list(rpc: FakeRpc, run_cli: RunCli) -> None:
    rpc.reply("prompt.list", {"blocks": [], "scopes": []})

    code, out, _err = run_cli("prompt", "list")

    assert code == 0
    assert out.strip()


def test_prompt_show_prints_only_the_requested_block_in_the_exact_scope(
    rpc: FakeRpc, run_cli: RunCli
) -> None:
    expected = {"id": "user:long", "text": "sentinel line\n" * 80, "editable": True}
    rpc.reply("prompt.list", {"blocks": [expected, {"id": "other", "text": "excluded"}]})

    code, out, _err = run_cli("prompt", "show", "user:long", "--scope", "agent:assistant")

    assert code == 0
    assert rpc.calls == [("prompt.list", {"scope": {"type": "agent", "agent_id": "assistant"}})]
    assert json.loads(out) == {"scope": "agent:assistant", **expected}


def test_prompt_update_sends_the_file_content_to_the_default_scope(
    rpc: FakeRpc, run_cli: RunCli, tmp_path: Path
) -> None:
    content_file = tmp_path / "tools.txt"
    content_file.write_text("# Custom tools", encoding="utf-8")
    rpc.reply("prompt.update", {"id": "core:tools", "text": "# Custom tools", "is_modified": True})

    code, out, _err = run_cli("prompt", "update", "core:tools", "--file", str(content_file))

    assert code == 0
    assert rpc.calls == [("prompt.update", {"id": "core:tools", "content": "# Custom tools"})]
    assert "core:tools" in out


def test_prompt_reset_restores_the_block(rpc: FakeRpc, run_cli: RunCli) -> None:
    rpc.reply("prompt.reset", {"id": "core:skills", "text": "# Skills", "is_modified": False})

    code, out, _err = run_cli("prompt", "reset", "core:skills")

    assert code == 0
    assert rpc.calls == [("prompt.reset", {"id": "core:skills"})]
    assert "core:skills" in out


def test_prompt_preview_prints_the_token_estimate_and_the_rendered_text(
    rpc: FakeRpc, run_cli: RunCli
) -> None:
    rpc.reply("prompt.preview", {"text": "System for coder", "tokens": 12, "estimated": True})

    code, out, _err = run_cli("prompt", "preview", "coder")

    assert code == 0
    assert rpc.calls == [("prompt.preview", {"agent_id": "coder"})]
    assert "12" in out and "estimated=yes" in out
    assert out.rstrip().endswith("System for coder")


def test_prompt_agent_scope_mutations_send_the_scope_object(rpc: FakeRpc, run_cli: RunCli) -> None:
    created = {
        "id": "user:catalog-rules",
        "owner": "always",
        "kind": "text",
        "enabled": True,
        "editable": True,
        "source": "user",
        "is_modified": True,
    }
    rpc.reply("prompt.create_block", created)
    rpc.reply(
        "prompt.set_layout",
        {"layout": [{"id": "user:catalog-rules", "enabled": True, "source": "user"}]},
    )
    rpc.reply("prompt.remove_block", {"id": "user:catalog-rules"})
    scope = ("--scope", "agent:librarian")

    content = ("--content", "Keep the index current.", "--position", "0")
    layout_json = ("--layout-json", '[{"id": "user:catalog-rules", "enabled": true}]')

    create = run_cli("prompt", "create", "catalog-rules", *content, *scope)
    layout = run_cli("prompt", "set-layout", *layout_json, *scope)
    remove = run_cli("prompt", "remove", "user:catalog-rules", *scope)

    assert [code for code, _out, _err in (create, layout, remove)] == [0, 0, 0]
    assert "user:catalog-rules" in create[1]
    assert "agent:librarian" in layout[1]
    assert "user:catalog-rules" in remove[1]
    assert rpc.params("prompt.create_block") == {
        "slug": "catalog-rules",
        "scope": AGENT_SCOPE,
        "content": "Keep the index current.",
        "position": 0,
    }
    assert rpc.params("prompt.set_layout")["scope"] == AGENT_SCOPE
    assert rpc.params("prompt.remove_block")["scope"] == AGENT_SCOPE


def test_prompt_set_layout_rejects_a_layout_that_is_not_an_array(
    rpc: FakeRpc, run_cli: RunCli
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        run_cli("prompt", "set-layout", "--layout-json", '{"id":"core:tools"}')

    assert exc_info.value.code == 2
    assert rpc.calls == []
