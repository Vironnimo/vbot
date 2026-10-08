"""Tests for the ``vbot provider`` list, status, usage and enable/disable commands."""

from __future__ import annotations

from typing import Any

import pytest

from tests.cli.cli_test_support import FakeRpc, RunCli


def _connection(connection_id: str, **fields: Any) -> dict[str, Any]:
    provider_id, _, _ = connection_id.partition(":")
    connection = {
        "id": connection_id,
        "provider_id": provider_id,
        "type": "api_key",
        "label": "API Key",
        "enabled": True,
        "usable": True,
        "accounts": [],
    }
    return connection | fields


def _set_enabled_result(connection_id: str, enabled: bool, **fields: Any) -> dict[str, Any]:
    provider_id, _, _ = connection_id.partition(":")
    result = {
        "provider_id": provider_id,
        "connection_id": connection_id,
        "enabled": enabled,
        "configured": True,
    }
    return result | fields


def test_provider_list_overview_keeps_ids_and_offers_the_full_detail(
    rpc: FakeRpc, run_cli: RunCli
) -> None:
    connections = [
        _connection(
            "sample:oauth",
            type="oauth",
            label="Subscription",
            accounts=[{"id": "work", "usable": True, "source": "test-owned-source"}],
        ),
        _connection("local:server", type="none", label="Local", reachable=False),
        _connection("sample:key", label="API", enabled=False, usable=False),
    ]
    rpc.reply("connection.list", {"connections": connections})

    _code, brief, _err = run_cli("provider", "list")
    _code, full, _err = run_cli("provider", "list", "--details")

    assert rpc.calls == [("connection.list", {}), ("connection.list", {})]
    for connection in connections:
        assert connection["id"] in brief and connection["id"] in full
    assert "work" in brief and "work" in full
    assert "test-owned-source" not in brief and "test-owned-source" in full
    assert "vbot provider list --details" in brief
    assert "[WARN]" in next(line for line in brief.splitlines() if "local:server" in line)
    assert brief.index("sample:key") < brief.index("local:server")
    assert len(brief) < len(full)


def test_provider_list_details_show_each_account_state(rpc: FakeRpc, run_cli: RunCli) -> None:
    rpc.reply(
        "connection.list",
        {
            "connections": [
                _connection(
                    "openai:default",
                    label="OpenAI",
                    accounts=[
                        {
                            "id": "default",
                            "usable": True,
                            "source": "process_env",
                            "credential_key": "OPENAI_API_KEY",
                        },
                        {
                            "id": "work",
                            "usable": False,
                            "source": "data_dir",
                            "credential_key": "OPENAI_API_KEY__WORK",
                        },
                    ],
                ),
                _connection("openrouter:main", label="OpenRouter", usable=False),
            ]
        },
    )

    code, out, _err = run_cli("provider", "list", "--details")

    assert code == 0
    for text in (
        "openai:default",
        "openrouter:main",
        "usable: yes",
        "usable: no",
        "default",
        "process_env",
        "work",
        "data_dir",
    ):
        assert text in out


def test_provider_list_reports_the_empty_state(rpc: FakeRpc, run_cli: RunCli) -> None:
    rpc.reply("connection.list", {"connections": []})

    code, out, _err = run_cli("provider", "list")

    assert code == 0
    assert out.strip()


def test_provider_list_reports_a_server_error(rpc: FakeRpc, run_cli: RunCli) -> None:
    rpc.fail("connection.list", "provider_error", "boom", status=500)

    code, out, _err = run_cli("provider", "list")

    assert code == 1
    assert "provider_error: boom" in out


