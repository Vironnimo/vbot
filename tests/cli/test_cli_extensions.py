"""Tests for extensions catalog and enable/disable CLI commands."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from cli import extensions_management
from cli import main as cli_main
from cli.server_management import CommandResult, ServerInstance
from core.utils.logging import resolve_daily_log_path


def make_instance(tmp_path: Path) -> ServerInstance:
    data_dir = tmp_path / "data"
    return ServerInstance(
        host="127.0.0.1",
        port=8420,
        data_dir=data_dir,
        url="http://127.0.0.1:8420",
        log_path=resolve_daily_log_path(data_dir),
    )


def _extensions_payload() -> list[dict[str, Any]]:
    return [
        {
            "name": "guard_bash",
            "status": "loaded",
            "disabled": False,
            "version": "1.2.0",
            "description": "Guards dangerous bash",
            "error": None,
            "config": {"deny": ["rm -rf"]},
            "capability_errors": [],
            "ready_state": "ready",
            "capabilities": {
                "hooks": {"tool_call": 1},
                "tools": [{"name": "word_count", "ready": True}],
                "recall_backends": [],
                "startup": True,
                "shutdown": False,
            },
        },
        {
            "name": "broken",
            "status": "failed",
            "disabled": False,
            "version": None,
            "description": None,
            "error": "import failed: boom",
            "config": {},
            "capability_errors": [],
            "ready_state": "ready",
            "capabilities": {
                "hooks": {},
                "tools": [],
                "recall_backends": [],
                "startup": False,
                "shutdown": False,
            },
        },
        {
            "name": "legacy",
            "status": "disabled",
            "disabled": True,
            "version": None,
            "description": None,
            "error": None,
            "config": {},
            "capability_errors": [],
            "ready_state": "ready",
            "capabilities": {
                "hooks": {},
                "tools": [],
                "recall_backends": [],
                "startup": False,
                "shutdown": False,
            },
        },
    ]


def test_extensions_list_formats_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        assert json == {"method": "extensions.list", "params": {}}
        return httpx.Response(
            200, json={"ok": True, "result": {"extensions": _extensions_payload()}}
        )

    monkeypatch.setattr(extensions_management.httpx, "post", fake_post)

    result = extensions_management.extensions_list(instance)

    assert result.ok is True
    assert result.message == "\n".join(
        [
            "extensions:",
            "- guard_bash  loaded  v1.2.0  Guards dangerous bash",
            "    hooks: tool_call(1); tools: word_count; startup",
            "- broken  failed",
            "    error: import failed: boom",
            "- legacy  disabled",
        ]
    )


def test_extensions_list_renders_overridden_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)
    overridden_payload = [
        {
            "name": "homeassistant",
            "status": "overridden",
            "disabled": False,
            "version": None,
            "description": None,
            "error": None,
            "overridden_by": "/data/extensions/homeassistant/__init__.py",
            "config": {},
            "capability_errors": [],
            "capabilities": {},
        }
    ]

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": {"extensions": overridden_payload}})

    monkeypatch.setattr(extensions_management.httpx, "post", fake_post)

    result = extensions_management.extensions_list(instance)

    assert result.ok is True
    assert result.message == "\n".join(
        [
            "extensions:",
            "- homeassistant  overridden",
            "    overridden by /data/extensions/homeassistant/__init__.py",
        ]
    )


def test_extensions_list_renders_waiting_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)
    waiting_payload = [
        {
            "name": "homeassistant",
            "status": "loaded",
            "disabled": False,
            "version": None,
            "description": None,
            "error": None,
            "config": {},
            "capability_errors": [],
            "ready_state": "waiting",
            "capabilities": {
                "hooks": {},
                "tools": [
                    {"name": "ha_get_state", "ready": False},
                    {"name": "ha_call_service", "ready": False},
                ],
                "recall_backends": [],
                "startup": False,
                "shutdown": False,
            },
        }
    ]

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": {"extensions": waiting_payload}})

    monkeypatch.setattr(extensions_management.httpx, "post", fake_post)

    result = extensions_management.extensions_list(instance)

    assert result.ok is True
    lines = result.message.splitlines()
    assert lines[0] == "extensions:"
    assert lines[1] == "- homeassistant  loaded"
    # The waiting line names the not-ready tools and points at the fix.
    assert "waiting for configuration (ha_get_state, ha_call_service)" in lines[2]
    assert "Settings > Extensions" in lines[2]
    # The capability tool list marks each not-ready tool inline.
    assert "ha_get_state (waiting)" in lines[3]
    assert "ha_call_service (waiting)" in lines[3]


def test_extensions_disable_writes_settings_and_applies_live(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)
    posted: list[dict[str, Any]] = []

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        posted.append(json)
        if json["method"] == "extensions.list":
            return httpx.Response(
                200, json={"ok": True, "result": {"extensions": _extensions_payload()}}
            )
        assert json == {
            "method": "settings.update",
            "params": {
                "extensions": {
                    "disabled": ["legacy", "guard_bash"],
                    "config": {"guard_bash": {"deny": ["rm -rf"]}},
                }
            },
        }
        return httpx.Response(200, json={"ok": True, "result": {}})

    monkeypatch.setattr(extensions_management.httpx, "post", fake_post)

    result = extensions_management.extensions_disable(instance, "guard_bash")

    # Disabling applies live and no longer mentions a restart.
    assert result == CommandResult(
        ok=True,
        message="extension 'guard_bash' disabled",
        instance=instance,
    )
    assert "restart" not in result.message
    assert [call["method"] for call in posted] == ["extensions.list", "settings.update"]


def _enabled_payload() -> list[dict[str, Any]]:
    """The extensions payload after 'legacy' has been enabled (now loaded)."""
    payload = _extensions_payload()
    for extension in payload:
        if extension["name"] == "legacy":
            extension["status"] = "loaded"
            extension["disabled"] = False
    return payload


def test_extensions_enable_applies_live(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)
    posted: list[str] = []
    list_payloads = [_extensions_payload(), _enabled_payload()]

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        posted.append(json["method"])
        if json["method"] == "extensions.list":
            payload = list_payloads.pop(0) if list_payloads else _enabled_payload()
            return httpx.Response(200, json={"ok": True, "result": {"extensions": payload}})
        assert json["params"]["extensions"]["disabled"] == []
        return httpx.Response(200, json={"ok": True, "result": {}})

    monkeypatch.setattr(extensions_management.httpx, "post", fake_post)

    result = extensions_management.extensions_enable(instance, "legacy")

    assert result.ok is True
    # Enable re-lists to confirm the freshly rebuilt extension loaded; happy path
    # shows only the live-applied line, no restart, no warning.
    assert result.message == "extension 'legacy' enabled (applied live)"
    assert posted == ["extensions.list", "settings.update", "extensions.list"]


def test_extensions_enable_warns_when_extension_fails_to_load(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)

    def failed_payload() -> list[dict[str, Any]]:
        payload = _extensions_payload()
        for extension in payload:
            if extension["name"] == "legacy":
                extension["status"] = "failed"
                extension["disabled"] = False
                extension["error"] = "import failed: boom"
        return payload

    list_payloads = [_extensions_payload(), failed_payload()]

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        if json["method"] == "extensions.list":
            payload = list_payloads.pop(0) if list_payloads else failed_payload()
            return httpx.Response(200, json={"ok": True, "result": {"extensions": payload}})
        return httpx.Response(200, json={"ok": True, "result": {}})

    monkeypatch.setattr(extensions_management.httpx, "post", fake_post)

    result = extensions_management.extensions_enable(instance, "legacy")

    # The toggle itself succeeded (ok=True) but the re-list surfaces the bad state.
    assert result.ok is True
    assert result.message.splitlines() == [
        "extension 'legacy' enabled (applied live)",
        "warning: 'legacy' is failed",
        "  error: import failed: boom",
    ]


def test_extensions_reload_prints_summary_failures_and_hint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        assert json == {"method": "extensions.reload", "params": {}}
        return httpx.Response(
            200, json={"ok": True, "result": {"extensions": _extensions_payload()}}
        )

    monkeypatch.setattr(extensions_management.httpx, "post", fake_post)

    result = extensions_management.extensions_reload(instance)

    assert result.ok is True
    assert result.message.splitlines() == [
        "extensions reloaded: 1 loaded, 1 failed, 1 disabled, 0 overridden",
        "  broken failed: import failed: boom",
        "run 'vbot extensions list' for details",
    ]


def test_extensions_reload_clean_has_no_failure_lines(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)
    clean = [ext for ext in _extensions_payload() if ext["status"] == "loaded"]

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": {"extensions": clean}})

    monkeypatch.setattr(extensions_management.httpx, "post", fake_post)

    result = extensions_management.extensions_reload(instance)

    assert result.ok is True
    assert result.message == "extensions reloaded: 1 loaded, 0 failed, 0 disabled, 0 overridden"


def test_run_dispatches_extensions_reload(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    instance = make_instance(tmp_path)
    calls: list[ServerInstance] = []

    def fake_resolve(*, host: str, port: int | None, data_dir: str | None) -> ServerInstance:
        return instance

    def fake_reload(resolved_instance: ServerInstance) -> CommandResult:
        calls.append(resolved_instance)
        return CommandResult(ok=True, message="extensions reloaded: 2 loaded", instance=instance)

    exit_code = cli_main.run(
        ["extensions", "reload"],
        resolve=fake_resolve,
        reload_extensions_fn=fake_reload,
    )

    assert exit_code == 0
    assert calls == [instance]
    assert capsys.readouterr().out.splitlines() == ["extensions reloaded: 2 loaded"]


def test_run_extensions_reload_extra_arg_is_usage_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    instance = make_instance(tmp_path)
    calls: list[ServerInstance] = []

    def fake_resolve(*, host: str, port: int | None, data_dir: str | None) -> ServerInstance:
        return instance

    def fake_reload(resolved_instance: ServerInstance) -> CommandResult:
        calls.append(resolved_instance)
        return CommandResult(ok=True, message="unexpected", instance=instance)

    exit_code = cli_main.run(
        ["extensions", "reload", "extra"],
        resolve=fake_resolve,
        reload_extensions_fn=fake_reload,
    )

    assert exit_code == 1
    # The usage error short-circuits before the reload RPC runs.
    assert calls == []
    assert "extensions reload takes no arguments" in capsys.readouterr().out


def test_extensions_disable_unknown_name_suggests_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        assert json["method"] == "extensions.list"
        return httpx.Response(
            200, json={"ok": True, "result": {"extensions": _extensions_payload()}}
        )

    monkeypatch.setattr(extensions_management.httpx, "post", fake_post)

    result = extensions_management.extensions_disable(instance, "guard_bas")

    assert result.ok is False
    assert "extension 'guard_bas' not found" in result.message
    assert "did you mean: guard_bash" in result.message


def test_extensions_disable_already_disabled_is_noop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)
    posted: list[str] = []

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        posted.append(json["method"])
        return httpx.Response(
            200, json={"ok": True, "result": {"extensions": _extensions_payload()}}
        )

    monkeypatch.setattr(extensions_management.httpx, "post", fake_post)

    result = extensions_management.extensions_disable(instance, "legacy")

    assert result.ok is True
    assert result.message == "extension 'legacy' is already disabled (no change)"
    assert posted == ["extensions.list"]


def test_run_dispatches_extensions_disable(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    instance = make_instance(tmp_path)
    calls: list[tuple[ServerInstance, str]] = []

    def fake_resolve(*, host: str, port: int | None, data_dir: str | None) -> ServerInstance:
        return instance

    def fake_disable(resolved_instance: ServerInstance, name: str) -> CommandResult:
        calls.append((resolved_instance, name))
        return CommandResult(ok=True, message="extension 'guard_bash' disabled", instance=instance)

    exit_code = cli_main.run(
        ["extensions", "disable", "guard_bash"],
        resolve=fake_resolve,
        disable_extension_fn=fake_disable,
    )

    assert exit_code == 0
    assert calls == [(instance, "guard_bash")]
    assert capsys.readouterr().out.splitlines() == ["extension 'guard_bash' disabled"]


def _schema_payload() -> list[dict[str, Any]]:
    """One loaded, schema'd extension (Home-Assistant-shaped) plus a schema-less one."""
    return [
        {
            "name": "homeassistant",
            "status": "loaded",
            "disabled": False,
            "version": "1.0.0",
            "description": "Control a Home Assistant instance.",
            "error": None,
            "config": {"url": "http://homeassistant.local:8123"},
            "capability_errors": [],
            "ready_state": "waiting",
            "settings_schema": [
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
            ],
            "capabilities": {
                "hooks": {},
                "tools": [{"name": "ha_get_state", "ready": False}],
                "recall_backends": [],
                "startup": False,
                "shutdown": False,
            },
        }
    ]


