"""Tests for provider credential commands: API keys, OAuth device flow and Custom Providers."""

from __future__ import annotations

import io
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.cli.cli_test_support import FakeRpc, RunCli

KEY_SAVED = {
    "provider_id": "openrouter",
    "connection_id": "openrouter:api-key",
    "account": "default",
    "credential_key": "OPENROUTER_API_KEY",
    "configured": True,
}
KEY_SAVED_FOR_WORK = KEY_SAVED | {"account": "work", "credential_key": "OPENROUTER_API_KEY__WORK"}
OAUTH_CONNECTIONS = [
    {"id": "openai:api-key", "provider_id": "openai", "type": "api_key"},
    {"id": "openai:subscription", "provider_id": "openai", "type": "oauth"},
    {"id": "other:oauth", "provider_id": "other", "type": "oauth"},
]
DEVICE_FLOW = {
    "user_code": "ABCD-1234",
    "verification_uri": "https://example.com/device",
    "expires_in": 900,
}


@pytest.mark.parametrize(
    ("argv", "stdin", "params", "saved", "expected"),
    [
        pytest.param(
            ("openrouter", "sk-or-test", "--connection", "openrouter:api-key"),
            None,
            {
                "provider_id": "openrouter",
                "value": "sk-or-test",
                "connection_id": "openrouter:api-key",
            },
            KEY_SAVED,
            "set openrouter:api-key credential OPENROUTER_API_KEY (account: default)",
            id="argument-and-connection",
        ),
        pytest.param(
            ("openrouter", "--stdin", "--account", "work"),
            "sk-or-test\r\n",
            {"provider_id": "openrouter", "value": "sk-or-test", "account": "work"},
            KEY_SAVED_FOR_WORK,
            "set openrouter:api-key credential OPENROUTER_API_KEY__WORK (account: work)",
            id="stdin-and-account",
        ),
    ],
)
def test_provider_key_set_saves_the_key_without_echoing_it(
    rpc: FakeRpc,
    run_cli: RunCli,
    monkeypatch: pytest.MonkeyPatch,
    argv: tuple[str, ...],
    stdin: str | None,
    params: dict[str, Any],
    saved: dict[str, Any],
    expected: str,
) -> None:
    if stdin is not None:
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    rpc.reply("provider.set_key", saved)

    code, out, err = run_cli("provider", "key", "set", *argv)

    assert code == 0
    assert rpc.calls == [("provider.set_key", params)]
    assert out.splitlines() == [expected]
    assert "sk-or-test" not in out + err


@pytest.mark.parametrize(
    ("refresh", "code", "shown"),
    [
        pytest.param({"provider_id": "openai", "model_count": 42}, 0, "42", id="refreshed"),
        pytest.param(
            {
                "provider_id": "openai",
                "errors": [{"connection_id": "openai:api-key", "error": "discovery-sentinel"}],
            },
            1,
            "discovery-sentinel",
            id="discovery-failed-after-save",
        ),
    ],
)
def test_provider_key_set_can_refresh_the_model_catalog(
    rpc: FakeRpc, run_cli: RunCli, refresh: dict[str, Any], code: int, shown: str
) -> None:
    rpc.reply(
        "provider.set_key", {"connection_id": "openai:api-key", "credential_key": "OPENAI_API_KEY"}
    )
    rpc.reply("model.refresh_db", refresh)

    exit_code, out, err = run_cli(
        "provider", "key", "set", "openai", "secret-sentinel", "--refresh-models"
    )

    assert exit_code == code
    assert rpc.calls == [
        ("provider.set_key", {"provider_id": "openai", "value": "secret-sentinel"}),
        ("model.refresh_db", {"provider_id": "openai"}),
    ]
    assert "set openai:api-key credential OPENAI_API_KEY (account: default)" in out
    assert shown in out
    assert "secret-sentinel" not in out + err


