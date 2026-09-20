"""Explicit extraction opt-in persists consistently through all Settings APIs."""

import pytest

from core.fetch_config import WEB_FETCH_PROVIDERS, parse_web_fetch_settings
from core.settings import SettingsValidationError, parse_settings_update
from core.settings.paths import (
    apply_settings_patch,
    build_effective_settings,
    parse_patch_operations,
)
from core.storage.storage import StorageManager


def test_defaults_never_enable_paid_extraction(tmp_path):
    storage = StorageManager(tmp_path)
    assert storage.load_web_fetch_settings() == {"provider": "direct", "mode": "fallback"}
    assert build_effective_settings({})["web_fetch"] == storage.load_web_fetch_settings()


def test_path_settings_support_selection_and_reset():
    candidate, _ = apply_settings_patch(
        {},
        parse_patch_operations(
            [
                {"op": "set", "path": "web_fetch.provider", "value": "parallel"},
                {"op": "set", "path": "web_fetch.mode", "value": "prefer"},
            ]
        ),
    )
    assert build_effective_settings(candidate)["web_fetch"] == {
        "provider": "parallel",
        "mode": "prefer",
    }


@pytest.mark.parametrize("provider", WEB_FETCH_PROVIDERS)
def test_selection_roundtrips_and_sparse_update_preserves_mode(tmp_path, provider):
    storage = StorageManager(tmp_path)
    parsed = parse_settings_update({"web_fetch": {"provider": provider, "mode": "prefer"}})
    storage.update_settings_sections(parsed)
    assert storage.load_web_fetch_settings() == {"provider": provider, "mode": "prefer"}
    storage.update_settings_sections(parse_settings_update({"web_fetch": {"provider": "direct"}}))
    assert storage.load_web_fetch_settings() == {"provider": "direct", "mode": "prefer"}


@pytest.mark.parametrize(
    "value",
    [None, [], {"provider": "auto"}, {"mode": "always"}, {"api_key": "secret"}, {"provider": True}],
)
def test_invalid_or_secret_fields_cannot_enter_settings(value):
    with pytest.raises(SettingsValidationError):
        parse_settings_update({"web_fetch": value})
    with pytest.raises(ValueError):
        parse_web_fetch_settings(value)
