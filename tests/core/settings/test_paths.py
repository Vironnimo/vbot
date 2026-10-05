"""Tests for the public Settings path and atomic patch contract."""

from __future__ import annotations

import re

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


@pytest.mark.parametrize(
    ("path", "message"),
    [
        # Dynamic keys need bracket quoting.
        pytest.param(
            "local_models.context_windows.ollama",
            'did you mean: local_models.context_windows["<model>"]',
            id="unquoted-dynamic-key",
        ),
        pytest.param("web_search.providr", "did you mean: web_search.provider", id="typo"),
        # Live voice is configured as a Task Model binding, not a visibility toggle.
        pytest.param(
            "live_voice.enabled",
            "unknown settings path 'live_voice.enabled'",
            id="removed-live-voice",
        ),
    ],
)
def test_unknown_path_suggests_catalog_candidates(path: str, message: str) -> None:
    with pytest.raises(SettingsPathError, match=re.escape(message)):
        resolve_setting(path)


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


@pytest.mark.parametrize(
    ("operations", "message"),
    [
        pytest.param(
            [{"op": "set", "path": "web_search.provider", "value": "invalid"}],
            "web_search.provider must be one of",
            id="invalid-value",
        ),
        pytest.param(
            [{"op": "set", "path": "notifications.run_failed", "value": "yes"}],
            "notifications.run_failed must be boolean",
            id="non-boolean-switch",
        ),
        # An unhashable operation name must not crash the set lookup.
        pytest.param(
            [{"op": [], "path": "server.port", "value": 8420}],
            "operations[0].op must be set or unset",
            id="non-string-op",
        ),
        # Oversized numbers are validation errors, not overflow crashes.
        pytest.param(
            [{"op": "set", "path": "server.port", "value": 10**400}],
            "server.port must be at most 65535",
            id="oversized-integer",
        ),
        pytest.param(
            [{"op": "set", "path": "archive.retention_days", "value": 0}],
            "archive.retention_days must be at least 1",
            id="retention-below-range",
        ),
        pytest.param(
            [{"op": "set", "path": "compaction.trigger.threshold", "value": 10**400}],
            "compaction.trigger.threshold must be at most 1",
            id="oversized-float",
        ),
        pytest.param(
            [
                {"op": "set", "path": "compaction.trigger", "value": {}},
                {"op": "set", "path": "compaction.trigger.type", "value": "context_ratio"},
            ],
            "must not duplicate or overlap paths",
            id="overlapping-paths",
        ),
    ],
)
def test_patch_rejects_invalid_operations(
    operations: list[dict[str, object]], message: str
) -> None:
    with pytest.raises(SettingsPathError, match=re.escape(message)):
        parse_patch_operations(operations)


def test_integer_patch_does_not_require_float_representation() -> None:
    value = 10**400
    operations = parse_patch_operations(
        [{"op": "set", "path": "attachments.max_size_bytes", "value": value}]
    )

    updated, _changed = apply_settings_patch({}, operations)

    assert updated["attachment_max_size_bytes"] == value


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("option_operation", ["set", "unset"])
def test_unsetting_task_target_rejects_overlapping_option_changes(
    reverse: bool, option_operation: str
) -> None:
    operations = [
        {"op": "unset", "path": 'model_tasks["text_to_speech"].target'},
        {
            "op": option_operation,
            "path": 'model_tasks["text_to_speech"].options["voice"]',
            **({"value": "echo"} if option_operation == "set" else {}),
        },
    ]
    if reverse:
        operations.reverse()

    with pytest.raises(SettingsPathError):
        parse_patch_operations(operations)


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("explicit_option", [False, True])
def test_task_target_patch_resets_old_options_before_explicit_changes(
    reverse: bool, explicit_option: bool
) -> None:
    original = {
        "model_tasks": {
            "text_to_speech": {
                "target": "local/previous",
                "options": {"old_option": True},
            }
        }
    }
    operations = [
        {
            "op": "set",
            "path": 'model_tasks["text_to_speech"].target',
            "value": "local/replacement",
        }
    ]
    if explicit_option:
        operations.append(
            {
                "op": "set",
                "path": 'model_tasks["text_to_speech"].options["voice"]',
                "value": "echo",
            }
        )
    if reverse:
        operations.reverse()

    updated, _changed = apply_settings_patch(original, parse_patch_operations(operations))

    assert build_effective_settings(updated)["model_tasks"]["text_to_speech"] == {
        "target": "local/replacement",
        "options": {"voice": "echo"} if explicit_option else {},
    }
    assert original["model_tasks"]["text_to_speech"]["options"] == {"old_option": True}


def test_same_task_target_patch_preserves_options() -> None:
    original = {
        "model_tasks": {"text_to_speech": {"target": "local/current", "options": {"voice": "echo"}}}
    }
    updated, changed = apply_settings_patch(
        original,
        parse_patch_operations(
            [
                {
                    "op": "set",
                    "path": 'model_tasks["text_to_speech"].target',
                    "value": "local/current",
                }
            ]
        ),
    )

    assert updated == original
    assert changed == ()


@pytest.mark.parametrize(
    ("previous_target", "next_target"),
    [
        ("openai/model::api-key", "openai/other::api-key"),
        ("openai/model::api-key", "openai/model::other-key"),
        ("openai/model::api-key:work", "openai/model::api-key:home"),
        ("local/engine", "local/other"),
    ],
)
def test_changed_task_identity_resets_options(previous_target: str, next_target: str) -> None:
    original = {
        "model_tasks": {"text_to_speech": {"target": previous_target, "options": {"voice": "echo"}}}
    }

    updated, _changed = apply_settings_patch(
        original,
        parse_patch_operations(
            [{"op": "set", "path": 'model_tasks["text_to_speech"].target', "value": next_target}]
        ),
    )

    assert build_effective_settings(updated)["model_tasks"]["text_to_speech"] == {
        "target": next_target,
        "options": {},
    }