def _post_returning(
    payload: list[dict[str, Any]], captured: list[dict[str, Any]], result_by_method: dict[str, Any]
) -> Any:
    """Build a fake httpx.post that serves extensions.list and captures writes."""

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        captured.append(json)
        if json["method"] == "extensions.list":
            return httpx.Response(200, json={"ok": True, "result": {"extensions": payload}})
        result = result_by_method.get(json["method"], {})
        return httpx.Response(200, json={"ok": True, "result": result})

    return fake_post


def test_extensions_show_renders_schema_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)
    monkeypatch.setattr(
        extensions_management.httpx, "post", _post_returning(_schema_payload(), [], {})
    )

    result = extensions_management.extensions_show(instance, "homeassistant")

    assert result.ok is True
    lines = result.message.splitlines()
    assert lines[0] == "homeassistant  loaded"
    assert "settings:" in lines
    assert '  url (text): "http://homeassistant.local:8123"   Server URL' in lines
    # A secret shows only its set-state, never a value.
    assert "  token (secret): not set   Access token" in lines
    assert "set with: vbot extensions homeassistant set <field> <value>" in lines


def test_extensions_show_unknown_name_suggests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)
    monkeypatch.setattr(
        extensions_management.httpx, "post", _post_returning(_schema_payload(), [], {})
    )

    result = extensions_management.extensions_show(instance, "homeassistan")

    assert result.ok is False
    assert "not found" in result.message
    assert "did you mean: homeassistant" in result.message