@pytest.mark.parametrize(
    ("options", "params", "result", "expected"),
    [
        pytest.param(
            ("--connection", "openrouter:api-key"),
            {"provider_id": "openrouter", "connection_id": "openrouter:api-key"},
            KEY_SAVED | {"removed": True, "configured": False},
            ["removed openrouter:api-key credential OPENROUTER_API_KEY (account: default)"],
            id="removed",
        ),
        pytest.param(
            ("--account", "work"),
            {"provider_id": "openrouter", "account": "work"},
            KEY_SAVED_FOR_WORK | {"removed": True, "configured": False},
            ["removed openrouter:api-key credential OPENROUTER_API_KEY__WORK (account: work)"],
            id="named-account",
        ),
        pytest.param(
            (),
            {"provider_id": "openrouter"},
            KEY_SAVED | {"removed": False, "configured": True},
            [
                "no stored credential OPENROUTER_API_KEY for openrouter:api-key (account: default)",
                "still configured from the process environment; "
                "unset the variable there to fully disable the connection",
            ],
            id="process-environment-remains",
        ),
    ],
)
def test_provider_key_unset_reports_what_remains_configured(
    rpc: FakeRpc,
    run_cli: RunCli,
    options: tuple[str, ...],
    params: dict[str, Any],
    result: dict[str, Any],
    expected: list[str],
) -> None:
    rpc.reply("provider.unset_key", result)

    code, out, _err = run_cli("provider", "key", "unset", "openrouter", *options)

    assert code == 0
    assert rpc.calls == [("provider.unset_key", params)]
    assert out.splitlines() == expected


@pytest.mark.parametrize(
    ("command", "method"),
    [
        pytest.param(("connect",), "provider.connect", id="connect"),
        pytest.param(("connection", "status"), "provider.connection_status", id="status"),
        pytest.param(("disconnect",), "provider.disconnect", id="disconnect"),
    ],
)
def test_oauth_commands_select_the_only_oauth_connection(
    rpc: FakeRpc, run_cli: RunCli, command: tuple[str, ...], method: str
) -> None:
    rpc.reply("connection.list", {"connections": OAUTH_CONNECTIONS})
    rpc.reply(method, DEVICE_FLOW | {"account": "work", "connected": True})

    code, _out, _err = run_cli("providers", *command, "openai", "--account", "work")

    assert code == 0
    assert rpc.calls == [
        ("connection.list", {}),
        (
            method,
            {"provider_id": "openai", "connection_id": "openai:subscription", "account": "work"},
        ),
    ]


@pytest.mark.parametrize(
    ("types", "shown"),
    [
        pytest.param([], ("example",), id="unknown-provider"),
        pytest.param(["api_key"], ("no OAuth Connection", "vbot provider key set"), id="no-oauth"),
        pytest.param(
            ["oauth", "oauth"],
            ("--connection", "example:connection-0", "example:connection-1"),
            id="several-oauth",
        ),
    ],
)
def test_oauth_connect_never_starts_without_exactly_one_oauth_connection(
    rpc: FakeRpc, run_cli: RunCli, types: list[str], shown: tuple[str, ...]
) -> None:
    connections = [
        {"id": f"example:connection-{index}", "provider_id": "example", "type": kind}
        for index, kind in enumerate(types)
    ]
    rpc.reply("connection.list", {"connections": connections})

    code, out, _err = run_cli("provider", "connect", "example")

    assert code == 1
    assert rpc.methods == ["connection.list"]
    for text in shown:
        assert text in out


@pytest.mark.parametrize(
    ("options", "account", "follow_up"),
    [
        pytest.param(
            (),
            "default",
            "vbot provider connection status openai --connection openai:subscription "
            "(keep the same target options)",
            id="default-account",
        ),
        pytest.param(
            ("--account", "work"),
            "work",
            "vbot provider connection status openai --connection openai:subscription "
            "--account work (keep the same target options)",
            id="named-account",
        ),
    ],
)
def test_oauth_connect_prints_the_device_flow_instructions(
    rpc: FakeRpc, run_cli: RunCli, options: tuple[str, ...], account: str, follow_up: str
) -> None:
    rpc.reply("provider.connect", DEVICE_FLOW | {"account": account})

    code, out, err = run_cli(
        "provider", "connect", "openai", "--connection", "openai:subscription", *options
    )

    assert code == 0
    params = {"provider_id": "openai", "connection_id": "openai:subscription"}
    assert rpc.calls == [("provider.connect", params | ({"account": account} if options else {}))]
    lines = out.splitlines()
    assert lines[:4] == [
        f"device flow started for openai:subscription (account: {account})",
        "user_code: ABCD-1234",
        "verification_uri: https://example.com/device",
        "expires_in_seconds: 900",
    ]
    assert lines[4].endswith(follow_up)
    assert "Login started; browser authorization is still required" in err


