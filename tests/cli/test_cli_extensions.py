"""Tests for the ``vbot extensions`` catalog, toggle, reload and settings commands."""

from __future__ import annotations

import io
import sys
from typing import Any

import pytest

from tests.cli.cli_test_support import FakeRpc, RunCli


def _capabilities(**overrides: Any) -> dict[str, Any]:
    capabilities = {
        "hooks": {},
        "tools": [],
        "recall_backends": [],
        "startup": False,
        "shutdown": False,
    }
    return capabilities | overrides


def _extension(name: str, status: str, **fields: Any) -> dict[str, Any]:
    extension = {
        "name": name,
        "status": status,
        "disabled": status == "disabled",
        "version": None,
        "description": None,
        "error": None,
        "config": {},
        "capability_errors": [],
        "ready_state": "ready",
        "capabilities": _capabilities(),
    }
    return extension | fields


def _extensions_payload() -> list[dict[str, Any]]:
    return [
        _extension(
            "guard_bash",
            "loaded",
            version="1.2.0",
            description="Guards dangerous bash",
            config={"deny": ["rm -rf"]},
            capabilities=_capabilities(
                hooks={"tool_call": 1},
                tools=[{"name": "word_count", "ready": True}],
                startup=True,
            ),
        ),
        _extension("broken", "failed", error="import failed: boom"),
        _extension("legacy", "disabled"),
    ]


def _with_legacy(**fields: Any) -> dict[str, Any]:
    payload = _extensions_payload()
    payload[2].update(fields)
    return {"extensions": payload}


def _schema_payload(*extra_fields: dict[str, Any]) -> dict[str, Any]:
    """One loaded Extension with a settings schema (Home-Assistant-shaped)."""
    return {
        "extensions": [
            _extension(
                "homeassistant",
                "loaded",
                version="1.0.0",
                description="Control a Home Assistant instance.",
                config={"url": "http://homeassistant.local:8123"},
                ready_state="waiting",
                settings_schema=[
                    {
                        "key": "url",
                        "type": "text",
                        "label": "Server URL",
                        "description": "Base URL.",
                        "required": False,
                        "default": "http://homeassistant.local:8123",
                    },
                    {
                        "key": "token",
                        "type": "secret",
                        "label": "Access token",
                        "description": "Long-lived token.",
                        "required": False,
                        "env_key": "HASS_TOKEN",
                        "set": False,
                    },
                    *extra_fields,
                ],
                capabilities=_capabilities(tools=[{"name": "ha_get_state", "ready": False}]),
            )
        ]
    }


@pytest.mark.parametrize(
    ("extensions", "shown"),
    [
        pytest.param(
            _extensions_payload(),
            (
                "guard_bash",
                "loaded",
                "v1.2.0",
                "Guards dangerous bash",
                "tool_call(1)",
                "word_count",
                "broken",
                "failed",
                "import failed: boom",
                "legacy",
                "disabled",
            ),
            id="loaded-failed-disabled",
        ),
        pytest.param(
            [
                _extension(
                    "homeassistant",
                    "overridden",
                    overridden_by="/data/extensions/homeassistant/__init__.py",
                    capabilities={},
                )
            ],
            ("homeassistant", "overridden", "/data/extensions/homeassistant/__init__.py"),
            id="overridden",
        ),
    ],
)
def test_extensions_list_prints_each_extension_state(
    rpc: FakeRpc, run_cli: RunCli, extensions: list[dict[str, Any]], shown: tuple[str, ...]
) -> None:
    rpc.reply("extensions.list", {"extensions": extensions})

    code, out, _err = run_cli("extensions", "list")

    assert code == 0
    assert rpc.calls == [("extensions.list", {})]
    for text in shown:
        assert text in out


def test_extensions_list_marks_tools_waiting_for_configuration(
    rpc: FakeRpc, run_cli: RunCli
) -> None:
    tools = [{"name": "ha_get_state", "ready": False}, {"name": "ha_call_service", "ready": False}]
    waiting = _extension(
        "homeassistant", "loaded", ready_state="waiting", capabilities=_capabilities(tools=tools)
    )
    rpc.reply("extensions.list", {"extensions": [waiting]})

    code, out, _err = run_cli("extensions", "list")

    lines = out.splitlines()
    assert code == 0
    assert lines[1] == "- homeassistant  loaded"
    # The waiting line names the not-ready tools and points at the fix.
    assert "ha_get_state" in lines[2] and "ha_call_service" in lines[2]
    assert "Settings > Extensions" in lines[2]
    # The capability tool list marks each not-ready tool inline.
    assert "ha_get_state (waiting)" in lines[3] and "ha_call_service (waiting)" in lines[3]


