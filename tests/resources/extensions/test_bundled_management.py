"""Bundled Extensions: management operations load from the bundled root and describe themselves."""

from __future__ import annotations

from resources.extensions.swarm._store_values import _validate_profile
from tests.resources.extensions.bundled_test_support import load_bundled


def test_management_operations_describe_each_action_and_validate_their_examples() -> None:
    registry = load_bundled("mcp", "swarm")

    mcp_names = {operation["name"] for operation in registry.management("mcp").describe()}
    assert {"save", "test", "invoke", "respond", "cancel-job"} <= mcp_names
    for extension in ("mcp", "swarm"):
        descriptions = registry.management(extension).describe()
        assert descriptions
        assert all(item["description"] != item["name"] for item in descriptions)
        assert len({item["description"] for item in descriptions}) == len(descriptions)
    profile_save = next(
        item for item in registry.management("swarm").describe() if item["name"] == "profiles.save"
    )
    for example in profile_save["parameters"]["properties"]["profile"]["examples"]:
        assert _validate_profile(example)["participants"]


def test_mcp_contributes_its_settings_view_with_only_the_services_it_uses() -> None:
    registry = load_bundled("mcp")
    [(identity, contribution)] = registry.contribution_declarations()
    assert identity.name == "mcp"
    assert contribution.spec["surface"] == "settings.sections"
    assert contribution.spec["when"] == {"settings_section": "extensions"}
    assert contribution.spec["kind"] == "view"
    page = registry.current_page(identity, contribution.spec["page"])
    assert page is not None
    declaration, entry = page
    assert declaration.navigation is False
    assert declaration.services == ("agent.list", "tool.list", "agent.update")
    assert entry.name == "page.html"