@pytest.mark.parametrize("account", ["default", "work"])
def test_oauth_disconnect_removes_the_account_token(
    rpc: FakeRpc, run_cli: RunCli, account: str
) -> None:
    options = () if account == "default" else ("--account", account)
    rpc.reply("provider.disconnect", {"account": account, "status": "disconnected"})

    code, out, _err = run_cli(
        "provider", "disconnect", "openai", "--connection", "openai:subscription", *options
    )

    assert code == 0
    params = {"provider_id": "openai", "connection_id": "openai:subscription"}
    assert rpc.calls == [
        ("provider.disconnect", params | ({"account": account} if options else {}))
    ]
    assert out.splitlines() == [f"disconnected openai:subscription (account: {account})"]


@pytest.mark.parametrize(
    ("options", "state", "expected", "attention"),
    [
        pytest.param(
            (),
            {"connected": True, "flow_active": False, "account": "default"},
            "openai:subscription: account=default connected=yes flow_active=no",
            None,
            id="connected",
        ),
        pytest.param(
            ("--account", "work"),
            {"connected": False, "flow_active": True, "account": "work"},
            "openai:subscription: account=work connected=no flow_active=yes",
            "Account is not connected; inspect the device-flow state",
            id="flow-pending",
        ),
    ],
)
def test_oauth_connection_status_reports_the_account_state(
    rpc: FakeRpc,
    run_cli: RunCli,
    options: tuple[str, ...],
    state: dict[str, Any],
    expected: str,
    attention: str | None,
) -> None:
    rpc.reply("provider.connection_status", state)

    code, out, err = run_cli(
        "provider",
        "connection",
        "status",
        "openai",
        "--connection",
        "openai:subscription",
        *options,
    )

    assert code == 0
    params = {"provider_id": "openai", "connection_id": "openai:subscription"}
    account = {"account": state["account"]} if options else {}
    assert rpc.calls == [("provider.connection_status", params | account)]
    assert out.splitlines() == [expected]
    if attention is None:
        assert "not connected" not in err
    else:
        assert attention in err


def test_oauth_commands_surface_server_errors(rpc: FakeRpc, run_cli: RunCli) -> None:
    rpc.fail(
        "provider.connect",
        "oauth_not_supported",
        "provider connection 'openai:api-key' is not an OAuth connection",
        status=200,
    )

    code, out, err = run_cli("provider", "connect", "openai", "--connection", "openai:api-key")

    assert code == 1
    assert out.startswith("oauth_not_supported:")
    assert "openai:api-key" in out
    assert "rpc_method: provider.connect" in err


_STORED_LOCAL_AI = {
    "id": "local-ai",
    "defaults": {"temperature": 0.4},
    "models": {"chat-model": {"name": "Chat", "capabilities": {"vision": True}}},
    "wire": {"defaults": {"reasoning": {"dialect": "thinking_toggle"}}},
}


