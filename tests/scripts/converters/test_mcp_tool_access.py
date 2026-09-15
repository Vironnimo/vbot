"""Offline MCP conversion retains deliberate choices without broad opt-ins."""

import json
import socket
import sqlite3
from contextlib import closing

import pytest

from scripts.converters.mcp_tool_access import convert_mcp_access, convert_policy


@pytest.mark.parametrize(
    "policy,prior,expected",
    [
        ({"mode": "all"}, set(), {"mode": "all"}),
        ({"mode": "all"}, {"mcp_blender"}, {"mode": "all", "granted": ["mcp_blender"]}),
        ({"mode": "none"}, {"mcp_blender"}, {"mode": "none"}),
        (
            {"mode": "selected", "allowed": ["read", "mcp_blender"]},
            set(),
            {"mode": "selected", "allowed": ["read", "mcp_blender"], "granted": ["mcp_blender"]},
        ),
        (
            {"mode": "all", "denied": ["mcp_blender"]},
            {"mcp_blender"},
            {"mode": "all", "denied": ["mcp_blender"]},
        ),
    ],
)
def test_policy_conversion(policy, prior, expected):
    assert convert_policy(policy, prior) == expected
    assert convert_policy(expected, prior) == expected


def test_offline_conversion_updates_profiles_bindings_and_retains_backups(tmp_path, monkeypatch):
    connection = tmp_path / "mcp/connections.json"
    connection.parent.mkdir()
    connection.write_text(
        json.dumps(
            [{"id": "blender", "transport": "stdio", "command": "unused", "agents": ["alice"]}]
        )
    )
    agent = tmp_path / "agents/alice/agent.json"
    agent.parent.mkdir(parents=True)
    agent.write_text('{"id":"alice"}')
    profile = {
        "tool_access": {"mode": "selected", "allowed": ["mcp_blender"]},
        "instructions": "unchanged",
    }
    swarm = tmp_path / "extension-data/swarm/swarm.db"
    swarm.parent.mkdir(parents=True)
    with closing(sqlite3.connect(swarm)) as db, db:
        db.execute("CREATE TABLE profiles(id TEXT PRIMARY KEY, payload TEXT)")
        db.execute("CREATE TABLE swarms(id TEXT PRIMARY KEY, profile_snapshot TEXT)")
        db.execute("INSERT INTO profiles VALUES (?,?)", ("profile", json.dumps(profile)))
        db.execute("INSERT INTO swarms VALUES (?,?)", ("swarm", json.dumps(profile)))
    with closing(sqlite3.connect(tmp_path / "sessions.db")) as db, db:
        db.execute(
            "CREATE TABLE temporary_session_bindings"
            "(session_key INTEGER PRIMARY KEY, config_json TEXT)"
        )
        db.execute("INSERT INTO temporary_session_bindings VALUES (?,?)", (1, json.dumps(profile)))
    assert len(convert_mcp_access(tmp_path)) == 4
    assert "agents" in json.loads(connection.read_text())[0]
    with pytest.raises(ValueError):
        convert_mcp_access(tmp_path, apply=True)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        with pytest.raises(ValueError):
            convert_mcp_access(tmp_path, apply=True, port=port)

    def refused(*args, **kwargs):
        raise ConnectionRefusedError()

    monkeypatch.setattr(socket, "create_connection", refused)
    convert_mcp_access(tmp_path, apply=True, port=port)
    assert "agents" not in json.loads(connection.read_text())[0]
    assert json.loads(agent.read_text())["tool_access"]["granted"] == ["mcp_blender"]
    with closing(sqlite3.connect(tmp_path / "sessions.db")) as db:
        value = json.loads(
            db.execute("SELECT config_json FROM temporary_session_bindings").fetchone()[0]
        )
    assert value["tool_access"]["granted"] == ["mcp_blender"]
    assert value["instructions"] == "unchanged"
    assert list((tmp_path / "backups").glob("*/database-rows.json"))
    assert convert_mcp_access(tmp_path, apply=True, port=port) == []
