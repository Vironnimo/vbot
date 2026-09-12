"""Web search: contract behavior."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from core.tools.tools import ToolRegistry
from core.tools.web_search import (
    WEB_SEARCH_TOOL_DESCRIPTION,
    WEB_SEARCH_TOOL_NAME,
    WEB_SEARCH_TOOL_PARAMETERS,
    _resolve_web_search_settings,
    register_web_search_tool,
    web_search_handler,
)
from tests.core.tools.web_search_helpers import (
    _fake_credential_resolver,
    assert_failure_envelope,
    make_context,
)


def _collect_schema_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    result: list[str] = []
    if isinstance(value, dict):
        for key, nested_value in value.items():
            result.append(str(key))
            result.extend(_collect_schema_strings(nested_value))
    elif isinstance(value, list):
        for item in value:
            result.extend(_collect_schema_strings(item))
    return result


def test_register_web_search_tool_schema() -> None:
    registry = ToolRegistry()

    register_web_search_tool(registry, lambda key: "")

    tool = registry.get("web_search")
    assert tool.name == WEB_SEARCH_TOOL_NAME == "web_search"
    assert tool.description == WEB_SEARCH_TOOL_DESCRIPTION
    assert tool.parameters == WEB_SEARCH_TOOL_PARAMETERS

    definitions = registry.provider_definitions(["web_search"])
    assert len(definitions) == 1
    definition = definitions[0]
    assert definition["name"] == "web_search"
    assert definition["description"] == WEB_SEARCH_TOOL_DESCRIPTION

    parameters = definition["parameters"]
    assert parameters["type"] == "object"
    assert parameters["required"] == ["query"]
    assert "additionalProperties" not in parameters
    assert tool.open_input_schema is True
    display = registry.display_for_call(
        "web_search",
        {
            "description": "Find the current release notes",
            "query": "vBot release notes",
        },
        result={
            "ok": True,
            "error": None,
            "data": {"results": [{"url": "https://example.com/vbot"}]},
            "artifacts": [],
        },
    )
    assert display["primary"][0]["value"] == "Find the current release notes"
    assert display["facts"] == [{"kind": "count", "value": 1, "unit": "results", "at_least": False}]

    properties = parameters["properties"]
    assert "provider" not in properties
    assert set(properties) == {
        "query",
        "domains",
        "count",
        "page",
        "recency",
    }
    domains_schema = properties["domains"]
    assert domains_schema["items"] == {
        "type": "string",
        "minLength": 1,
        "maxLength": 253,
    }
    assert domains_schema["minItems"] == 1
    assert domains_schema["maxItems"] == 10
    assert domains_schema["uniqueItems"] is True
    count_schema = properties["count"]
    assert count_schema["minimum"] == 1
    assert count_schema["maximum"] == 20
    page_schema = properties["page"]
    assert page_schema["minimum"] == 1
    assert page_schema["maximum"] == 10
    assert page_schema["default"] == 1
    assert properties["recency"]["enum"] == ["day", "month", "year"]
    assert isinstance(properties["recency"]["description"], str)
    assert properties["recency"]["description"]
    assert all("default" not in schema for name, schema in properties.items() if name != "page")


@pytest.mark.asyncio
async def test_web_search_handler_missing_api_key(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot"},
        lambda key: "",
    )

    error = assert_failure_envelope(result, "missing_api_key")
    assert "BRAVE_API_KEY" in error["message"]


@pytest.mark.asyncio
async def test_web_search_handler_empty_query(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = await web_search_handler(
        make_context(workspace),
        {"query": "   "},
        _fake_credential_resolver,
    )

    assert_failure_envelope(result, "validation_error")


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [0, 21])
async def test_web_search_handler_count_out_of_range(tmp_path: Path, count: int) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot", "count": count},
        _fake_credential_resolver,
    )

    assert_failure_envelope(result, "validation_error")


@pytest.mark.asyncio
@pytest.mark.parametrize("retired_field", ["freshness", "date_after", "date_before"])
async def test_web_search_handler_rejects_retired_time_filters(
    tmp_path: Path,
    retired_field: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot", retired_field: "day"},
        _fake_credential_resolver,
    )

    assert_failure_envelope(result, "validation_error")


@pytest.mark.asyncio
async def test_web_search_handler_invalid_recency(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot", "recency": "week"},
        _fake_credential_resolver,
    )

    assert_failure_envelope(result, "validation_error")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "domains",
    [
        "",
        "example.com",
        [],
        {"domain": "example.com"},
        ["https://example.com"],
        ["example.com/docs"],
        ["*.example.com"],
        ["bad domain.example"],
        ["-example.com"],
        [1],
        [f"domain{index}.example" for index in range(11)],
    ],
)
async def test_web_search_handler_rejects_invalid_domains(
    tmp_path: Path,
    domains: Any,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot", "domains": domains},
        _fake_credential_resolver,
    )

    error = assert_failure_envelope(result, "validation_error")
    assert "domains" in error["message"]


def test_api_key_not_in_schema() -> None:
    all_strings = _collect_schema_strings(WEB_SEARCH_TOOL_PARAMETERS)
    for credential_key in (
        "BRAVE_API_KEY",
        "TAVILY_API_KEY",
        "EXA_API_KEY",
        "SERPER_API_KEY",
        "FIRECRAWL_API_KEY",
    ):
        assert all(credential_key not in value for value in all_strings)


def test_resolve_web_search_settings_logs_unexpected_resolver_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def boom() -> dict[str, Any]:
        raise RuntimeError("settings backend exploded")

    with caplog.at_level(logging.ERROR, logger="vbot.tools.web_search"):
        settings, error = _resolve_web_search_settings(boom)

    assert settings is None
    assert error is not None and "could not be loaded" in error
    crash_records = [
        record
        for record in caplog.records
        if record.levelno == logging.ERROR
        and "settings resolver crashed unexpectedly" in record.getMessage()
    ]
    assert crash_records, "expected an error log for the crashing settings resolver"
    assert crash_records[0].exc_info is not None
