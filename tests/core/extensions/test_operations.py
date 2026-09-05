"""Live Extension ownership and management validation regressions."""

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


def test_catalog_replacement_preserves_other_owners():
    registry = ToolRegistry()
    builtin = registry.register(**declaration("builtin"))
    operations = ExtensionOperations("example")
    operations.bind(registry)
    operations.replace_tools("server", [declaration("first")])

    operations.replace_tools("server", [declaration("second")])

    assert [tool.name for tool in registry.list_tools()] == ["builtin", "second"]
    assert registry.get("builtin") is builtin


def test_invalid_replacement_preserves_entire_previous_catalog():
    registry = ToolRegistry()
    operations = ExtensionOperations("example")
    operations.bind(registry)
    operations.replace_tools("server", [declaration("first")])
    previous = registry.get("first")

    with pytest.raises(ValueError):
        operations.replace_tools("server", [declaration("second"), declaration("bad-name")])

    assert registry.list_tools() == [previous]


def test_collision_cannot_replace_another_catalog():
    registry = ToolRegistry()
    operations = ExtensionOperations("example")
    operations.bind(registry)
    operations.replace_tools("first", [declaration("owned")])
    previous = registry.get("owned")

    with pytest.raises(ValueError):
        operations.replace_tools("second", [declaration("owned")])

    assert registry.get("owned") is previous


def test_retired_extension_cannot_republish():
    registry = ToolRegistry()
    operations = ExtensionOperations("example")
    operations.bind(registry)
    operations.replace_tools("server", [declaration("owned")])
    operations.retire()

    with pytest.raises(RuntimeError):
        operations.replace_tools("server", [declaration("late")])

    assert registry.list_tools() == []


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
