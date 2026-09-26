"""Tests for storage."""

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from core.settings import (
    DEFAULT_APPEARANCE_CHAT_WIDTH,
    DEFAULT_APPEARANCE_CHAT_WORKING_MODE,
    DEFAULT_APPEARANCE_LANGUAGE,
)
from core.settings.paths import SettingsPathError, parse_patch_operations
from core.storage import (
    StorageError,
    StorageManager,
)
from core.storage import storage as storage_module


def test_load_settings_returns_empty_when_missing(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)

    assert storage.load_settings() == {}


def test_save_and_load_settings_round_trip(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)
    settings = {"port": 8420, "keep_awake": True, "timezone": "Europe/Berlin"}

    storage.save_settings(settings)

    assert storage.load_settings() == settings
    text = storage.settings_path.read_text(encoding="utf-8")
    assert text.endswith("\n")
    assert list(json.loads(text)) == ["format_version", "keep_awake", "port", "timezone"]
    assert json.loads(text)["format_version"] == 1


def test_settings_updates_keep_unknown_fields_on_disk_at_every_modeled_level(
    tmp_path: Path,
) -> None:
    storage = StorageManager(tmp_path)
    storage.ensure_directories()
    storage.settings_path.write_text(
        json.dumps(
            {
                "format_version": 1,
                "future_top_level": {"kept": True},
                "appearance": {"language": "en", "future_appearance": 1},
                "providers": {
                    "custom": {
                        "local": {
                            "name": "Local",
                            "adapter": "openai_compatible",
                            "base_url": "http://127.0.0.1:1234/v1",
                            "auth": "none",
                            "future_provider_field": "x",
                            "models": {"m": {"name": "M", "future_model_field": 2}},
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    loaded = storage.load_settings()
    storage.update_settings_sections({"appearance": {"language": "en", "chat_width": "wide"}})
    storage.update_settings(lambda settings: settings.update({"keep_awake": True}))

    assert "future_top_level" not in loaded
    assert loaded["appearance"] == {"language": "en"}
    assert "future_provider_field" not in loaded["providers"]["custom"]["local"]
    on_disk = json.loads(storage.settings_path.read_text(encoding="utf-8"))
    assert on_disk["future_top_level"] == {"kept": True}
    assert on_disk["appearance"]["future_appearance"] == 1
    assert on_disk["appearance"]["chat_width"] == "wide"
    assert on_disk["keep_awake"] is True
    local = on_disk["providers"]["custom"]["local"]
    assert local["future_provider_field"] == "x"
    assert local["models"]["m"]["future_model_field"] == 2


@pytest.mark.parametrize("mutation", ["patch", "section"])
def test_reset_last_agent_default_keeps_unknown_siblings(tmp_path: Path, mutation: str) -> None:
    storage = StorageManager(tmp_path)
    storage.save_settings(
        {
            "defaults": {
                "agent": {"temperature": 0.2, "future_agent_option": {"enabled": True}},
                "future_defaults_option": "retained",
            }
        }
    )

    if mutation == "patch":
        operations = parse_patch_operations([{"op": "unset", "path": "defaults.agent.temperature"}])
        storage.patch_settings(operations)
    else:
        storage.update_settings_sections({"defaults": {"agent": {"temperature": None}}})

    assert storage.load_defaults() == {}
    assert json.loads(storage.settings_path.read_text(encoding="utf-8"))["defaults"] == {
        "agent": {"future_agent_option": {"enabled": True}},
        "future_defaults_option": "retained",
    }


@pytest.mark.parametrize("mutation", ["patch", "section"])
def test_remove_last_task_binding_keeps_unknown_siblings_only(
    tmp_path: Path, mutation: str
) -> None:
    storage = StorageManager(tmp_path)
    storage.save_settings(
        {
            "model_tasks": {
                "text_to_speech": {"target": "openai/tts-1", "future_option": True},
                "future_task": {"target": "future/target"},
            }
        }
    )

    if mutation == "patch":
        operations = parse_patch_operations(
            [{"op": "unset", "path": 'model_tasks["text_to_speech"].target'}]
        )
        storage.patch_settings(operations)
    else:
        storage.update_model_task_settings({"text_to_speech": {"target": ""}})

    assert storage.load_model_task_settings() == {}
    assert json.loads(storage.settings_path.read_text(encoding="utf-8"))["model_tasks"] == {
        "future_task": {"target": "future/target"}
    }


def test_reset_missing_settings_does_not_create_empty_containers(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)

    storage.update_settings_sections({"defaults": {"agent": {"temperature": None}}})
    storage.update_model_task_settings({"text_to_speech": {"target": ""}})

    assert storage.load_settings() == {}


@pytest.mark.parametrize("reset", ["last_field", "whole_policy"])
@pytest.mark.parametrize("unknown_field", [False, True])
def test_reset_model_routing_override_prunes_only_after_preserving_unknown_fields(
    tmp_path: Path, reset: str, unknown_field: bool
) -> None:
    storage = StorageManager(tmp_path)
    default_policy = {"mode": "allowed", "providers": ["approved"], "allow_fallbacks": False}
    storage.save_settings(
        {
            "providers": {
                "openrouter": {
                    "routing": {
                        "default": default_policy,
                        "models": {
                            "test/model": {
                                "mode": "automatic",
                                **({"future_option": "retained"} if unknown_field else {}),
                            },
                            "future/model": {"future_option": "retained"},
                        },
                        "future_routing": True,
                    },
                    "future_openrouter": True,
                },
                "future_provider": True,
            }
        }
    )
    path = 'providers.openrouter.routing.models["test/model"]'
    if reset == "last_field":
        path += ".mode"
    operations = parse_patch_operations([{"op": "unset", "path": path}])

    _previous, candidate, changed = storage.patch_settings(operations)
    assert changed == (path,)

    routing = storage.load_openrouter_routing_settings()
    if reset == "last_field" and unknown_field:
        assert candidate["providers"]["openrouter"]["routing"]["models"]["test/model"] == {}
        assert routing["models"]["test/model"] == {
            "mode": "automatic",
            "providers": [],
            "blocked": [],
            "allow_fallbacks": True,
        }
    else:
        assert "test/model" not in routing["models"]
    assert routing["default"] == {**default_policy, "blocked": []}
    on_disk = json.loads(storage.settings_path.read_text(encoding="utf-8"))["providers"]
    assert on_disk["future_provider"] is True
    assert on_disk["openrouter"]["future_openrouter"] is True
    assert on_disk["openrouter"]["routing"]["future_routing"] is True
    expected = {"future/model": {"future_option": "retained"}}
    if reset == "last_field" and unknown_field:
        expected["test/model"] = {"future_option": "retained"}
    assert on_disk["openrouter"]["routing"]["models"] == expected


def test_empty_and_unknown_only_model_policies_keep_automatic_meaning(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)
    storage.save_settings(
        {
            "providers": {
                "openrouter": {
                    "routing": {
                        "default": {
                            "mode": "allowed",
                            "providers": ["approved"],
                            "allow_fallbacks": False,
                        },
                        "models": {
                            "empty/model": {},
                            "future/model": {"future_option": "retained"},
                            "explicit/model": {"mode": "automatic"},
                        },
                    }
                }
            }
        }
    )
    operations = parse_patch_operations(
        [{"op": "unset", "path": 'providers.openrouter.routing.models["future/model"].mode'}]
    )

    previous, candidate, changed = storage.patch_settings(operations)
    assert changed == ()
    assert candidate == previous
    storage.set_provider_connection_enabled("openrouter:api-key", True)

    routing = storage.load_openrouter_routing_settings()
    automatic = {"mode": "automatic", "providers": [], "blocked": [], "allow_fallbacks": True}
    assert routing["models"] == dict.fromkeys(
        ("empty/model", "future/model", "explicit/model"), automatic
    )
    on_disk = json.loads(storage.settings_path.read_text(encoding="utf-8"))
    assert on_disk["providers"]["openrouter"]["routing"]["models"]["future/model"] == {
        **automatic,
        "future_option": "retained",
    }


def test_patch_runtime_validation_sees_only_known_fields_and_precedes_write(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)
    storage.save_settings({"web_search": {"provider": "searxng", "future_option": "retained"}})
    original = storage.settings_path.read_bytes()
    operations = parse_patch_operations([{"op": "unset", "path": "web_search.provider"}])

    def reject(previous: dict[str, Any], candidate: dict[str, Any]) -> None:
        assert previous == {"web_search": {"provider": "searxng"}}
        assert candidate == {"web_search": {}}
        raise ValueError("runtime validation failed")

    with pytest.raises(ValueError, match="runtime validation failed"):
        storage.patch_settings(operations, validate_candidate=reject)
    assert storage.settings_path.read_bytes() == original


def test_patch_still_rejects_new_unknown_fields(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)
    storage.save_settings({"providers": {"future_option": "retained"}})
    original = storage.settings_path.read_bytes()
    operations = parse_patch_operations(
        [{"op": "set", "path": "providers.openrouter.routing.default", "value": {"typo": True}}]
    )

    with pytest.raises(SettingsPathError, match="unsupported"):
        storage.patch_settings(operations)
    assert storage.settings_path.read_bytes() == original


def test_settings_written_by_a_newer_vbot_are_not_used_or_overwritten(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)
    storage.ensure_directories()
    original = '{"format_version": 2, "keep_awake": true}'
    storage.settings_path.write_text(original, encoding="utf-8")

    assert storage.load_settings() == {}
    with pytest.raises(StorageError, match="written by a newer vBot"):
        storage.update_settings(lambda settings: settings.update({"keep_awake": False}))
    with pytest.raises(StorageError, match="Refusing to overwrite Settings file"):
        storage.save_settings({"keep_awake": False})
    with pytest.raises(StorageError, match="written by a newer vBot"):
        storage.patch_settings(
            parse_patch_operations([{"op": "unset", "path": "server.keep_awake"}])
        )
    assert storage.settings_path.read_text(encoding="utf-8") == original


def test_load_settings_ignores_non_object_json(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    storage = StorageManager(tmp_path)
    storage.ensure_directories()
    storage.settings_path.write_text("[]", encoding="utf-8")

    with caplog.at_level("WARNING"):
        loaded = storage.load_settings()

    assert loaded == {}
    assert caplog.records
    assert storage.settings_path.read_text(encoding="utf-8") == "[]"


def test_load_settings_ignores_invalid_json(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    storage = StorageManager(tmp_path)
    storage.ensure_directories()
    storage.settings_path.write_text("{", encoding="utf-8")

    with caplog.at_level("WARNING"):
        loaded = storage.load_settings()

    assert loaded == {}
    assert caplog.records
    assert storage.settings_path.read_text(encoding="utf-8") == "{"


def test_load_settings_ignores_invalid_schema_fields(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    storage = StorageManager(tmp_path)
    storage.ensure_directories()
    original = '{"format_version": 1, "server_port": 8500, "compaction": {"enabled": "yes"}}'
    storage.settings_path.write_text(original, encoding="utf-8")

    with caplog.at_level("WARNING"):
        loaded = storage.load_settings()

    assert loaded == {"server_port": 8500}
    assert caplog.records
    assert storage.settings_path.read_text(encoding="utf-8") == original


def _age(path: Path, *, seconds: float = 60.0) -> None:
    """Move a file's mtime out of the racy window, as if written a while ago."""
    past = time.time_ns() - int(seconds * 1_000_000_000)
    os.utime(path, ns=(past, past))


def _count_settings_reads(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    reads: list[Path] = []
    load = storage_module.load_runtime_settings_json

    def counting(path: Path) -> Any:
        reads.append(path)
        return load(path)

    monkeypatch.setattr(storage_module, "load_runtime_settings_json", counting)
    return reads


def test_unchanged_settings_are_served_from_memory_as_independent_copies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = StorageManager(tmp_path)
    storage.save_settings({"defaults": {"agent": {"temperature": 0.5}}, "keep_awake": True})
    _age(storage.settings_path)
    reads = _count_settings_reads(monkeypatch)

    first = storage.load_settings()
    first["defaults"]["agent"]["temperature"] = 1.0
    first["keep_awake"] = False
    second = storage.load_settings()

    assert second == {"defaults": {"agent": {"temperature": 0.5}}, "keep_awake": True}
    assert len(reads) == 1


def test_settings_changes_are_read_after_every_kind_of_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = StorageManager(tmp_path)
    storage.ensure_directories()
    storage.settings_path.write_text('{"format_version": 1, "port": 8421}', encoding="utf-8")
    _age(storage.settings_path)
    assert storage.load_settings() == {"port": 8421}

    # An external in-place edit of the same size keeps the file identity.
    storage.settings_path.write_text('{"format_version": 1, "port": 8422}', encoding="utf-8")
    assert storage.load_settings() == {"port": 8422}

    _age(storage.settings_path)
    storage.load_settings()
    storage.save_settings({"keep_awake": True, "port": 8426})
    assert storage.load_settings() == {"keep_awake": True, "port": 8426}

    storage.settings_path.unlink()
    assert storage.load_settings() == {}


def test_recently_written_settings_are_reread_even_with_an_identical_stamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two writes in one filesystem timestamp tick must not look unchanged."""
    storage = StorageManager(tmp_path)
    storage.ensure_directories()
    path = storage.settings_path
    path.write_text('{"format_version": 1, "port": 8421}', encoding="utf-8")
    stamp = path.stat().st_mtime_ns
    assert storage.load_settings() == {"port": 8421}

    path.write_text('{"format_version": 1, "port": 8422}', encoding="utf-8")
    os.utime(path, ns=(stamp, stamp))

    assert storage.load_settings() == {"port": 8422}


def test_load_settings_logs_unchanged_degradation_only_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    storage = StorageManager(tmp_path)
    storage.ensure_directories()
    storage.settings_path.write_text('{"format_version": 1, "debug": []}', encoding="utf-8")

    with caplog.at_level("WARNING"):
        storage.load_settings()
        storage.load_settings()

    assert len(caplog.records) == 1


@pytest.mark.parametrize(
    "invalid_section",
    [
        {"appearance": {"chat_width": []}},
        {"appearance": {"chat_working_mode": {}}},
        {"compaction": {"trigger": {"threshold": 10**400}}},
        {"defaults": {"agent": {"temperature": 10**400}}},
    ],
)
def test_load_settings_isolates_malformed_values_without_losing_valid_siblings(
    tmp_path: Path, invalid_section: dict[str, Any]
) -> None:
    storage = StorageManager(tmp_path)
    storage.save_settings({"keep_awake": True, **invalid_section})
    original = storage.settings_path.read_bytes()

    assert storage.load_settings() == {"keep_awake": True}
    with pytest.raises(StorageError):
        storage.update_settings(lambda settings: settings.update({"keep_awake": False}))
    assert storage.settings_path.read_bytes() == original


def test_load_settings_ignores_non_utf8_without_rewriting(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)
    original = b'{"appearance":{"language":"\xff"}}'
    storage.settings_path.write_bytes(original)

    assert storage.load_settings() == {}
    with pytest.raises(StorageError):
        storage.update_settings(lambda settings: settings.update({"keep_awake": False}))
    assert storage.settings_path.read_bytes() == original


def test_settings_json_integer_limit_is_reported_without_overwriting(tmp_path: Path) -> None:
    from core.settings import validate_settings_file

    storage = StorageManager(tmp_path)
    previous_limit = sys.get_int_max_str_digits()
    try:
        sys.set_int_max_str_digits(640)
        original = '{"unknown": ' + "9" * 641 + "}"
        storage.settings_path.write_text(original, encoding="utf-8")

        assert storage.load_settings() == {}
        assert not validate_settings_file(storage.settings_path).ok
        with pytest.raises(StorageError):
            storage.update_settings(lambda settings: settings.update({"keep_awake": False}))
        assert storage.settings_path.read_text(encoding="utf-8") == original
    finally:
        sys.set_int_max_str_digits(previous_limit)


def test_update_settings_rejects_invalid_file_without_overwriting_it(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)
    storage.ensure_directories()
    original = '{"format_version": 1, "server_port": 8420, "compaction": {"enabled": "yes"}}'
    storage.settings_path.write_text(original, encoding="utf-8")

    with pytest.raises(StorageError):
        storage.update_settings(lambda settings: settings.update({"server_port": 8500}))

    assert storage.settings_path.read_text(encoding="utf-8") == original


def test_load_defaults_returns_empty_when_missing(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)

    assert storage.load_defaults() == {}


def test_load_defaults_reads_and_normalizes_all_agent_fields(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)
    storage.save_settings(
        {
            "defaults": {
                "agent": {
                    "model": "openrouter/anthropic/claude-sonnet-4",
                    "fallback_models": ["openai/gpt-4.1-mini"],
                    "temperature": 1,
                    "thinking_effort": "medium",
                }
            }
        }
    )

    defaults = storage.load_defaults()

    assert defaults == {
        "agent": {
            "model": "openrouter/anthropic/claude-sonnet-4",
            "fallback_models": ["openai/gpt-4.1-mini"],
            "temperature": 1.0,
            "thinking_effort": "medium",
        }
    }


def test_update_defaults_none_value_removes_existing_key(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)
    storage.save_settings(
        {
            "defaults": {
                "agent": {
                    "model": "openrouter/anthropic/claude-sonnet-4",
                    "temperature": 0.7,
                    "thinking_effort": "high",
                }
            },
            "server_port": 8500,
        }
    )

    updated = storage.update_settings_sections({"defaults": {"agent": {"temperature": None}}})

    assert updated["defaults"] == {
        "agent": {
            "model": "openrouter/anthropic/claude-sonnet-4",
            "thinking_effort": "high",
        }
    }
    assert storage.load_settings() == {
        "defaults": {
            "agent": {
                "model": "openrouter/anthropic/claude-sonnet-4",
                "thinking_effort": "high",
            }
        },
        "server_port": 8500,
    }


@pytest.mark.parametrize(
    ("values", "_message"),
    [
        ({"temperature": 2.5}, "Agent default temperature must be between 0 and 2"),
        ({"thinking_effort": "ultra"}, "Agent default thinking_effort must be one of"),
        ({"model": 123}, "Agent default model must be a string"),
    ],
)
def test_update_defaults_rejects_invalid_agent_values(
    tmp_path: Path,
    values: dict[str, Any],
    _message: str,
) -> None:
    storage = StorageManager(tmp_path)

    with pytest.raises(StorageError):
        storage.update_settings_sections({"defaults": {"agent": values}})


def test_save_settings_rejects_unserializable_values(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)

    with pytest.raises(StorageError):
        storage.save_settings({"path": object()})

    assert "path" not in json.loads(storage.settings_path.read_text(encoding="utf-8"))


def test_load_appearance_settings_returns_default_language_when_missing(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)

    assert storage.load_appearance_settings() == {
        "language": DEFAULT_APPEARANCE_LANGUAGE,
        "chat_width": DEFAULT_APPEARANCE_CHAT_WIDTH,
        "chat_working_mode": DEFAULT_APPEARANCE_CHAT_WORKING_MODE,
    }


def test_load_appearance_settings_defaults_invalid_section(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)
    storage.save_settings({"appearance": []})

    assert storage.load_appearance_settings() == {
        "language": DEFAULT_APPEARANCE_LANGUAGE,
        "chat_width": DEFAULT_APPEARANCE_CHAT_WIDTH,
        "chat_working_mode": DEFAULT_APPEARANCE_CHAT_WORKING_MODE,
    }


def test_speech_settings_default_and_update_round_trip(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)

    assert storage.load_speech_settings() == {
        "transcription_audio": {
            "profile": "compatibility",
            "format": "wav",
            "sample_rate_hz": 16_000,
        }
    }

    updated = storage.update_settings_sections(
        {
            "speech": {
                "transcription_audio": {
                    "profile": "custom",
                    "format": "flac",
                    "sample_rate_hz": 24_000,
                }
            }
        }
    )

    assert updated["speech"] == {
        "transcription_audio": {
            "profile": "custom",
            "format": "flac",
            "sample_rate_hz": 24_000,
        }
    }
    assert storage.load_speech_settings() == updated["speech"]


def test_load_subagent_settings_returns_defaults_when_missing(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)

    settings = storage.load_subagent_settings()

    assert settings == {
        "max_subagent_depth": 4,
        "max_subagents_per_turn": 8,
        "subagent_timeout_minutes": 60,
    }


def test_load_subagent_settings_reads_custom_values(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)
    storage.save_settings(
        {
            "max_subagent_depth": 2,
            "max_subagents_per_turn": 5,
            "subagent_timeout_minutes": 30,
        }
    )

    settings = storage.load_subagent_settings()

    assert settings == {
        "max_subagent_depth": 2,
        "max_subagents_per_turn": 5,
        "subagent_timeout_minutes": 30,
    }


def test_load_recall_settings_defaults_to_sqlite_fts(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)

    assert storage.load_recall_settings() == {"backend": "sqlite_fts"}


def test_session_title_settings_default_disabled_and_replace_as_complete_section(
    tmp_path: Path,
) -> None:
    storage = StorageManager(tmp_path)

    assert storage.load_session_title_settings() == {"enabled": False, "model": ""}

    updated = storage.update_settings_sections(
        {
            "session_titles": {
                "enabled": True,
                "model": " openai/gpt-4.1-mini::api-key ",
            }
        }
    )

    assert updated["session_titles"] == {
        "enabled": True,
        "model": "openai/gpt-4.1-mini::api-key",
    }
    assert storage.load_settings()["session_titles"] == updated["session_titles"]


def test_load_recall_settings_reads_configured_backend(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)
    storage.save_settings({"recall": {"backend": "sqlite_fts"}})

    assert storage.load_recall_settings() == {"backend": "sqlite_fts"}


def test_update_recall_settings_persists_under_recall_key(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)
    storage.save_settings({"server_port": 8500})

    updated = storage.update_settings_sections({"recall": {"backend": " sqlite_fts "}})

    assert updated["recall"] == {"backend": "sqlite_fts"}
    assert storage.load_settings() == {
        "server_port": 8500,
        "recall": {"backend": "sqlite_fts"},
    }


def test_update_recall_settings_rejects_unsupported_fields(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)

    with pytest.raises(StorageError):
        storage.update_settings_sections({"recall": {"backend": "sqlite_fts", "unknown": True}})


def test_load_extensions_settings_defaults_to_empty(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)

    assert storage.load_extensions_settings() == {"disabled": [], "config": {}}


def test_update_settings_sections_persists_extensions(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)
    storage.save_settings({"server_port": 8500})

    updated = storage.update_settings_sections(
        {
            "extensions": {
                "disabled": [" legacy ", "legacy", "old"],
                "config": {"guard_bash": {"deny": ["rm -rf"]}},
            }
        }
    )

    assert updated["extensions"] == {
        "disabled": ["legacy", "old"],
        "config": {"guard_bash": {"deny": ["rm -rf"]}},
    }
    assert storage.load_extensions_settings() == {
        "disabled": ["legacy", "old"],
        "config": {"guard_bash": {"deny": ["rm -rf"]}},
    }
    assert storage.load_settings()["server_port"] == 8500


def test_update_settings_sections_rejects_non_object_extension_config(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)

    with pytest.raises(StorageError, match="settings.extensions.config.guard_bash"):
        storage.update_settings_sections(
            {"extensions": {"config": {"guard_bash": {"bad": {1, 2}}}}}
        )


def test_load_web_search_settings_defaults_to_brave(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)

    assert storage.load_web_search_settings() == {
        "provider": "brave",
        "default_count": 12,
        "searxng": {"base_url": "http://localhost:8888"},
    }


def test_update_web_search_settings_persists_provider_and_searxng_url(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)
    storage.save_settings({"server_port": 8500})

    updated = storage.update_settings_sections(
        {
            "web_search": {
                "provider": "searxng",
                "default_count": 15,
                "searxng": {"base_url": " http://localhost:9999/ "},
            }
        }
    )

    assert updated["web_search"] == {
        "provider": "searxng",
        "default_count": 15,
        "searxng": {"base_url": "http://localhost:9999/"},
    }
    assert storage.load_settings() == {
        "server_port": 8500,
        "web_search": updated["web_search"],
    }


def test_update_web_search_settings_preserves_searxng_url_when_switching_provider(
    tmp_path: Path,
) -> None:
    storage = StorageManager(tmp_path)
    storage.update_settings_sections(
        {
            "web_search": {
                "provider": "searxng",
                "default_count": 15,
                "searxng": {"base_url": "http://localhost:9999"},
            }
        }
    )

    updated = storage.update_settings_sections({"web_search": {"provider": "brave"}})

    assert updated["web_search"] == {
        "provider": "brave",
        "default_count": 15,
        "searxng": {"base_url": "http://localhost:9999"},
    }


@pytest.mark.parametrize(
    ("web_search", "_message"),
    [
        ([], "Web search settings must be a mapping"),
        ({"provider": "unknown"}, "Web search provider must be one of"),
        (
            {"provider": "searxng", "searxng": []},
            "Expected settings.web_search.searxng to be an object",
        ),
        (
            {"provider": "searxng", "searxng": {"base_url": ""}},
            "SearXNG base_url must be a non-empty string",
        ),
        (
            {"provider": "brave", "default_count": 21},
            "Web search default_count must be an integer between 1 and 20",
        ),
    ],
)
def test_update_web_search_settings_rejects_invalid_payloads(
    tmp_path: Path,
    web_search: Any,
    _message: str,
) -> None:
    storage = StorageManager(tmp_path)

    with pytest.raises(StorageError):
        storage.update_settings_sections({"web_search": web_search})


def test_model_task_settings_round_trip_and_preserve_other_settings(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)
    storage.save_settings({"server_port": 8500})

    updated = storage.update_model_task_settings(
        {
            "speech_to_text": {
                "target": "openrouter/openai/gpt-4o-transcribe::api-key",
                "options": {"language": "auto"},
            }
        }
    )

    assert updated == {
        "speech_to_text": {
            "target": "openrouter/openai/gpt-4o-transcribe::api-key",
            "options": {"language": "auto"},
        }
    }
    assert storage.load_settings()["server_port"] == 8500
    assert storage.load_model_task_settings() == updated


def test_model_task_empty_target_removes_binding(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)
    storage.update_model_task_settings(
        {
            "speech_to_text": {
                "target": "openrouter/openai/gpt-4o-transcribe::api-key",
                "options": {},
            }
        }
    )

    updated = storage.update_model_task_settings({"speech_to_text": {"target": ""}})

    assert updated == {}
    assert storage.load_settings()["model_tasks"] == {}


@pytest.mark.parametrize("change_target", [False, True])
def test_model_task_target_update_resets_only_changed_target_options(
    tmp_path: Path, change_target: bool
) -> None:
    storage = StorageManager(tmp_path)
    original = "openai/first::api-key"
    options = {"voice": "custom-voice"}
    storage.update_model_task_settings({"text_to_speech": {"target": original, "options": options}})
    target = "openai/second::api-key" if change_target else original

    storage.update_settings_sections({"model_tasks": {"text_to_speech": {"target": target}}})

    assert storage.load_model_task_settings()["text_to_speech"] == {
        "target": target,
        "options": {} if change_target else options,
    }


def test_load_recall_settings_defaults_invalid_section(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)
    storage.ensure_directories()
    storage.settings_path.write_text('{"format_version": 1, "recall": []}', encoding="utf-8")

    assert storage.load_recall_settings() == {"backend": "sqlite_fts"}


def test_load_compaction_settings_returns_defaults_when_missing(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)

    settings = storage.load_compaction_settings()

    assert settings == {
        "enabled": True,
        "trigger": {"type": "context_ratio", "threshold": 0.8},
        "strategy": {
            "type": "summary_tail",
            "tail_tokens": 15_000,
            "summary_model": None,
        },
    }


def test_load_compaction_settings_reads_and_normalizes_values(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)
    storage.save_settings(
        {
            "compaction": {
                "enabled": False,
                "trigger": {"type": "context_ratio", "threshold": 1, "tokens": 200_000},
                "strategy": {
                    "type": "summary_tail",
                    "tail_tokens": 7_500,
                    "summary_model": "openrouter/anthropic/claude-sonnet-4",
                },
            }
        }
    )

    settings = storage.load_compaction_settings()

    assert settings == {
        "enabled": False,
        "trigger": {"type": "context_ratio", "threshold": 1.0, "tokens": 200_000},
        "strategy": {
            "type": "summary_tail",
            "tail_tokens": 7_500,
            "summary_model": "openrouter/anthropic/claude-sonnet-4",
        },
    }


def test_update_compaction_settings_persists_under_compaction_key(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)
    storage.save_settings(
        {
            "server_port": 8500,
            "compaction": {
                "enabled": False,
                "trigger": {"type": "context_ratio", "threshold": 0.9},
                "strategy": {
                    "type": "summary_tail",
                    "tail_tokens": 12_000,
                    "summary_model": None,
                },
            },
        }
    )

    updated = storage.update_settings_sections(
        {
            "compaction": {
                "strategy": {
                    "type": "summary_tail",
                    "tail_tokens": 8_000,
                    "summary_model": "openai/gpt-4.1-mini",
                },
            }
        }
    )

    assert updated["compaction"] == {
        "enabled": False,
        "trigger": {"type": "context_ratio", "threshold": 0.9},
        "strategy": {
            "type": "summary_tail",
            "tail_tokens": 8_000,
            "summary_model": "openai/gpt-4.1-mini",
        },
    }
    assert storage.load_compaction_settings() == updated["compaction"]
    assert storage.load_settings() == {"compaction": updated["compaction"], "server_port": 8500}