def test_extensions_disable_writes_the_settings_and_applies_live(
    rpc: FakeRpc, run_cli: RunCli
) -> None:
    rpc.reply("extensions.list", {"extensions": _extensions_payload()})
    rpc.reply("settings.update", {})

    code, out, _err = run_cli("extensions", "disable", "guard_bash")

    assert code == 0
    assert rpc.methods == ["extensions.list", "settings.update"]
    assert rpc.params("settings.update") == {
        "extensions": {
            "disabled": ["legacy", "guard_bash"],
            "config": {"guard_bash": {"deny": ["rm -rf"]}},
        }
    }
    # Disabling applies live and no longer mentions a restart.
    assert "guard_bash" in out and "restart" not in out


@pytest.mark.parametrize(
    ("name", "code", "shown"),
    [
        pytest.param("guard_bas", 1, "did you mean: guard_bash", id="unknown-name"),
        pytest.param("legacy", 0, "legacy", id="already-disabled"),
    ],
)
def test_extensions_disable_writes_nothing_for_an_unknown_or_disabled_extension(
    rpc: FakeRpc, run_cli: RunCli, name: str, code: int, shown: str
) -> None:
    rpc.reply("extensions.list", {"extensions": _extensions_payload()})

    exit_code, out, _err = run_cli("extensions", "disable", name)

    assert exit_code == code
    assert rpc.methods == ["extensions.list"]
    assert name in out and shown in out


@pytest.mark.parametrize(
    ("observation", "code", "shown"),
    [
        pytest.param(
            {"ok": True, "result": _with_legacy(status="loaded", disabled=False)},
            0,
            ("legacy",),
            id="loaded",
        ),
        # The toggle itself succeeded, but the re-list surfaces the bad state.
        pytest.param(
            {
                "ok": True,
                "result": _with_legacy(
                    status="failed", disabled=False, error="import failed: boom"
                ),
            },
            1,
            ("legacy", "failed", "import failed: boom"),
            id="failed-to-load",
        ),
        pytest.param(
            {"ok": False, "error": {"code": "observation_failed", "message": "read-sentinel"}},
            1,
            ("read-sentinel",),
            id="observation-failed",
        ),
    ],
)
def test_extensions_enable_saves_the_setting_and_reports_the_observed_load(
    rpc: FakeRpc,
    run_cli: RunCli,
    observation: dict[str, Any],
    code: int,
    shown: tuple[str, ...],
) -> None:
    rpc.reply("extensions.list", {"extensions": _extensions_payload()})
    rpc.respond("extensions.list", observation)
    rpc.reply("settings.update", {})

    exit_code, out, _err = run_cli("extensions", "enable", "legacy")

    assert exit_code == code
    assert rpc.methods == ["extensions.list", "settings.update", "extensions.list"]
    assert rpc.params("settings.update")["extensions"]["disabled"] == []
    for text in shown:
        assert text in out
    assert "restart" not in out


@pytest.mark.parametrize(
    ("extensions", "code", "shown"),
    [
        pytest.param(
            _extensions_payload(),
            1,
            (
                "1 loaded",
                "1 failed",
                "1 disabled",
                "broken",
                "import failed: boom",
                "vbot extensions list",
            ),
            id="with-failures",
        ),
        pytest.param(
            _extensions_payload()[:1],
            0,
            ("1 loaded", "0 failed", "0 disabled", "0 overridden"),
            id="clean",
        ),
    ],
)
def test_extensions_reload_prints_the_load_summary(
    rpc: FakeRpc,
    run_cli: RunCli,
    extensions: list[dict[str, Any]],
    code: int,
    shown: tuple[str, ...],
) -> None:
    rpc.reply("extensions.reload", {"extensions": extensions})

    exit_code, out, _err = run_cli("extensions", "reload")

    assert exit_code == code
    assert rpc.calls == [("extensions.reload", {})]
    for text in shown:
        assert text in out