def test_extensions_set_secret_routes_to_set_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)
    captured: list[dict[str, Any]] = []
    monkeypatch.setattr(
        extensions_management.httpx,
        "post",
        _post_returning(
            _schema_payload(),
            captured,
            {"extensions.set_secret": {"name": "homeassistant", "key": "token", "set": True}},
        ),
    )

    result = extensions_management.extensions_set(instance, "homeassistant", "token", "secret-xyz")

    assert result.ok is True
    assert "secret 'token' set for 'homeassistant'" in result.message
    secret_calls = [call for call in captured if call["method"] == "extensions.set_secret"]
    # The field key is sent, never the env key — the server maps it to HASS_TOKEN.
    assert secret_calls == [
        {
            "method": "extensions.set_secret",
            "params": {"name": "homeassistant", "key": "token", "value": "secret-xyz"},
        }
    ]


def test_extensions_set_secret_empty_value_clears(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)
    monkeypatch.setattr(
        extensions_management.httpx,
        "post",
        _post_returning(
            _schema_payload(),
            [],
            {"extensions.set_secret": {"name": "homeassistant", "key": "token", "set": False}},
        ),
    )

    result = extensions_management.extensions_set(instance, "homeassistant", "token", "")

    assert result.ok is True
    assert result.message == "secret 'token' cleared for 'homeassistant'"


