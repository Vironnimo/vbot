"""Public presentation declarations and manifest-only Extension lifecycle."""

import json
from pathlib import Path
from typing import Any

import pytest

from core.extensions import ExtensionRegistry
from core.extensions.extensions import API_VERSION, ExtensionAPI, ExtensionDeclarations
from tests.core.extensions.extension_test_support import write_package


def test_manifest_only_extension_publishes_owned_ui_without_importing_native_code(
    tmp_path: Path,
) -> None:
    package = tmp_path / "ui_only"
    package.mkdir()
    (package / "page.html").write_text("<!doctype html>", encoding="utf-8")
    (package / "native.py").write_text(
        "raise AssertionError('native code imported on server')", encoding="utf-8"
    )
    manifest = {
        "api_version": 30,
        "required_features": ["ui.contributions"],
        "desktop": {"entry": "native.py"},
        "pages": [
            {
                "page_id": "panel",
                "title": "Panel",
                "entry": "page.html",
                "navigation": False,
                "services": ["agent.list"],
            }
        ],
        "contributions": [
            {
                "id": "panel",
                "surface": "settings.sections",
                "title": "Panel",
                "kind": "view",
                "page": "panel",
            }
        ],
    }
    (package / "extension.json").write_text(json.dumps(manifest), encoding="utf-8")
    registry = ExtensionRegistry.load(tmp_path)
    identity = registry.registration_identity("ui_only")
    contribution = registry.current_contribution(identity, "panel")
    assert contribution is not None and contribution.spec["page"] == "panel"
    current_page = registry.current_page(identity, "panel")
    assert current_page is not None
    page, _entry = current_page
    assert page.navigation is False
    assert page.services == ("agent.list",)
    registry.retire_registration()
    assert registry.contribution_declarations() == []
    assert registry.current_contribution(identity, "panel") is None
    replacement = ExtensionRegistry.load(tmp_path)
    assert replacement.current_contribution(identity, "panel") is None
    assert ExtensionRegistry.load(tmp_path, disabled={"ui_only"}).contribution_declarations() == []
    manifest["required_features"] = ["future.unknown"]
    (package / "extension.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert ExtensionRegistry.load(tmp_path).records()[0].status == "failed"


@pytest.mark.parametrize(
    "fields",
    [
        {"surface": "unknown"},
        {"when": {"password": True}},
        {"order": float("nan")},
        {"action": {"command": "insert", "operation": "insert"}},
        {"icon": {"paths": ["<script>alert(1)</script>"]}},
        {"text": {"context": "credentials"}},
    ],
)
def test_contribution_declarations_reject_unsupported_shapes(fields: dict) -> None:
    api = ExtensionAPI("fixture", ExtensionDeclarations(), config={}, logger=None)
    spec = {"surface": "shell.status", "kind": "status", "title": "Status", **fields}
    with pytest.raises(ValueError):
        api.register_contribution("status", **spec)


def test_contribution_declarations_detach_inputs_and_keep_explicit_operation_scope() -> None:
    declarations = ExtensionDeclarations()
    api = ExtensionAPI("fixture", declarations, config={}, logger=None)
    source: dict[str, Any] = {"operation": "read", "params": {"session": {"context": "session_id"}}}
    api.register_contribution(
        "status",
        surface="shell.status",
        kind="status",
        title="Status",
        source=source,
        operations=["update"],
    )
    source["params"]["session"] = "changed"
    assert declarations.contributions[0].spec["source"]["params"]["session"] == {
        "context": "session_id"
    }
    assert declarations.contributions[0].spec["operations"] == ["read", "update"]
    with pytest.raises(ValueError):
        api.register_contribution(
            "status", surface="shell.status", kind="status", title="Duplicate"
        )
    api.register_page("page", "Page", "page.html", icon={"paths": ["M1 1L9 9"]})
    assert declarations.pages[0].icon == {"paths": ["M1 1L9 9"], "view_box": "0 0 16 16"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "async_registration", [False, True], ids=["sync-register", "async-register"]
)
@pytest.mark.parametrize("async_load", [False, True], ids=["load", "aload"])
async def test_final_declaration_references_are_diagnosed_without_losing_valid_capabilities(
    tmp_path: Path, async_registration: bool, async_load: bool
) -> None:
    register = "async def register(api):" if async_registration else "def register(api):"
    pause = "    await asyncio.sleep(0)\n" if async_registration else ""
    package = write_package(
        tmp_path,
        "references",
        "import asyncio\n"
        "async def read(arguments):\n"
        "    return {'text': 'ready'}\n"
        f"{register}\n"
        "    api.register_contribution('valid-source', surface='shell.status', kind='status', "
        "title='Ready', source={'operation': 'read'}, operations=['read'])\n"
        "    api.register_contribution('bad-operations', surface='shell.status', kind='status', "
        "title='Bad operations', source={'operation': 'missing-source'}, "
        "action={'operation': 'missing-action'}, operations=['missing-dynamic'])\n"
        "    api.register_service('read', 'query', {'type': 'object'}, read, "
        "resources=['changes'])\n"
        "    api.register_service('save', 'execute', {'type': 'object'}, read)\n"
        "    api.register_service('bad', 'query', {'type': 'object'}, read, "
        "resources=['changes', 'missing-resource'])\n"
        f"{pause}"
        "    api.operations.register('read', 'Read', {'type': 'object'}, read)\n"
        "    api.register_resource('changes')\n"
        "    api.register_page('panel', 'Panel', 'panel.html')\n"
        "    api.register_page('unbuilt', 'Unbuilt', 'unbuilt.html')\n",
        manifest={
            "api_version": API_VERSION,
            "contributions": [
                {
                    "id": contribution_id,
                    "surface": "settings.sections",
                    "kind": "view",
                    "title": contribution_id,
                    "page": page,
                }
                for contribution_id, page in [
                    ("valid-view", "panel"),
                    ("bad-view", "missing-page"),
                    ("unbuilt-view", "unbuilt"),
                ]
            ],
        },
    )
    (package / "panel.html").write_text("<!doctype html>", encoding="utf-8")
    registry = (
        await ExtensionRegistry.aload(tmp_path) if async_load else ExtensionRegistry.load(tmp_path)
    )
    try:
        record = registry.records()[0]
        assert record.status == "loaded"
        identity = registry.registration_identity("references")
        assert registry.current_contribution(identity, "valid-view") is not None
        assert registry.current_contribution(identity, "valid-source") is not None
        assert registry.current_contribution(identity, "bad-view") is None
        assert registry.current_contribution(identity, "bad-operations") is None
        diagnostics = "\n".join(record.capability_errors)
        assert "contribution 'bad-view' skipped" in diagnostics
        assert "missing-page" in diagnostics
        assert "contribution 'bad-operations' skipped" in diagnostics
        assert all(
            name in diagnostics for name in ("missing-action", "missing-source", "missing-dynamic")
        )
        assert "page 'unbuilt' skipped: entry is missing" in diagnostics
        assert "service 'bad' skipped: unknown resources: missing-resource" in diagnostics
        assert [service.name for service in record.declarations.services] == ["read", "save"]
        assert [resource.name for resource in record.declarations.resources] == ["changes"]
        # Assets may appear after a build without changing registration identity.
        assert registry.current_page(identity, "unbuilt") is not None
        assert registry.current_contribution(identity, "unbuilt-view") is not None
        (package / "unbuilt.html").write_text("<!doctype html>", encoding="utf-8")
        assert {page.page_id for _identity, page, _entry in registry.page_declarations()} == {
            "panel",
            "unbuilt",
        }
    finally:
        registry.retire_registration()
