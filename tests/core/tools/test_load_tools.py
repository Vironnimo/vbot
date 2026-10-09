"""The load_tools Tool through production dispatch: outcomes, tolerated forms and refusals.

Chat hands the call the Tools of its Model request (input contracts) and the
On-demand Tools it can load. That a successful load becomes part of the prompt
epoch is Chat's contract (``tests/core/chat/test_chat_loop_tool_definitions.py``).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from core.providers.adapter import tool_result_text
from core.tools import ToolCall, ToolExecutor, ToolRegistry, tool_success
from core.tools.load_tools import register_load_tools_tool
from core.tools.on_demand import LOAD_TOOLS_TOOL_NAME
from tests.core.tools.tools_test_support import JsonObject, make_execution_config

_SEARCH_PARAMETERS = {
    "type": "object",
    "properties": {"query": {"type": "string", "description": "What to search for."}},
    "required": ["query"],
}
_FETCH_PARAMETERS = {
    "type": "object",
    "properties": {"url": {"type": "string", "description": "Page address."}},
    "required": ["url"],
}
_ON_DEMAND = ("web_search", "web_fetch")


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    register_load_tools_tool(registry)
    for name, description, parameters in (
        ("read", "Read a file.", {"type": "object", "properties": {}}),
        ("web_search", "Search the web.\nReturns titles and links.", _SEARCH_PARAMETERS),
        ("web_fetch", "Fetch one web page as text.", _FETCH_PARAMETERS),
    ):
        registry.register(
            name,
            description,
            parameters,
            lambda _c, _a: tool_success({}),
            open_input_schema=True,
        )
    return registry


async def _load(
    arguments: Any, *, loadable: tuple[str, ...] = _ON_DEMAND, allowed: tuple[str, ...] = ()
) -> JsonObject:
    """Call load_tools in a request that lists read and load_tools."""
    registry = _registry()
    names = ["read", LOAD_TOOLS_TOOL_NAME, *_ON_DEMAND]
    definitions = registry.provider_definitions(names, include_internal=True)
    config = make_execution_config(
        allowed_tools=list(allowed or names),
        input_contracts=registry.contracts_for_provider_definitions(definitions),
        loadable_tools={
            str(definition["name"]): definition
            for definition in definitions
            if definition["name"] in loadable
        },
    )
    [result] = await ToolExecutor(registry).execute_many(
        [ToolCall(id="call-1", name=LOAD_TOOLS_TOOL_NAME, arguments=arguments)], config
    )
    return result


def _text(result: JsonObject) -> str:
    return str(tool_result_text(json.dumps(result, ensure_ascii=False)))


_SEARCH_SECTION = (
    "Tool: web_search\n"
    "Description: Search the web.\nReturns titles and links.\n"
    'Parameters (JSON Schema): {"type":"object","properties":{"query":{"type":"string",'
    '"description":"What to search for."}},"required":["query"]}'
)
_FETCH_SECTION = (
    "Tool: web_fetch\n"
    "Description: Fetch one web page as text.\n"
    'Parameters (JSON Schema): {"type":"object","properties":{"url":{"type":"string",'
    '"description":"Page address."}},"required":["url"]}'
)
_CALL_LOADED = "Call the loaded Tools by name with normal Tool calls."


@pytest.mark.asyncio
async def test_loading_returns_each_definition_and_how_to_call_it() -> None:
    result = await _load({"names": ["web_search", "web_fetch"]})

    assert _text(result) == "\n\n".join(
        [
            "- web_search: loaded\n- web_fetch: loaded",
            _SEARCH_SECTION,
            _FETCH_SECTION,
            _CALL_LOADED,
        ]
    )


@pytest.mark.asyncio
async def test_each_name_reports_its_outcome_in_call_order() -> None:
    result = await _load({"names": ["read", "WebSearch", "nope", "web_search"]})

    assert result["ok"] is True
    assert _text(result) == "\n\n".join(
        [
            "- read: already available; call it directly\n"
            "- web_search: loaded\n"
            "- nope: not available to load\n"
            "Tools you can load: web_fetch.",
            _SEARCH_SECTION,
            _CALL_LOADED,
        ]
    )


@pytest.mark.asyncio
async def test_already_available_tools_succeed_with_nothing_to_load() -> None:
    result = await _load({"names": ["read", "web_fetch"]}, loadable=("web_search",))

    assert _text(result) == (
        "- read: already available; call it directly\n"
        "- web_fetch: already available; call it directly"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param({"names": "web_search, web_fetch"}, id="comma-separated-text"),
        pytest.param({"names": '["web_search", "web_fetch"]'}, id="json-array-text"),
        pytest.param({"query": "select:web_search,web_fetch"}, id="claude-code-select"),
        pytest.param({"tools": ["web_search", "web_fetch"]}, id="tools-alias"),
        pytest.param({"name": "web_search\nweb_fetch"}, id="name-alias-lines"),
        pytest.param({"tool_names": ["`web_search`", "'web_fetch'"]}, id="quoted-names"),
        pytest.param({"names": ["functions.web_search", "Web-Fetch"]}, id="called-name-rules"),
        pytest.param({"names": [{"name": "web_search"}, {"tool": "web_fetch"}]}, id="objects"),
        pytest.param(["web_search", "web_fetch"], id="bare-list"),
        pytest.param("web_search web_fetch", id="bare-text"),
    ],
)
async def test_tolerated_call_forms_load_the_named_tools(arguments: Any) -> None:
    result = await _load(arguments)

    assert _text(result).startswith("- web_search: loaded\n- web_fetch: loaded\n\n")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "loadable", "allowed", "message"),
    [
        pytest.param(
            {"names": ["nope", "missing"]},
            _ON_DEMAND,
            (),
            'Nothing was loaded: no Tool named "nope" or "missing" can be loaded. '
            "Tools you can load: web_fetch, web_search. Call load_tools again with "
            '"names" set to the Tools the task needs from that list, for example '
            '{"names": ["web_fetch"]}.',
            id="unknown-names",
        ),
        pytest.param(
            {"names": ["web_fetch"]},
            _ON_DEMAND,
            ("read", LOAD_TOOLS_TOOL_NAME, "web_search"),
            'Nothing was loaded: no Tool named "web_fetch" can be loaded. '
            "Tools you can load: web_search. Call load_tools again with "
            '"names" set to the Tools the task needs from that list, for example '
            '{"names": ["web_search"]}.',
            id="not-allowed-in-this-run",
        ),
        pytest.param(
            {},
            _ON_DEMAND,
            (),
            "Nothing was loaded: the call named no Tool. Tools you can load: web_fetch, "
            'web_search. Call load_tools again with "names" set to the Tools the task needs '
            'from that list, for example {"names": ["web_fetch"]}.',
            id="no-names",
        ),
        pytest.param(
            {"names": ["nope"]},
            (),
            (),
            'Nothing was loaded: no Tool named "nope" can be loaded. Every Tool you can use '
            "is already available; call it directly by name.",
            id="nothing-to-load",
        ),
    ],
)
async def test_a_call_that_loads_nothing_is_refused_with_the_tools_to_load(
    arguments: Any, loadable: tuple[str, ...], allowed: tuple[str, ...], message: str
) -> None:
    result = await _load(arguments, loadable=loadable, allowed=allowed)

    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_arguments"
    assert result["error"]["message"] == message