def test_extensions_set_text_writes_merged_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)
    captured: list[dict[str, Any]] = []
    monkeypatch.setattr(
        extensions_management.httpx,
        "post",
        _post_returning(_schema_payload(), captured, {"settings.update": {}}),
    )

    result = extensions_management.extensions_set(
        instance, "homeassistant", "url", "http://pi.local:8123"
    )

    assert result.ok is True
    assert "set 'homeassistant.url' = \"http://pi.local:8123\" (applied live)" in result.message
    update = next(call for call in captured if call["method"] == "settings.update")
    assert update["params"] == {
        "extensions": {
            "disabled": [],
            "config": {"homeassistant": {"url": "http://pi.local:8123"}},
        }
    }


def test_extensions_set_toggle_and_number_coercion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)
    payload = _schema_payload()
    payload[0]["settings_schema"].extend(
        [
            {"key": "verbose", "type": "toggle", "label": "Verbose", "required": False},
            {"key": "timeout", "type": "number", "label": "Timeout", "required": False},
        ]
    )
    captured: list[dict[str, Any]] = []
    monkeypatch.setattr(
        extensions_management.httpx,
        "post",
        _post_returning(payload, captured, {"settings.update": {}}),
    )

    toggle = extensions_management.extensions_set(instance, "homeassistant", "verbose", "true")
    number = extensions_management.extensions_set(instance, "homeassistant", "timeout", "30")

    assert toggle.ok is True and number.ok is True
    updates = [
        call["params"]["extensions"]["config"]
        for call in captured
        if call["method"] == "settings.update"
    ]
    assert updates[0]["homeassistant"]["verbose"] is True
    assert updates[1]["homeassistant"]["timeout"] == 30


