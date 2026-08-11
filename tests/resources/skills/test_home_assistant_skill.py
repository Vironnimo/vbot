"""Tests for the bundled Home Assistant Skill and its WebSocket script."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import threading
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from websockets.sync.server import serve

from core.skills.requirements import environment_requirement_names
from core.skills.skills import SkillRegistry
from core.tools.skill import load_skill_content

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SKILLS_ROOT = PROJECT_ROOT / "resources" / "skills"
SKILL_ROOT = SKILLS_ROOT / "home-assistant"
SCRIPT_PATH = SKILL_ROOT / "scripts" / "ha_ws.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("home_assistant_skill_ha_ws", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError(f"Unable to load {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


HA_WS = _load_script()


class FakeClient:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = iter(responses)
        self.requests: list[dict[str, Any]] = []

    def request(self, payload: dict[str, Any]) -> Any:
        self.requests.append(payload)
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


def _config(entity_id: str) -> dict[str, Any]:
    return {
        "views": [
            {
                "title": "Home",
                "cards": [{"type": "tile", "entity": entity_id}],
            }
        ]
    }


def test_builtin_skill_requires_hass_token_and_exposes_script() -> None:
    missing_registry = SkillRegistry.load(SKILLS_ROOT, environment={})
    missing = missing_registry.availability_for("home-assistant", ["*"])
    assert missing.state == "unavailable"
    assert missing.missing

    registry = SkillRegistry.load(SKILLS_ROOT, environment={"HASS_TOKEN": "available"})
    skill = registry.get("home-assistant")
    assert skill is not None
    assert environment_requirement_names(skill.requirements) == ("HASS_TOKEN",)
    assert SCRIPT_PATH.is_file()
    assert (SKILL_ROOT / "references" / "dashboard-design.md").is_file()

    loaded = load_skill_content(
        "home-assistant",
        SKILL_ROOT / "SKILL.md",
        env_keys=("HASS_TOKEN",),
    )
    activated = loaded["activation_content"]
    assert activated.startswith('<skill_content name="home-assistant">')
    assert "<environment_access>" in activated
    assert "- `HASS_TOKEN`" in activated
    assert SCRIPT_PATH.as_posix() in activated
    assert "- references/dashboard-design.md" in activated
    assert "<skill_content" not in loaded["content"]
    assert loaded["resource_files"]["files"] == [
        SCRIPT_PATH.as_posix(),
        "references/dashboard-design.md",
    ]


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        ("http://homeassistant.local:8123", "ws://homeassistant.local:8123/api/websocket"),
        ("https://ha.example.test/", "wss://ha.example.test/api/websocket"),
        ("https://example.test/home", "wss://example.test/home/api/websocket"),
    ],
)
def test_build_websocket_url(base_url: str, expected: str) -> None:
    assert HA_WS.build_websocket_url(base_url) == expected


def test_websocket_client_authenticates_and_returns_matching_result() -> None:
    captured: dict[str, Any] = {}

    def handler(websocket: Any) -> None:
        websocket.send(json.dumps({"type": "auth_required", "ha_version": "2026.8.0"}))
        captured["auth"] = json.loads(websocket.recv())
        websocket.send(json.dumps({"type": "auth_ok", "ha_version": "2026.8.0"}))
        command = json.loads(websocket.recv())
        captured["command"] = command
        websocket.send(
            json.dumps(
                {
                    "id": command["id"],
                    "type": "result",
                    "success": True,
                    "result": {"resource_mode": "storage"},
                }
            )
        )

    with serve(handler, "127.0.0.1", 0) as server:
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            port = server.socket.getsockname()[1]
            client = HA_WS.HomeAssistantWebSocket(
                f"http://127.0.0.1:{port}",
                "test-token",
                2,
            )
            result = client.request({"type": "lovelace/info"})
        finally:
            server.shutdown()
            thread.join(timeout=2)

    assert result == {"resource_mode": "storage"}
    assert captured["auth"] == {"type": "auth", "access_token": "test-token"}
    assert captured["command"] == {"id": 1, "type": "lovelace/info"}


def test_validate_dashboard_config_rejects_missing_card_type() -> None:
    with pytest.raises(HA_WS.HomeAssistantScriptError, match=r"cards\[0\]\.type"):
        HA_WS.validate_dashboard_config(
            {"views": [{"title": "Home", "cards": [{"entity": "light.kitchen"}]}]}
        )


def test_dashboard_validate_cli_runs_without_a_connection(tmp_path: Path) -> None:
    config_path = tmp_path / "dashboard.json"
    config_path.write_text(json.dumps(_config("light.kitchen")), encoding="utf-8")

    completed = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "dashboard-validate", str(config_path)],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0
    assert json.loads(completed.stdout)["action"] == "dashboard_validate"
    assert completed.stderr == ""


def test_apply_dashboard_dry_run_has_no_mutation_or_backup(tmp_path: Path) -> None:
    current = _config("light.before")
    target = _config("light.after")
    client = FakeClient([current])
    backup = tmp_path / "backup.json"

    result = HA_WS.apply_dashboard(
        client,
        target,
        url_path="kitchen-wall",
        expected_sha256=HA_WS.dashboard_sha256(current),
        backup_path=backup,
        apply=False,
    )

    assert result["applied"] is False
    assert result["changed"] is True
    assert [request["type"] for request in client.requests] == ["lovelace/config"]
    assert not backup.exists()


def test_apply_dashboard_backs_up_and_verifies_before_reporting_success(tmp_path: Path) -> None:
    current = _config("light.before")
    target = _config("light.after")
    client = FakeClient([current, None, target])
    backup = tmp_path / "backup.json"

    result = HA_WS.apply_dashboard(
        client,
        target,
        url_path="kitchen-wall",
        expected_sha256=HA_WS.dashboard_sha256(current),
        backup_path=backup,
        apply=True,
    )

    assert result["applied"] is True
    assert result["verified_sha256"] == HA_WS.dashboard_sha256(target)
    assert json.loads(backup.read_text(encoding="utf-8")) == current
    assert [request["type"] for request in client.requests] == [
        "lovelace/config",
        "lovelace/config/save",
        "lovelace/config",
    ]


def test_apply_dashboard_rejects_a_race_before_writing(tmp_path: Path) -> None:
    current = _config("light.changed_elsewhere")
    client = FakeClient([current])
    backup = tmp_path / "backup.json"

    with pytest.raises(HA_WS.HomeAssistantScriptError):
        HA_WS.apply_dashboard(
            client,
            _config("light.proposed"),
            url_path=None,
            expected_sha256=HA_WS.dashboard_sha256(_config("light.stale_export")),
            backup_path=backup,
            apply=True,
        )

    assert [request["type"] for request in client.requests] == ["lovelace/config"]
    assert not backup.exists()


def test_create_dashboard_rolls_back_metadata_when_initial_save_fails() -> None:
    failure = HA_WS.HomeAssistantScriptError("save rejected")
    client = FakeClient(
        [
            [],
            {"id": "wall_dashboard", "url_path": "kitchen-wall"},
            failure,
            failure,
            None,
        ]
    )

    with pytest.raises(HA_WS.HomeAssistantScriptError):
        HA_WS.create_dashboard(
            client,
            {"title": "Kitchen", "url_path": "kitchen-wall"},
            _config("light.kitchen"),
            apply=True,
        )

    assert [request["type"] for request in client.requests] == [
        "lovelace/dashboards/list",
        "lovelace/dashboards/create",
        "lovelace/config/save",
        "lovelace/config",
        "lovelace/dashboards/delete",
    ]
