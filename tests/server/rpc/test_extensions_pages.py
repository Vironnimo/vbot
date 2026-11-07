"""Extension page descriptors carry current, owner-bound attention state."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core.extensions import ExtensionRegistry
from server.rpc.extensions_methods import _extension_pages
from tests.core.extensions.extension_test_support import write_package


@pytest.mark.asyncio
async def test_page_descriptors_read_badges_on_every_reconnect_and_drop_retired_owners(
    tmp_path: Path,
) -> None:
    root = tmp_path / "extensions"
    package = write_package(
        root,
        "fixture",
        "def register(api):\n    api.register_page('board', 'Board', 'index.html')\n",
    )
    (package / "index.html").write_text("<!doctype html>", encoding="utf-8")
    registry = ExtensionRegistry.load(root)
    identity = registry.registration_identity("fixture")
    state = SimpleNamespace(
        runtime=SimpleNamespace(extensions=registry),
        file_delivery=SimpleNamespace(
            open_extension_page=Mock(return_value={"url": "/api/files/fixture"})
        ),
    )
    assert (await _extension_pages(state, {}))["pages"][0]["badge"] == 0
    registry.set_page_badge(identity, "board", 7)
    descriptor = (await _extension_pages(state, {}))["pages"][0]
    assert descriptor["badge"] == 7
    assert descriptor["route"] == "extension:fixture:board"
    assert "root" not in descriptor and "entry" not in descriptor
    registry.retire_registration()
    assert await _extension_pages(state, {}) == {"pages": []}
    state.runtime.extensions = ExtensionRegistry.load(root)
    assert (await _extension_pages(state, {}))["pages"][0]["badge"] == 0