def test_extensions_set_number_invalid_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)
    payload = _schema_payload()
    payload[0]["settings_schema"].append(
        {"key": "timeout", "type": "number", "label": "Timeout", "required": False}
    )
    captured: list[dict[str, Any]] = []
    monkeypatch.setattr(extensions_management.httpx, "post", _post_returning(payload, captured, {}))

    result = extensions_management.extensions_set(instance, "homeassistant", "timeout", "soon")

    assert result.ok is False
    assert "not a number" in result.message
    # Nothing is written when coercion fails.
    assert [call["method"] for call in captured] == ["extensions.list"]


def test_extensions_set_unknown_field_lists_available(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)
    monkeypatch.setattr(
        extensions_management.httpx, "post", _post_returning(_schema_payload(), [], {})
    )

    result = extensions_management.extensions_set(instance, "homeassistant", "hostname", "x")

    assert result.ok is False
    assert "has no setting 'hostname'" in result.message
    assert "available settings: url, token" in result.message


def test_extensions_set_unknown_extension_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)
    monkeypatch.setattr(
        extensions_management.httpx, "post", _post_returning(_schema_payload(), [], {})
    )

    result = extensions_management.extensions_set(instance, "nope", "url", "x")

    assert result.ok is False
    assert "extension 'nope' not found" in result.message


def test_run_dispatches_extensions_show(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    instance = make_instance(tmp_path)
    calls: list[tuple[ServerInstance, str]] = []

    def fake_resolve(*, host: str, port: int | None, data_dir: str | None) -> ServerInstance:
        return instance

    def fake_show(resolved_instance: ServerInstance, name: str) -> CommandResult:
        calls.append((resolved_instance, name))
        return CommandResult(ok=True, message="homeassistant  loaded", instance=instance)

    exit_code = cli_main.run(
        ["extensions", "homeassistant"],
        resolve=fake_resolve,
        show_extension_fn=fake_show,
    )

    assert exit_code == 0
    assert calls == [(instance, "homeassistant")]


def test_run_dispatches_extensions_set(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    instance = make_instance(tmp_path)
    calls: list[tuple[Any, ...]] = []

    def fake_resolve(*, host: str, port: int | None, data_dir: str | None) -> ServerInstance:
        return instance

    def fake_set(
        resolved_instance: ServerInstance, name: str, field: str, value: str
    ) -> CommandResult:
        calls.append((resolved_instance, name, field, value))
        return CommandResult(ok=True, message="ok", instance=instance)

    exit_code = cli_main.run(
        ["extensions", "homeassistant", "set", "url", "http://pi.local:8123"],
        resolve=fake_resolve,
        set_extension_fn=fake_set,
    )

    assert exit_code == 0
    assert calls == [(instance, "homeassistant", "url", "http://pi.local:8123")]


def test_run_extensions_set_reads_value_from_stdin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import io

    instance = make_instance(tmp_path)
    calls: list[tuple[Any, ...]] = []

    def fake_resolve(*, host: str, port: int | None, data_dir: str | None) -> ServerInstance:
        return instance

    def fake_set(
        resolved_instance: ServerInstance, name: str, field: str, value: str
    ) -> CommandResult:
        calls.append((name, field, value))
        return CommandResult(ok=True, message="ok", instance=instance)

    monkeypatch.setattr(cli_main.sys, "stdin", io.StringIO("token-from-stdin\n"))

    exit_code = cli_main.run(
        ["extensions", "homeassistant", "set", "token", "--stdin"],
        resolve=fake_resolve,
        set_extension_fn=fake_set,
    )

    assert exit_code == 0
    assert calls == [("homeassistant", "token", "token-from-stdin")]


def test_run_extensions_unknown_subcommand_is_usage_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    instance = make_instance(tmp_path)

    def fake_resolve(*, host: str, port: int | None, data_dir: str | None) -> ServerInstance:
        return instance

    exit_code = cli_main.run(["extensions", "homeassistant", "bogus"], resolve=fake_resolve)

    assert exit_code == 1
    assert "unknown command 'extensions homeassistant bogus'" in capsys.readouterr().out