def test_extensions_reload_with_an_extra_argument_is_a_usage_error(
    rpc: FakeRpc, run_cli: RunCli, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as error:
        run_cli("extensions", "reload", "extra")

    assert error.value.code == 2
    assert "vbot extensions reload --help" in capsys.readouterr().err
    assert rpc.calls == []


def test_extensions_show_prints_the_settings_without_secret_values(
    rpc: FakeRpc, run_cli: RunCli
) -> None:
    rpc.reply("extensions.list", _schema_payload())

    code, out, _err = run_cli("extensions", "homeassistant")

    lines = out.splitlines()
    assert code == 0
    assert lines[0] == "homeassistant  loaded"
    assert "settings:" in lines
    assert '  url (text): "http://homeassistant.local:8123"   Server URL' in lines
    # A secret shows only its set-state, never a value.
    assert "  token (secret): not set   Access token" in lines
    assert "set with: vbot extensions set homeassistant <field> <value>" in lines


def test_extensions_show_suggests_a_close_name(rpc: FakeRpc, run_cli: RunCli) -> None:
    rpc.reply("extensions.list", _schema_payload())

    code, out, _err = run_cli("extensions", "homeassistan")

    assert code == 1
    assert "homeassistan" in out and "did you mean: homeassistant" in out


@pytest.mark.parametrize("value", ["secret-xyz", ""])
def test_extensions_set_sends_a_secret_by_field_key_without_echoing_it(
    rpc: FakeRpc, run_cli: RunCli, value: str
) -> None:
    rpc.reply("extensions.list", _schema_payload())
    rpc.reply(
        "extensions.set_secret", {"name": "homeassistant", "key": "token", "set": bool(value)}
    )

    code, out, err = run_cli("extensions", "homeassistant", "set", "token", value)

    assert code == 0
    # The field key is sent, never the env key; the server maps it to HASS_TOKEN.
    assert rpc.params("extensions.set_secret") == {
        "name": "homeassistant",
        "key": "token",
        "value": value,
    }
    assert "token" in out and "homeassistant" in out
    if value:
        assert value not in out + err


class _BinaryStdin:
    """A console stdin whose ``buffer`` delivers raw bytes."""

    def __init__(self, data: bytes) -> None:
        self.buffer = io.BytesIO(data)


@pytest.mark.parametrize(
    ("stdin", "value"),
    [
        pytest.param("token-from-stdin\n", "token-from-stdin", id="text"),
        pytest.param(b"\xef\xbb\xbf" + "Łódź".encode() + b"\r\n", "Łódź", id="utf8-bytes-with-bom"),
    ],
)
def test_extensions_set_reads_the_value_from_stdin(
    rpc: FakeRpc,
    run_cli: RunCli,
    monkeypatch: pytest.MonkeyPatch,
    stdin: str | bytes,
    value: str,
) -> None:
    stream = io.StringIO(stdin) if isinstance(stdin, str) else _BinaryStdin(stdin)
    monkeypatch.setattr(sys, "stdin", stream)
    rpc.reply("extensions.list", _schema_payload())
    rpc.reply("extensions.set_secret", {"name": "homeassistant", "key": "token", "set": True})

    code, _out, _err = run_cli("extensions", "homeassistant", "set", "token", "--stdin")

    assert code == 0
    assert rpc.params("extensions.set_secret")["value"] == value


def test_extensions_set_rejects_non_utf8_stdin_before_any_request(
    rpc: FakeRpc, run_cli: RunCli, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "stdin", _BinaryStdin(b"\x81"))

    code, _out, _err = run_cli("extensions", "homeassistant", "set", "token", "--stdin")

    assert code == 1
    assert rpc.calls == []


@pytest.mark.parametrize(
    ("field", "raw", "saved"),
    [
        pytest.param("url", "http://pi.local:8123", "http://pi.local:8123", id="text"),
        pytest.param("verbose", "true", True, id="toggle"),
        pytest.param("timeout", "30", 30, id="number"),
    ],
)
def test_extensions_set_writes_the_coerced_value_into_the_merged_config(
    rpc: FakeRpc, run_cli: RunCli, field: str, raw: str, saved: object
) -> None:
    rpc.reply(
        "extensions.list",
        _schema_payload(
            {"key": "verbose", "type": "toggle", "label": "Verbose", "required": False},
            {"key": "timeout", "type": "number", "label": "Timeout", "required": False},
        ),
    )
    rpc.reply("settings.update", {})

    code, out, _err = run_cli("extensions", "homeassistant", "set", field, raw)

    assert code == 0
    merged = {"url": "http://homeassistant.local:8123", field: saved}
    assert rpc.params("settings.update") == {
        "extensions": {"disabled": [], "config": {"homeassistant": merged}}
    }
    assert f"homeassistant.{field}" in out


@pytest.mark.parametrize(
    ("name", "field", "raw", "shown"),
    [
        pytest.param("homeassistant", "timeout", "soon", "timeout", id="invalid-number"),
        pytest.param(
            "homeassistant", "hostname", "x", "available settings: url, token, timeout",
            id="unknown-field",
        ),
        pytest.param("nope", "url", "x", "nope", id="unknown-extension"),
    ],
)  # fmt: skip
def test_extensions_set_writes_nothing_for_an_invalid_setting(
    rpc: FakeRpc, run_cli: RunCli, name: str, field: str, raw: str, shown: str
) -> None:
    rpc.reply(
        "extensions.list",
        _schema_payload(
            {"key": "timeout", "type": "number", "label": "Timeout", "required": False}
        ),
    )

    code, out, _err = run_cli("extensions", name, "set", field, raw)

    assert code == 1
    assert rpc.methods == ["extensions.list"]
    assert shown in out
