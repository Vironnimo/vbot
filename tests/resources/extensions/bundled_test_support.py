"""Load one shipped Extension from the real bundled root."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.extensions import ExtensionRegistry

_REPO_ROOT = Path(__file__).resolve().parents[3]

BUNDLED_EXTENSIONS_DIR = _REPO_ROOT / "resources" / "extensions"


def load_bundled(
    *names: str,
    config_provider: Callable[[str], dict[str, Any]] | None = None,
    credential_resolver: Callable[[str], str] | None = None,
) -> ExtensionRegistry:
    """Load the bundled Extensions *names* with every other bundled Extension disabled.

    The load goes through the real bundled root, so it also proves that the root ships
    a loadable Extension. Disabled Extensions are never imported, which keeps one load
    to the cost of the Extensions under test.
    """
    others = {
        entry.name
        for entry in BUNDLED_EXTENSIONS_DIR.iterdir()
        if entry.name not in names and (entry / "extension.json").is_file()
    }
    return ExtensionRegistry.load(
        _REPO_ROOT / "does-not-exist-data-extensions",
        bundled_dir=BUNDLED_EXTENSIONS_DIR,
        disabled=others,
        config_provider=config_provider,
        credential_resolver=credential_resolver,
    )
