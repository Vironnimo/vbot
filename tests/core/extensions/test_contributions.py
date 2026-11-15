"""Public presentation declarations and manifest-only Extension lifecycle."""

import json
from pathlib import Path
from typing import Any

import pytest

from core.extensions import ExtensionRegistry
from core.extensions.extensions import ExtensionAPI, ExtensionDeclarations


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
