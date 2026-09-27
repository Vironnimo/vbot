"""Live Extension Tool catalogs and management operations."""

import pytest

from core.extensions.operations import ExtensionOperations
from core.tools.tools import ToolRegistry, tool_success


def declaration(name, handler=None):
    return {
        "name": name,
        "description": "test-owned-description",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        "handler": handler or (lambda context, arguments: tool_success({})),
    }


def test_catalog_replacement_is_atomic_and_owned_per_catalog():
    registry = ToolRegistry()
    builtin = registry.register(**declaration("builtin"))
    operations = ExtensionOperations("example")
    operations.bind(registry)
    operations.replace_tools("server", [declaration("first")])

    operations.replace_tools("server", [declaration("second")])

    # Replacing one catalog leaves every other owner's Tools in place.
    assert [tool.name for tool in registry.list_tools()] == ["builtin", "second"]
    assert registry.get("builtin") is builtin
    previous = registry.get("second")
    # An invalid replacement keeps the entire previous catalog.
    with pytest.raises(ValueError):
        operations.replace_tools("server", [declaration("third"), declaration("bad-name")])
    # Another catalog cannot take over a name this one owns.
    with pytest.raises(ValueError):
        operations.replace_tools("other", [declaration("second")])
    assert registry.list_tools() == [builtin, previous]

    operations.retire()

    # A retired Extension cannot republish; its catalogs are gone.
    with pytest.raises(RuntimeError):
        operations.replace_tools("server", [declaration("late")])
    assert registry.list_tools() == [builtin]


def test_dynamic_hidden_catalog_tool_stays_registered_for_dispatch_ownership():
    registry = ToolRegistry()
    operations = ExtensionOperations("example")
    operations.bind(registry)
    private = declaration("private")
    private["catalog_visible"] = False

    operations.replace_tools("session", [private])

    assert operations.tool_names == ("private",)
    assert operations.catalog_visible_tool_names == ()
    assert registry.get("private").catalog_visible is False
    assert [tool.name for tool in registry.list_tools(include_catalog_hidden=False)] == []


@pytest.mark.asyncio
async def test_management_validation_does_not_disclose_secret_values():
    operations = ExtensionOperations("example")
    called = []

    async def handler(arguments):
        called.append(arguments)
        return {}

    operations.register(
        "secret",
        "test-owned-description",
        {"type": "object", "properties": {"value": {"const": "expected"}}},
        handler,
        secret=True,
    )

    with pytest.raises(ValueError) as error:
        await operations.invoke("secret", {"value": "must-never-leak"})

    assert "must-never-leak" not in str(error.value)
    assert called == []