def test_unset_removes_override_and_restores_default() -> None:
    original = {"web_search": {"provider": "searxng"}}
    operations = parse_patch_operations([{"op": "unset", "path": "web_search.provider"}])

    updated, changed = apply_settings_patch(original, operations)

    assert updated == {}
    assert changed == ("web_search.provider",)
    assert build_effective_settings(updated)["web_search"]["provider"] == "brave"


def test_archive_retention_null_disables_it_and_unset_restores_the_default() -> None:
    disabled, changed = apply_settings_patch(
        {"archive": {"retention_days": 14}},
        parse_patch_operations([{"op": "set", "path": "archive.retention_days", "value": None}]),
    )
    cleared, _changed = apply_settings_patch(
        disabled, parse_patch_operations([{"op": "unset", "path": "archive.retention_days"}])
    )

    assert disabled == {"archive": {"retention_days": None}}
    assert changed == ("archive.retention_days",)
    assert build_effective_settings(disabled)["archive"] == {"retention_days": None}
    assert setting_details(disabled, "archive.retention_days")["value"] is None
    assert cleared == {}
    assert build_effective_settings(cleared)["archive"] == {"retention_days": 30}


def test_unset_prunes_empty_nested_task_options() -> None:
    original = {
        "model_tasks": {
            "text_to_speech": {
                "target": "openai/tts-1",
                "options": {"audio": {"voice": "echo"}},
            }
        }
    }

    updated, _changed = apply_settings_patch(
        original,
        parse_patch_operations(
            [{"op": "unset", "path": 'model_tasks["text_to_speech"].options["audio"]["voice"]'}]
        ),
    )

    assert updated == {"model_tasks": {"text_to_speech": {"target": "openai/tts-1"}}}


def test_unset_prunes_empty_extension_config() -> None:
    updated, _changed = apply_settings_patch(
        {"extensions": {"config": {"example": {"field": True}}}},
        parse_patch_operations([{"op": "unset", "path": 'extensions.config["example"]["field"]'}]),
    )

    assert updated == {}


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


def test_context_ratio_trigger_accepts_and_unsets_an_absolute_cap() -> None:
    original = {"compaction": {"trigger": {"type": "context_ratio", "threshold": 0.8}}}
    capped, changed = apply_settings_patch(
        original,
        parse_patch_operations(
            [{"op": "set", "path": "compaction.trigger.tokens", "value": 200_000}]
        ),
    )

    assert capped["compaction"]["trigger"] == {
        "type": "context_ratio",
        "threshold": 0.8,
        "tokens": 200_000,
    }
    assert changed == ("compaction.trigger.tokens",)

    uncapped, changed = apply_settings_patch(
        capped,
        parse_patch_operations([{"op": "unset", "path": "compaction.trigger.tokens"}]),
    )

    assert uncapped == original
    assert changed == ("compaction.trigger.tokens",)


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
            "live_voice": {"enabled": True},
        }
    )

    assert effective["server"]["port"] == 9000
    assert effective["server"]["keep_awake"] is False
    assert isinstance(effective["server"]["timezone"], str)
    assert effective["skills"] == {"directories": ["~/skills"]}
    assert effective["subagents"] == {
        "max_subagent_depth": 2,
        "max_active_subagents": 8,
        "max_active_subagents_total": 50,
    }
    assert "server_port" not in effective
    assert "skill_directories" not in effective
    # Live voice is a Task Model binding; its old opt-in is not a setting.
    assert "live_voice" not in effective


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
    assert "server.timezone" in paths
    assert "web_search.provider" in paths
    assert "speech.transcription_audio.profile" in paths
    assert "notifications.automation_failed" in paths
    assert 'local_models.context_windows["<model>"]' in paths
    assert 'extensions.config["<extension>"]["<field>"]' in paths
    assert 'model_tasks["<task>"].options["<option_path>"]...' in paths


@pytest.mark.parametrize(
    ("raw", "keep_awake"),
    [({}, False), ({"keep_awake": True}, True), ({"keep_awake": "yes"}, False)],
    ids=["default", "configured", "unusable-falls-back"],
)
def test_server_keep_awake_reads_the_flat_raw_key(raw: dict[str, object], keep_awake: bool) -> None:
    assert build_effective_settings(raw)["server"]["keep_awake"] is keep_awake
    assert setting_details(raw, "server.keep_awake")["value"] is keep_awake


@pytest.mark.parametrize(
    ("path", "raw_key", "value"),
    [("server.keep_awake", "keep_awake", True), ("server.timezone", "timezone", "Europe/Berlin")],
)
def test_server_patch_maps_to_a_flat_raw_key(path: str, raw_key: str, value: object) -> None:
    updated, changed = apply_settings_patch(
        {}, parse_patch_operations([{"op": "set", "path": path, "value": value}])
    )
    cleared, _changed = apply_settings_patch(
        updated, parse_patch_operations([{"op": "unset", "path": path}])
    )

    assert updated == {raw_key: value}
    assert changed == (path,)
    assert build_effective_settings(updated)["server"][raw_key] == value
    assert cleared == {}