@pytest.mark.parametrize(
    ("argv", "connections", "code", "shown", "hidden"),
    [
        pytest.param(
            ("openrouter",),
            [_connection("openai:api-key"), _connection("openrouter:api-key", usable=False)],
            0,
            ("openrouter:api-key",),
            ("openai:api-key",),
            id="filters-by-provider",
        ),
        pytest.param(
            ("openrouter", "--connection", "openrouter:api-key"),
            [],
            1,
            ("openrouter:api-key",),
            (),
            id="missing-connection",
        ),
        pytest.param(
            ("openruter",),
            [_connection("openrouter:api-key")],
            1,
            ("openruter", "openrouter"),
            (),
            id="unknown-provider-suggests",
        ),
    ],
)
def test_provider_status_shows_the_provider_connections(
    rpc: FakeRpc,
    run_cli: RunCli,
    argv: tuple[str, ...],
    connections: list[dict[str, Any]],
    code: int,
    shown: tuple[str, ...],
    hidden: tuple[str, ...],
) -> None:
    rpc.reply("connection.list", {"connections": connections})

    exit_code, out, _err = run_cli("provider", "status", *argv)

    assert exit_code == code
    assert rpc.calls == [("connection.list", {})]
    for text in shown:
        assert text in out
    for text in hidden:
        assert text not in out


@pytest.mark.parametrize(
    ("options", "params", "provider", "expected"),
    [
        pytest.param(
            ("--connection", "openai:subscription"),
            {"connections": ["openai:subscription"]},
            {
                "connection": "openai:subscription",
                "display_name": "OpenAI",
                "plan": "Plus",
                "windows": [
                    {"label": "5h", "used_percent": 42.5, "reset_at": "2026-07-20T18:00:00Z"},
                    {"label": "Week", "used_percent": 12.0, "reset_at": None},
                ],
                "error": None,
            },
            [
                "- OpenAI (openai:subscription)  plan: Plus",
                "  - 5h: used=42.5% remaining=57.5% reset_at=2026-07-20T18:00:00Z",
                "  - Week: used=12% remaining=88% reset_at=-",
            ],
            id="live-windows",
        ),
        pytest.param(
            (),
            {},
            {
                "connection": "github-copilot:oauth",
                "display_name": "GitHub Copilot",
                "plan": None,
                "windows": [],
                "error": "Network error",
            },
            ["- GitHub Copilot (github-copilot:oauth)  plan: -", "  error: Network error"],
            id="provider-error",
        ),
        pytest.param(
            (),
            {},
            {
                "connection": "openrouter:api-key",
                "display_name": "OpenRouter",
                "plan": None,
                "credits": {"enabled": True, "balance": 4.7312, "unit": "USD"},
                "windows": [
                    {
                        "label": "API key spending cap",
                        "used_percent": 25.0,
                        "reset_at": None,
                        "used_units": 2.5,
                        "total_units": 10.0,
                        "unit": "USD",
                    }
                ],
                "error": None,
            },
            [
                "- OpenRouter (openrouter:api-key)  plan: -",
                "  credits: 4.73 USD",
                "  - API key spending cap: used=25% remaining=75% reset_at=- units=2.5/10 USD",
            ],
            id="credits-and-units",
        ),
    ],
)
def test_provider_usage_prints_the_live_usage_snapshot(
    rpc: FakeRpc,
    run_cli: RunCli,
    options: tuple[str, ...],
    params: dict[str, Any],
    provider: dict[str, Any],
    expected: list[str],
) -> None:
    rpc.reply("provider.usage", {"generated_at": "2026-07-20T16:00:00Z", "providers": [provider]})

    code, out, _err = run_cli("provider", "usage", *options)

    assert code == 0
    assert rpc.calls == [("provider.usage", params)]
    assert out.splitlines() == [
        "provider usage:",
        "generated_at: 2026-07-20T16:00:00Z",
        *expected,
    ]


@pytest.mark.parametrize(
    "target",
    [
        pytest.param(("ollama", "--connection"), id="provider-and-connection"),
        # The Connection id as provider list shows it stands for both.
        pytest.param((), id="connection-id"),
    ],
)
@pytest.mark.parametrize(
    ("connection_id", "saved", "shown"),
    [
        pytest.param(
            "ollama:local", {"reachable": True}, ("ollama:local", "reachable"), id="reachable"
        ),
        pytest.param(
            "ollama:local",
            {"reachable": False},
            ("ollama:local", "not reachable"),
            id="unreachable",
        ),
        pytest.param(
            "ollama:cloud",
            {"configured": False},
            ("provider status ollama --connection ollama:cloud",),
            id="missing-credential",
        ),
    ],
)
def test_provider_enable_with_an_explicit_connection_reports_its_readiness(
    rpc: FakeRpc,
    run_cli: RunCli,
    connection_id: str,
    saved: dict[str, Any],
    shown: tuple[str, ...],
    target: tuple[str, ...],
) -> None:
    rpc.reply("connection.set_enabled", _set_enabled_result(connection_id, True) | saved)

    code, out, _err = run_cli("provider", "enable", *target, connection_id)

    assert code == 0
    assert rpc.calls == [
        (
            "connection.set_enabled",
            {"provider_id": "ollama", "connection_id": connection_id, "enabled": True},
        )
    ]
    for text in shown:
        assert text in out


