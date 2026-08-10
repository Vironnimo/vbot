"""Tests for the public Settings path and atomic patch contract."""

from __future__ import annotations

import pytest

from core.settings.paths import (
    SettingsPathError,
    apply_settings_patch,
    build_effective_settings,
    catalog_payload,
    parse_patch_operations,
    parse_settings_path,
    resolve_setting,
    setting_details,
)


def test_parse_settings_path_preserves_dotted_and_quoted_segments() -> None:
    path = parse_settings_path('local_models.context_windows["ollama/qwen2.5:7b"]')

    assert path.values == ("local_models", "context_windows", "ollama/qwen2.5:7b")
    assert [segment.quoted for segment in path.segments] == [False, False, True]


@pytest.mark.parametrize(
    "path",
    [
        "",
        ".web_search.provider",
        "web_search..provider",
        "web_search[provider]",
        'web_search[""]',
        'web_search["provider"',
    ],
)
def test_parse_settings_path_rejects_invalid_syntax(path: str) -> None:
    with pytest.raises(SettingsPathError):
        parse_settings_path(path)


def test_dynamic_keys_require_bracket_quoting() -> None:
    with pytest.raises(SettingsPathError):
        resolve_setting("local_models.context_windows.ollama")


def test_atomic_patch_sets_multiple_nested_values() -> None:
    operations = parse_patch_operations(
        [
            {"op": "set", "path": "web_search.provider", "value": "searxng"},
            {
                "op": "set",
                "path": "web_search.searxng.base_url",
                "value": "https://search.example/",
            },
        ]
    )

    updated, changed = apply_settings_patch({}, operations)

    assert updated == {
        "web_search": {
            "provider": "searxng",
            "searxng": {"base_url": "https://search.example/"},
        }
    }
    assert changed == ("web_search.provider", "web_search.searxng.base_url")
    assert build_effective_settings(updated)["web_search"]["provider"] == "searxng"


def test_invalid_patch_leaves_input_unchanged() -> None:
    original = {"web_search": {"provider": "brave"}}

    with pytest.raises(SettingsPathError):
        parse_patch_operations([{"op": "set", "path": "web_search.provider", "value": "invalid"}])

    assert original == {"web_search": {"provider": "brave"}}


def test_patch_rejects_overlapping_paths() -> None:
    with pytest.raises(SettingsPathError):
        parse_patch_operations(
            [
                {"op": "set", "path": "compaction.trigger", "value": {}},
                {
                    "op": "set",
                    "path": "compaction.trigger.type",
                    "value": "context_ratio",
                },
            ]
        )


def test_unset_removes_override_and_restores_default() -> None:
    original = {"web_search": {"provider": "searxng"}}
    operations = parse_patch_operations([{"op": "unset", "path": "web_search.provider"}])

    updated, changed = apply_settings_patch(original, operations)

    assert updated == {}
    assert changed == ("web_search.provider",)
    assert build_effective_settings(updated)["web_search"]["provider"] == "brave"


def test_compaction_variant_can_be_changed_with_one_atomic_patch() -> None:
    operations = parse_patch_operations(
        [
            {"op": "set", "path": "compaction.trigger.type", "value": "input_tokens"},
            {"op": "set", "path": "compaction.trigger.tokens", "value": 24000},
        ]
    )

    updated, changed = apply_settings_patch({}, operations)

    assert updated == {"compaction": {"trigger": {"type": "input_tokens", "tokens": 24000}}}
    assert changed == ("compaction.trigger.type", "compaction.trigger.tokens")
    assert build_effective_settings(updated)["compaction"]["trigger"] == {
        "type": "input_tokens",
        "tokens": 24000,
    }


def test_setting_same_compaction_variant_preserves_sibling_overrides() -> None:
    original = {"compaction": {"trigger": {"type": "context_ratio", "threshold": 0.6}}}
    operations = parse_patch_operations(
        [{"op": "set", "path": "compaction.trigger.type", "value": "context_ratio"}]
    )

    updated, changed = apply_settings_patch(original, operations)

    assert updated == original
    assert changed == ()