@pytest.mark.parametrize(
    ("stored", "options", "stdin", "provider", "api_key", "saved", "expected"),
    [
        pytest.param(
            [],
            ("--api-key-stdin", "--models-endpoint", "/models", "--model", "chat-model"),
            "secret-sentinel\n",
            {
                "auth": "api_key",
                "models_endpoint": "/models",
                "models": {"chat-model": {"name": "chat-model", "capabilities": {}}},
            },
            {"api_key": "secret-sentinel"},
            {"id": "local-ai", "model_count": 1, "usable": True},
            "saved Custom Provider local-ai (1 models, usable)",
            id="api-key-from-stdin",
        ),
        pytest.param(
            [],
            ("--auth", "none", "--model", "chat-model", "--model", "image-model"),
            None,
            {
                "auth": "none",
                "models": {
                    "chat-model": {"name": "chat-model", "capabilities": {}},
                    "image-model": {"name": "image-model", "capabilities": {}},
                },
            },
            {},
            {"id": "local-ai", "usable": False},
            "saved Custom Provider local-ai (2 models, not usable)",
            id="keyless",
        ),
        pytest.param(
            [_STORED_LOCAL_AI],
            ("--auth", "none", "--model", "chat-model", "--model", "image-model"),
            None,
            {
                "auth": "none",
                "defaults": {"temperature": 0.4},
                "models": {
                    "chat-model": {"name": "Chat", "capabilities": {"vision": True}},
                    "image-model": {"name": "image-model", "capabilities": {}},
                },
                "wire": {"defaults": {"reasoning": {"dialect": "thinking_toggle"}}},
            },
            {},
            {"id": "local-ai", "model_count": 2, "usable": True},
            "saved Custom Provider local-ai (2 models, usable)",
            id="replacement-keeps-what-options-cannot-express",
        ),
        pytest.param(
            [_STORED_LOCAL_AI],
            ("--auth", "none", "--clear-wire"),
            None,
            {"auth": "none", "defaults": {"temperature": 0.4}, "models": {}},
            {},
            {"id": "local-ai", "model_count": 0, "usable": True},
            "saved Custom Provider local-ai (0 models, usable)",
            id="clear-wire",
        ),
    ],
)
def test_custom_provider_save_sends_the_definition_and_the_key_once(
    rpc: FakeRpc,
    run_cli: RunCli,
    monkeypatch: pytest.MonkeyPatch,
    stored: list[dict[str, Any]],
    options: tuple[str, ...],
    stdin: str | None,
    provider: dict[str, Any],
    api_key: dict[str, Any],
    saved: dict[str, Any],
    expected: str,
) -> None:
    if stdin is not None:
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    rpc.reply("provider.custom_list", {"providers": stored})
    rpc.reply("provider.custom_save", {"provider": saved})

    code, out, err = run_cli(
        "provider", "custom", "save", "local-ai",
        "--name", "Local AI",
        "--base-url", "http://127.0.0.1:8080/v1",
        *options,
    )  # fmt: skip

    assert code == 0
    definition = {
        "id": "local-ai",
        "name": "Local AI",
        "adapter": "openai_compatible",
        "base_url": "http://127.0.0.1:8080/v1",
    }
    assert rpc.calls == [
        ("provider.custom_list", {}),
        ("provider.custom_save", {"provider": definition | provider} | api_key),
    ]
    assert out.splitlines() == [expected]
    assert "secret-sentinel" not in out + err


def test_custom_provider_save_replaces_the_wire_block_from_a_file(
    rpc: FakeRpc, run_cli: RunCli, tmp_path: Path
) -> None:
    wire_file = tmp_path / "wire.json"
    wire_file.write_text('{"defaults": {"reasoning": {"supported": false}}}', encoding="utf-8")
    rpc.reply("provider.custom_list", {"providers": [_STORED_LOCAL_AI]})
    rpc.reply("provider.custom_save", {"provider": {"id": "local-ai", "usable": True}})

    code, _out, _err = run_cli(
        "provider", "custom", "save", "local-ai",
        "--name", "Local AI",
        "--base-url", "http://127.0.0.1:8080/v1",
        "--wire-file", str(wire_file),
    )  # fmt: skip

    assert code == 0
    assert rpc.calls[-1][1]["provider"]["wire"] == {"defaults": {"reasoning": {"supported": False}}}


@pytest.mark.parametrize(
    ("providers", "expected"),
    [
        pytest.param(
            [
                {
                    "id": "local-ai",
                    "name": "Local AI",
                    "auth": "none",
                    "base_url": "http://127.0.0.1:8080/v1",
                    "model_count": 2,
                    "credentials_configured": True,
                }
            ],
            [
                "Custom Providers:",
                "- id: local-ai  name: Local AI  auth: none  configured: yes  models: 2  "
                "endpoint: http://127.0.0.1:8080/v1",
            ],
            id="configured",
        ),
        pytest.param([], ["no Custom Providers configured"], id="empty"),
    ],
)
def test_custom_provider_list_shows_each_definition(
    rpc: FakeRpc, run_cli: RunCli, providers: list[dict[str, Any]], expected: list[str]
) -> None:
    rpc.reply("provider.custom_list", {"providers": providers})

    code, out, _err = run_cli("provider", "custom", "list")

    assert code == 0
    assert rpc.calls == [("provider.custom_list", {})]
    assert out.splitlines() == expected


def test_custom_provider_delete(rpc: FakeRpc, run_cli: RunCli) -> None:
    rpc.reply("provider.custom_delete", {"deleted": True})

    code, out, _err = run_cli("provider", "custom", "delete", "local-ai")

    assert code == 0
    assert rpc.calls == [("provider.custom_delete", {"provider_id": "local-ai"})]
    assert out.splitlines() == ["deleted Custom Provider local-ai"]