def test_provider_disable_resolves_the_only_connection(rpc: FakeRpc, run_cli: RunCli) -> None:
    rpc.reply("connection.list", {"connections": [_connection("openrouter:api-key")]})
    rpc.reply("connection.set_enabled", _set_enabled_result("openrouter:api-key", False))

    code, out, _err = run_cli("provider", "disable", "openrouter")

    assert code == 0
    assert rpc.calls == [
        ("connection.list", {}),
        (
            "connection.set_enabled",
            {"provider_id": "openrouter", "connection_id": "openrouter:api-key", "enabled": False},
        ),
    ]
    assert "openrouter:api-key" in out


def test_provider_enable_requires_a_connection_for_a_multi_connection_provider(
    rpc: FakeRpc, run_cli: RunCli
) -> None:
    rpc.reply(
        "connection.list",
        {
            "connections": [
                _connection(
                    "ollama:local", type="none", label="Local", enabled=False, usable=False
                ),
                _connection("ollama:cloud", label="Ollama Cloud", usable=False),
            ]
        },
    )

    code, out, _err = run_cli("provider", "enable", "ollama")

    assert code == 1
    assert rpc.methods == ["connection.list"]
    for text in ("pass --connection", "ollama:cloud", "ollama:local"):
        assert text in out


@pytest.mark.parametrize(
    ("options", "params", "samples", "expected"),
    [
        pytest.param(
            ("--since", "2026-08-01T00:00:00Z"),
            {"since": "2026-08-01T00:00:00Z"},
            [
                {
                    "sampled_at": "2026-08-01T10:00:00+00:00",
                    "providers": [
                        {
                            "connection": "openai:subscription",
                            "windows": [{"label": "5h", "used_percent": 42.0, "reset_at": "-"}],
                        },
                        {"connection": "broken:api-key", "error": "timeout"},
                    ],
                }
            ],
            [
                "- 2026-08-01T10:00:00+00:00",
                "  openai:subscription: - 5h: used=42% remaining=58% reset_at=-",
                "  broken:api-key: error: timeout",
            ],
            id="samples",
        ),
        pytest.param((), {}, [], None, id="empty-window"),
    ],
)
def test_provider_history_lists_recorded_usage_samples(
    rpc: FakeRpc,
    run_cli: RunCli,
    options: tuple[str, ...],
    params: dict[str, Any],
    samples: list[dict[str, Any]],
    expected: list[str] | None,
) -> None:
    rpc.reply(
        "provider.usage_history", {"generated_at": "2026-08-25T12:00:00+00:00", "samples": samples}
    )

    code, out, _err = run_cli("provider", "history", "list", *options)

    assert code == 0
    assert rpc.calls == [("provider.usage_history", params)]
    if expected is None:
        assert "no recorded usage samples" in out
    else:
        assert out.splitlines() == [
            "provider usage history:",
            "generated_at: 2026-08-25T12:00:00+00:00",
            *expected,
        ]


def test_provider_history_clear_requires_confirmation(rpc: FakeRpc, run_cli: RunCli) -> None:
    rpc.reply("provider.usage_history.clear", {"deleted_samples": 12})

    refused = run_cli("provider", "history", "clear")
    assert rpc.calls == []
    cleared = run_cli("provider", "history", "clear", "--yes")

    assert refused[0] == 1 and "--yes" in refused[1]
    assert cleared[0] == 0
    assert rpc.calls == [("provider.usage_history.clear", {})]
    assert cleared[1].splitlines() == ["deleted provider usage history: 12 samples"]
