"""Tests for the ``vbot config`` commands: public Settings path requests and output."""

from __future__ import annotations

from typing import Any

import pytest

from tests.cli.cli_test_support import FakeRpc, RunCli

PROVIDER_CATALOG_ROW = {
    "path": "web_search.provider",
    "type": "string",
    "application": "live",
    "value": "brave",
    "source": "default",
}


@pytest.mark.parametrize(
    ("argv", "method", "settings", "expected"),
    [
        pytest.param(
            ("config", "raw"),
            "settings.get_raw",
            {"server_port": 8420},
            '{\n  "server_port": 8420\n}',
            id="raw",
        ),
        pytest.param(
            ("config", "effective"),
            "settings.values",
            {"web_search": {"provider": "brave"}},
            '{\n  "web_search": {\n    "provider": "brave"\n  }\n}',
            id="effective",
        ),
    ],
)
def test_config_raw_and_effective_print_the_settings_document(
    rpc: FakeRpc,
    run_cli: RunCli,
    argv: tuple[str, ...],
    method: str,
    settings: dict[str, Any],
    expected: str,
) -> None:
    rpc.reply(method, {"settings": settings})

    code, out, _err = run_cli(*argv)

    assert code == 0
    assert rpc.calls == [(method, {})]
    assert out.rstrip("\n") == expected


@pytest.mark.parametrize(
    ("argv", "params"),
    [
        pytest.param(("config", "list", "web_search"), {"prefix": "web_search"}, id="prefix"),
        pytest.param(("config", "list"), {}, id="all-paths"),
    ],
)
def test_config_list_prints_one_row_per_catalog_entry(
    rpc: FakeRpc, run_cli: RunCli, argv: tuple[str, ...], params: dict[str, str]
) -> None:
    rpc.reply("settings.catalog", {"settings": [PROVIDER_CATALOG_ROW]})

    code, out, _err = run_cli(*argv)

    assert code == 0
    assert rpc.calls == [("settings.catalog", params)]
    assert out.splitlines() == ['web_search.provider = "brave" (string, live, source=default)']


def test_config_get_prints_the_effective_json_value(rpc: FakeRpc, run_cli: RunCli) -> None:
    rpc.reply("settings.get_path", {"setting": {"path": "server.port", "value": 8420}})

    code, out, _err = run_cli("config", "get", "server.port")

    assert code == 0
    assert rpc.calls == [("settings.get_path", {"path": "server.port"})]
    assert out.splitlines() == ["8420"]


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(("config", "describe", "web_search.provider"), id="describe"),
        pytest.param(("config", "get", "web_search.provider", "--details"), id="get-details"),
    ],
)
def test_config_describe_prints_source_default_and_lifecycle(
    rpc: FakeRpc, run_cli: RunCli, argv: tuple[str, ...]
) -> None:
    rpc.reply(
        "settings.get_path",
        {
            "setting": {
                "path": "web_search.provider",
                "value": "searxng",
                "configured": True,
                "configured_value": "searxng",
                "source": "configured",
                "default": "brave",
                "type": "string",
                "allowed_values": ["brave", "duckduckgo", "searxng", "tavily"],
                "nullable": False,
                "unsettable": True,
                "application": "live",
                "restart_required": False,
                "description": "Provider used by web_search.",
            }
        },
    )

    code, out, _err = run_cli(*argv)

    assert code == 0
    assert rpc.calls == [
        ("settings.get_path", {"path": "web_search.provider", "allow_missing": True})
    ]
    for text in (
        "value: searxng",
        "source: configured",
        "default: brave",
        "application: live",
        "restart_required: false",
    ):
        assert text in out


def test_config_set_sends_one_atomic_patch_and_prints_the_applied_change(
    rpc: FakeRpc, run_cli: RunCli
) -> None:
    rpc.reply(
        "settings.patch",
        {
            "changed": ["web_search.provider"],
            "changes": [
                {
                    "path": "web_search.provider",
                    "value": "searxng",
                    "configured": True,
                    "configured_value": "searxng",
                    "application": "live",
                }
            ],
            "restart_required": False,
        },
    )

    code, out, _err = run_cli("config", "set", "web_search.provider", "searxng")

    assert code == 0
    assert rpc.calls == [
        (
            "settings.patch",
            {"operations": [{"op": "set", "path": "web_search.provider", "value": "searxng"}]},
        )
    ]
    for text in ('web_search.provider = "searxng"', "application: live", "restart_required: no"):
        assert text in out


def test_config_patch_coerces_json_values_and_reports_a_pending_restart(
    rpc: FakeRpc, run_cli: RunCli
) -> None:
    rpc.reply(
        "settings.patch",
        {
            "changed": ["server.port"],
            "changes": [
                {
                    "path": "server.port",
                    "value": 8420,
                    "configured": True,
                    "configured_value": 9000,
                    "pending_value": 9000,
                    "application": "restart",
                }
            ],
            "restart_required": True,
        },
    )

    code, out, err = run_cli(
        "config", "patch",
        "--set", "server.port", "9000",
        "--set", "debug.enabled", "true",
        "--set", "web_search.searxng", '{"a":1}',
        "--set", "web_search.searxng.base_url", "https://search.example",
        "--unset", "defaults.agent.temperature",
    )  # fmt: skip

    assert code == 0
    assert rpc.params("settings.patch") == {
        "operations": [
            {"op": "set", "path": "server.port", "value": 9000},
            {"op": "set", "path": "debug.enabled", "value": True},
            {"op": "set", "path": "web_search.searxng", "value": {"a": 1}},
            {"op": "set", "path": "web_search.searxng.base_url", "value": "https://search.example"},
            {"op": "unset", "path": "defaults.agent.temperature"},
        ]
    }
    for text in ("pending: 9000", "application: restart", "restart_required: yes"):
        assert text in out
    assert "Settings saved; restart required for pending values" in err


def test_config_unset_sends_an_unset_patch(rpc: FakeRpc, run_cli: RunCli) -> None:
    rpc.reply("settings.patch", {"changed": [], "changes": [], "restart_required": False})

    code, _out, _err = run_cli("config", "unset", "defaults.agent.temperature")

    assert code == 0
    assert rpc.calls == [
        ("settings.patch", {"operations": [{"op": "unset", "path": "defaults.agent.temperature"}]})
    ]


def test_config_patch_without_operations_sends_nothing(rpc: FakeRpc, run_cli: RunCli) -> None:
    code, out, _err = run_cli("config", "patch")

    assert code == 1
    assert "at least one --set or --unset" in out
    assert rpc.calls == []


def test_config_reports_a_server_error(rpc: FakeRpc, run_cli: RunCli) -> None:
    rpc.fail("settings.get_raw", "internal_error", "boom", status=500)

    code, out, _err = run_cli("config", "raw")

    assert code == 1
    assert "internal_error: boom" in out
