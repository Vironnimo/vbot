"""MCP: setup text from other clients becomes connections, and its secrets become credentials."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import replace
from typing import Any

import pytest

from resources.extensions.mcp.client import ConnectionRunner
from tests.resources.extensions.mcp.mcp_test_support import start_service

_TOKEN = "ghp_secretSentinel0123456789abcdefABCDEF"

_CLAUDE_DESKTOP = json.dumps(
    {
        "mcpServers": {
            "filesystem": {
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-filesystem", "C:\\Users\\me"],
            },
            "github": {
                "command": "docker",
                "args": ["run", "-i", "--rm", "-e", "GITHUB_PERSONAL_ACCESS_TOKEN", "github"],
                "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": _TOKEN, "LOG_LEVEL": "debug"},
            },
            "Brave Search": {
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-brave-search"],
                "env": {"BRAVE_API_KEY": "YOUR_API_KEY_HERE"},
                "alwaysAllow": ["search"],
            },
            "notion": {"url": "https://mcp.notion.com/mcp"},
            "legacy": {
                "serverUrl": "https://example.com/sse",
                "headers": {"Authorization": "Bearer ${env:EXAMPLE_TOKEN}"},
            },
        }
    }
)
_VS_CODE = """// .vscode/mcp.json
{
  "inputs": [
    {"type": "promptString", "id": "key", "description": "Perplexity API key", "password": true}
  ],
  "servers": {
    "perplexity": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "server-perplexity-ask"],
      "env": {"PERPLEXITY_API_KEY": "${input:key}"},
    },
    "git": {"command": "uvx", "args": ["mcp-server-git", "--repository", "${workspaceFolder}"]},
    "remote": {
      "type": "http", "url": "https://example.com/mcp", "headers": {"X-Key": "${env:KEY}"}
    },
    "team": {"url": "https://example.com/team/mcp", "headers": {"X-Team": "${env:TEAM}"}},
  },
}"""
_CODEX = """[mcp_servers.linear]
url = "https://mcp.linear.app/mcp"
bearer_token_env_var = "LINEAR_TOKEN"
"""


def _server(preview: dict[str, Any], name: str) -> dict[str, Any]:
    return next(item for item in preview["servers"] if item["name"] == name)


def _shape(item: dict[str, Any]) -> dict[str, Any]:
    connection = item["connection"]
    return {
        "id": item["id"],
        "transport": connection["transport"],
        "target": connection.get("command") or connection.get("url"),
        "args": connection.get("args", []),
        "environment": connection.get("environment", {}),
        "credentials": [
            (credential["target"], credential["name"], credential["state"])
            for credential in item["credentials"]
        ],
        "enabled": item["enabled"],
        "selected": item["selected"],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param(
            _CLAUDE_DESKTOP,
            {
                "filesystem": {
                    "id": "filesystem",
                    "transport": "stdio",
                    "target": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-filesystem", "C:\\Users\\me"],
                    "environment": {},
                    "credentials": [],
                    "enabled": True,
                    "selected": True,
                },
                "github": {
                    "id": "github",
                    "transport": "stdio",
                    "target": "docker",
                    "args": ["run", "-i", "--rm", "-e", "GITHUB_PERSONAL_ACCESS_TOKEN", "github"],
                    "environment": {"LOG_LEVEL": "debug"},
                    # A literal token is stored as a credential on import.
                    "credentials": [
                        (
                            "GITHUB_PERSONAL_ACCESS_TOKEN",
                            "MCP_GITHUB_PERSONAL_ACCESS_TOKEN",
                            "provided",
                        )
                    ],
                    "enabled": True,
                    "selected": True,
                },
                "Brave Search": {
                    "id": "brave_search",
                    "transport": "stdio",
                    "target": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-brave-search"],
                    "environment": {},
                    # A placeholder is never stored: the key is still to be set.
                    "credentials": [("BRAVE_API_KEY", "MCP_BRAVE_SEARCH_BRAVE_API_KEY", "missing")],
                    "enabled": False,
                    "selected": True,
                },
                "notion": {
                    "id": "notion",
                    "transport": "http",
                    "target": "https://mcp.notion.com/mcp",
                    "args": [],
                    "environment": {},
                    "credentials": [],
                    "enabled": True,
                    "selected": True,
                },
                "legacy": {
                    "id": "legacy",
                    "transport": "sse",
                    "target": "https://example.com/sse",
                    "args": [],
                    "environment": {},
                    # A header keeps its complete value, so a partial reference is missing.
                    "credentials": [("Authorization", "MCP_LEGACY_AUTHORIZATION", "missing")],
                    "enabled": False,
                    "selected": True,
                },
            },
            id="claude-desktop",
        ),
        pytest.param(
            _VS_CODE,
            {
                "perplexity": {
                    "id": "perplexity",
                    "transport": "stdio",
                    "target": "npx",
                    "args": ["-y", "server-perplexity-ask"],
                    "environment": {},
                    "credentials": [("PERPLEXITY_API_KEY", "MCP_PERPLEXITY_API_KEY", "missing")],
                    "enabled": False,
                    "selected": True,
                },
                "git": {
                    "id": "git",
                    "transport": "stdio",
                    "target": "uvx",
                    "args": ["mcp-server-git", "--repository", "${workspaceFolder}"],
                    "environment": {},
                    "credentials": [],
                    # An editor variable vBot cannot fill in leaves it disabled.
                    "enabled": False,
                    "selected": True,
                },
                "remote": {
                    "id": "remote",
                    "transport": "http",
                    "target": "https://example.com/mcp",
                    "args": [],
                    "environment": {},
                    # A whole ${env:NAME} keeps naming that variable.
                    "credentials": [("X-Key", "KEY", "set")],
                    "enabled": True,
                    "selected": True,
                },
                "team": {
                    "id": "team",
                    "transport": "http",
                    "target": "https://example.com/team/mcp",
                    "args": [],
                    "environment": {},
                    # A variable the vBot host does not have still needs a value.
                    "credentials": [("X-Team", "TEAM", "missing")],
                    "enabled": False,
                    "selected": True,
                },
            },
            id="vs-code-with-comments",
        ),
        pytest.param(
            'npx -y @modelcontextprotocol/server-filesystem C:\\data "C:\\Program Files\\x"\n'
            "https://mcp.deepwiki.com/sse",
            {
                "filesystem": {
                    "id": "filesystem",
                    "transport": "stdio",
                    "target": "npx",
                    "args": [
                        "-y",
                        "@modelcontextprotocol/server-filesystem",
                        "C:\\data",
                        "C:\\Program Files\\x",
                    ],
                    "environment": {},
                    "credentials": [],
                    "enabled": True,
                    "selected": True,
                },
                "deepwiki": {
                    "id": "deepwiki",
                    "transport": "sse",
                    "target": "https://mcp.deepwiki.com/sse",
                    "args": [],
                    "environment": {},
                    "credentials": [],
                    "enabled": True,
                    "selected": True,
                },
            },
            id="command-line-and-url",
        ),
        pytest.param(
            "claude mcp add --transport stdio airtable --scope user "
            "--env AIRTABLE_API_KEY=YOUR_KEY -- npx -y airtable-mcp-server --api-key " + _TOKEN,
            {
                "airtable": {
                    "id": "airtable",
                    "transport": "stdio",
                    "target": "npx",
                    "args": ["-y", "airtable-mcp-server", "--api-key", _TOKEN],
                    "environment": {},
                    "credentials": [("AIRTABLE_API_KEY", "MCP_AIRTABLE_API_KEY", "missing")],
                    "enabled": False,
                    # A secret in the arguments cannot become a credential: not chosen.
                    "selected": False,
                },
            },
            id="claude-mcp-add",
        ),
        pytest.param(
            _CODEX,
            {
                "linear": {
                    "id": "linear",
                    "transport": "http",
                    "target": "https://mcp.linear.app/mcp",
                    "args": [],
                    "environment": {},
                    "credentials": [("Authorization", "MCP_LINEAR_AUTHORIZATION", "missing")],
                    "enabled": False,
                    "selected": True,
                },
            },
            id="codex-toml",
        ),
    ],
)
async def test_preview_reads_each_client_format_without_saving(host, monkeypatch, source, expected):
    monkeypatch.setenv("KEY", "from-the-environment")
    monkeypatch.delenv("TEAM", raising=False)
    service, _registry = await start_service(
        replace(host, resolve_credential=lambda key: os.environ.get(key, ""))
    )
    try:
        preview = await service.manage("import", {"source": source})

        assert {item["name"]: _shape(item) for item in preview["servers"]} == expected
        assert service.store is not None and service.store.load() == {}
        # A credential's value never leaves the preview; only a secret that cannot
        # become a credential stays in the connection, which is then not chosen.
        for item in preview["servers"]:
            assert _TOKEN not in json.dumps(item["credentials"])
            assert _TOKEN not in json.dumps(item["connection"]) or not item["selected"]
            # It is saved enabled only when nothing is left to fill in.
            missing = any(credential["state"] == "missing" for credential in item["credentials"])
            assert item["enabled"] is not (missing or item["unresolved"] or item["disabled"])
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_import_stores_secrets_as_credentials_and_never_replaces_a_connection(
    host, monkeypatch, caplog
):
    started: list[str] = []
    monkeypatch.setattr(ConnectionRunner, "start", lambda runner: started.append(runner.id))
    service, _registry = await start_service(host)
    try:
        await service.manage(
            "save", {"connection": {"id": "notion", "transport": "stdio", "command": "kept"}}
        )
        started.clear()
        preview = await service.manage("import", {"source": _CLAUDE_DESKTOP})
        notion = _server(preview, "notion")
        assert (notion["conflict"], notion["selected"]) == (True, False)
        with pytest.raises(ValueError, match="notion: connection notion already exists"):
            await service.manage(
                "import", {"source": _CLAUDE_DESKTOP, "apply": True, "servers": ["notion"]}
            )

        with caplog.at_level(logging.INFO):
            result = await service.manage(
                "import",
                {
                    "source": _CLAUDE_DESKTOP,
                    "apply": True,
                    "servers": ["github", "Brave Search", "notion"],
                    "ids": {"notion": "notion_remote"},
                },
            )

        saved = service.store.load() if service.store is not None else {}
        assert [item["id"] for item in result["imported"]] == [
            "github",
            "brave_search",
            "notion_remote",
        ]
        assert saved["notion"]["command"] == "kept"
        assert saved["github"]["credential_environment"] == {
            "GITHUB_PERSONAL_ACCESS_TOKEN": "MCP_GITHUB_PERSONAL_ACCESS_TOKEN"
        }
        assert host.resolve_credential("MCP_GITHUB_PERSONAL_ACCESS_TOKEN") == _TOKEN
        # A connection whose credential is still missing is saved disabled.
        assert saved["brave_search"]["enabled"] is False
        assert _server_status(result, "brave_search")["missing_credentials"] == [
            "MCP_BRAVE_SEARCH_BRAVE_API_KEY"
        ]
        assert started == ["github", "notion_remote"]
        assert _TOKEN not in json.dumps(saved) + json.dumps(result) + caplog.text
        assert "MCP connections imported" in caplog.text
    finally:
        await service.close()


def _server_status(result: dict[str, Any], identifier: str) -> dict[str, Any]:
    return next(item for item in result["imported"] if item["id"] == identifier)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "message"),
    [
        pytest.param('{"mcpServers": {"a": ', "not valid JSON", id="invalid-json"),
        pytest.param('{"other": {"x": 1}}', "No MCP servers found", id="no-servers"),
        pytest.param('npx "unclosed', "unclosed quote", id="unclosed-quote"),
    ],
)
async def test_unreadable_setup_text_names_the_problem(host, source, message):
    service, _registry = await start_service(host)
    try:
        with pytest.raises(ValueError, match=message):
            await service.manage("import", {"source": source})
    finally:
        await service.close()
