"""Persisted MCP faults must not disable unrelated connections or erase data."""

import json
import logging

import pytest

from core.extensions.extensions import ExtensionAPI, ExtensionDeclarations
from core.tools.tools import ToolRegistry
from resources.extensions.mcp.client import ConnectionRunner
from resources.extensions.mcp.config import (
    ConnectionStore,
    validate_connection,
    validate_connections_file,
)
from resources.extensions.mcp.extension import MCPService
from tests.resources.extensions.mcp_helpers import host as host


def connection(identifier="example", **fields):
    return {"id": identifier, "transport": "stdio", "command": "unused", **fields}


def document(*connections, **fields):
    return json.dumps({"format_version": 1, "connections": list(connections), **fields})


def saved_connections(store):
    return json.loads(store.path.read_text())["connections"]


@pytest.mark.parametrize("extra", [{"agents": []}, {"future_option": {"value": "secret-sentinel"}}])
def test_unknown_persisted_fields_do_not_block_or_rewrite_connection(tmp_path, extra):
    store = ConnectionStore(tmp_path)
    original = document(connection(**extra))
    store.path.write_text(original)
    assert store.load() == {"example": validate_connection(connection())}
    assert store.issues[0]["code"] == "unknown_fields"
    assert store.issues[0]["fields"] == list(extra)
    assert "secret-sentinel" not in json.dumps(store.issues)
    assert store.path.read_text() == original
    # Management still rejects misspelled input rather than pretending to save it.
    with pytest.raises(ValueError):
        validate_connection(connection(**extra))


@pytest.mark.parametrize(
    "bad",
    [
        None,
        [],
        {"id": []},
        connection("broken", timeout=-1),
        {"id": "broken", "transport": "stdio"},
    ],
)
def test_invalid_record_is_isolated_and_preserved_on_other_changes(tmp_path, bad):
    store = ConnectionStore(tmp_path)
    store.path.write_text(document(bad, connection()))
    records = store.load()
    assert set(records) == {"example"}
    assert store.issues[0]["code"] == "invalid_connection"
    records["example"]["enabled"] = False
    store.save(records)
    saved = saved_connections(store)
    assert saved[0] == bad
    assert saved[1]["enabled"] is False
    store.save({})
    assert saved_connections(store) == [bad]


def test_duplicate_ids_are_isolated_without_picking_a_target(tmp_path):
    store = ConnectionStore(tmp_path)
    duplicates = [connection("duplicate"), connection("duplicate", command="different")]
    store.path.write_text(document(*duplicates, connection()))
    assert set(store.load()) == {"example"}
    assert [issue["code"] for issue in store.issues] == ["duplicate_id", "duplicate_id"]
    store.save({"example": connection(enabled=False)})
    assert saved_connections(store)[:2] == duplicates
    # An explicit replacement repairs the selected id and removes its ambiguity.
    store.save({"example": connection(), "duplicate": connection("duplicate", command="repaired")})
    assert store.load()["duplicate"]["command"] == "repaired"
    assert not store.issues


def test_unknown_fields_survive_save_of_known_fields(tmp_path):
    store = ConnectionStore(tmp_path)
    extra = {"future_option": {"opaque": [1, 2]}}
    store.path.write_text(document(connection(**extra), future_root={"kept": True}))
    store.load()
    assert store.issues[0] == {
        "code": "unknown_fields",
        "fields": ["future_root"],
        "message": "Unrecognized document fields ignored",
    }
    store.save({"example": connection(enabled=False)})
    saved_document = json.loads(store.path.read_text())
    saved = saved_document["connections"][0]
    assert saved["future_option"] == extra["future_option"]
    assert saved["enabled"] is False
    assert saved_document["format_version"] == 1
    assert saved_document["future_root"] == {"kept": True}


def test_save_creates_a_versioned_document(tmp_path):
    store = ConnectionStore(tmp_path)
    store.save({"example": connection()})
    assert json.loads(store.path.read_text()) == {
        "format_version": 1,
        "connections": [validate_connection(connection())],
    }


@pytest.mark.parametrize(
    ("original", "message"),
    [
        ("broken json", "Invalid JSON"),
        ("{}", "persistence Generation 1"),
        ("null", "Expected a JSON object"),
        (json.dumps([connection()]), "Expected a JSON object"),
        ('{"format_version": 2, "connections": []}', "written by a newer vBot"),
        ('{"format_version": 1, "connections": {}}', "must be an array"),
    ],
)
def test_unreadable_document_is_never_overwritten(tmp_path, original, message):
    store = ConnectionStore(tmp_path)
    store.path.write_text(original)
    with pytest.raises(ValueError, match=message):
        store.load()
    with pytest.raises(ValueError, match=message):
        store.save({"example": connection()})
    assert store.path.read_text() == original


def test_validate_connections_file_reports_every_connection(tmp_path):
    path = tmp_path / "connections.json"
    assert validate_connections_file(path).ok
    path.write_text(
        document(
            connection("example", future_option=1),
            connection("broken", timeout=-1),
            connection("twin"),
            connection("twin"),
            future_root=True,
        )
    )

    report = validate_connections_file(path)

    assert [(item.severity, item.path) for item in report.diagnostics] == [
        ("warning", "$.future_root"),
        ("warning", "$.connections[0].future_option"),
        ("error", "$.connections[1]"),
        ("error", "$.connections[2]"),
        ("error", "$.connections[3]"),
    ]


@pytest.mark.asyncio
async def test_start_publishes_healthy_tools_and_reports_individual_issues(
    host, monkeypatch, caplog
):
    # MCP keeps its state in its Extension state directory.
    store = ConnectionStore(host.data_dir / "extension-data" / "mcp")
    assert store.directory == host.state_dir
    store.path.write_text(
        document(
            connection("godot", agents=[]),
            connection("blender", future_option="secret-sentinel"),
            connection("broken", timeout=-1),
        )
    )
    started = []
    monkeypatch.setattr(ConnectionRunner, "start", lambda runner: started.append(runner.id))
    api = ExtensionAPI(
        "mcp", ExtensionDeclarations(), config={}, logger=logging.getLogger("test.mcp.config")
    )
    registry = ToolRegistry()
    api.operations.bind(registry)
    service = MCPService(api)
    try:
        await service.start(host)
        assert set(started) == {"godot", "blender"}
        assert {tool.name for tool in registry.list_tools()} == {"mcp_godot", "mcp_blender"}
        result = await service.manage("list", {})
        assert {item["id"] for item in result["connections"]} == {"godot", "blender"}
        assert {issue["code"] for issue in result["configuration_issues"]} == {
            "unknown_fields",
            "invalid_connection",
        }
        assert len(caplog.records) == 3
        assert "secret-sentinel" not in caplog.text
        await service.manage("disable", {"id": "godot"})
        assert ConnectionStore(host.state_dir).load()["godot"]["enabled"] is False
        assert not (host.data_dir / "mcp").exists()
    finally:
        await service.close()