def test_compaction_leaf_patch_infers_default_variant() -> None:
    operations = parse_patch_operations(
        [
            {
                "op": "set",
                "path": "compaction.strategy.summary_model",
                "value": "openai:gpt-5.1",
            }
        ]
    )

    updated, _changed = apply_settings_patch({}, operations)

    assert updated == {"compaction": {"strategy": {"summary_model": "openai:gpt-5.1"}}}
    assert build_effective_settings(updated)["compaction"]["strategy"] == {
        "type": "summary_tail",
        "tail_tokens": 15000,
        "summary_model": "openai:gpt-5.1",
    }


def test_unset_compaction_leaf_restores_variant_default() -> None:
    original = {"compaction": {"trigger": {"type": "context_ratio", "threshold": 0.6}}}
    operations = parse_patch_operations([{"op": "unset", "path": "compaction.trigger.threshold"}])

    updated, changed = apply_settings_patch(original, operations)

    assert updated == {"compaction": {"trigger": {"type": "context_ratio"}}}
    assert changed == ("compaction.trigger.threshold",)
    assert setting_details(updated, "compaction.trigger.threshold")["source"] == "default"
    assert build_effective_settings(updated)["compaction"]["trigger"]["threshold"] == 0.8


def test_server_port_patch_canonicalizes_and_unsets_all_raw_aliases() -> None:
    original = {"PORT": 8100, "server_port": 8200}

    updated, _changed = apply_settings_patch(
        original,
        parse_patch_operations([{"op": "set", "path": "server.port", "value": 8300}]),
    )
    cleared, changed = apply_settings_patch(
        updated,
        parse_patch_operations([{"op": "unset", "path": "server.port"}]),
    )

    assert updated == {"server_port": 8300}
    assert cleared == {}
    assert changed == ("server.port",)
    assert setting_details(cleared, "server.port")["value"] == 8420


def test_dynamic_model_key_with_period_round_trips() -> None:
    path = 'local_models.context_windows["ollama/qwen2.5:7b"]'
    operations = parse_patch_operations([{"op": "set", "path": path, "value": 32768}])

    updated, _changed = apply_settings_patch({}, operations)
    details = setting_details(updated, path)

    assert updated == {"local_models": {"context_windows": {"ollama/qwen2.5:7b": 32768}}}
    assert details["value"] == 32768
    assert details["configured"] is True
    assert details["source"] == "configured"


def test_public_document_hides_flat_storage_keys() -> None:
    effective = build_effective_settings(
        {
            "server_port": 9000,
            "skill_directories": ["~/skills"],
            "max_subagent_depth": 2,
        }
    )

    assert effective["server"] == {"port": 9000}
    assert effective["skills"] == {"directories": ["~/skills"]}
    assert effective["subagents"]["max_subagent_depth"] == 2
    assert "server_port" not in effective
    assert "skill_directories" not in effective


def test_speech_defaults_to_compatibility_profile_and_100_mib_uploads() -> None:
    speech = build_effective_settings({})["speech"]

    assert speech == {
        "upload_max_size_bytes": 104_857_600,
        "transcription_audio": {
            "profile": "compatibility",
            "format": "wav",
            "sample_rate_hz": 16_000,
        },
    }


def test_transcription_audio_profile_can_be_patched_atomically() -> None:
    operations = parse_patch_operations(
        [
            {
                "op": "set",
                "path": "speech.transcription_audio.profile",
                "value": "custom",
            },
            {
                "op": "set",
                "path": "speech.transcription_audio.format",
                "value": "flac",
            },
            {
                "op": "set",
                "path": "speech.transcription_audio.sample_rate_hz",
                "value": 24_000,
            },
        ]
    )

    updated, _changed = apply_settings_patch({}, operations)

    assert build_effective_settings(updated)["speech"]["transcription_audio"] == {
        "profile": "custom",
        "format": "flac",
        "sample_rate_hz": 24_000,
    }


def test_catalog_contains_static_and_dynamic_public_paths() -> None:
    paths = {entry["path"] for entry in catalog_payload()}

    assert "server.port" in paths
    assert "web_search.provider" in paths
    assert "speech.transcription_audio.profile" in paths
    assert 'local_models.context_windows["<model>"]' in paths
    assert 'extensions.config["<extension>"]["<field>"]' in paths
    assert 'model_tasks["<task>"].options["<option_path>"]...' in paths


def test_unknown_path_suggests_catalog_candidate() -> None:
    with pytest.raises(SettingsPathError):
        resolve_setting("web_search.providr")
