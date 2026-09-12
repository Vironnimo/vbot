"""Tests for storage."""





from pathlib import Path
from typing import Any

import pytest

from core.settings import (
    DEFAULT_APPEARANCE_CHAT_WIDTH,
    DEFAULT_APPEARANCE_CHAT_WORKING_MODE,
    DEFAULT_APPEARANCE_LANGUAGE,
)
from core.storage import (
    StorageError,
    StorageManager,
)


def test_load_settings_returns_empty_when_missing(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)

    assert storage.load_settings() == {}


def test_save_and_load_settings_round_trip(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)
    settings = {"port": 8420, "feature": True, "name": "vBot"}

    storage.save_settings(settings)

    assert storage.load_settings() == settings
    assert storage.settings_path.read_text(encoding="utf-8").endswith("\n")


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
    original = '{"server_port": 8500, "compaction": {"enabled": "yes"}}'
    storage.settings_path.write_text(original, encoding="utf-8")

    with caplog.at_level("WARNING"):
        loaded = storage.load_settings()

    assert loaded == {"server_port": 8500}
    assert caplog.records
    assert storage.settings_path.read_text(encoding="utf-8") == original


def test_load_settings_logs_unchanged_degradation_only_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    storage = StorageManager(tmp_path)
    storage.ensure_directories()
    storage.settings_path.write_text('{"debug": []}', encoding="utf-8")

    with caplog.at_level("WARNING"):
        storage.load_settings()
        storage.load_settings()

    assert len(caplog.records) == 1


def test_update_settings_rejects_invalid_file_without_overwriting_it(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)
    storage.ensure_directories()
    original = '{"server_port": 8420, "compaction": {"enabled": "yes"}}'
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

    assert not storage.settings_path.exists()


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
    assert "model_tasks" not in storage.load_settings()


def test_load_recall_settings_defaults_invalid_section(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)
    storage.ensure_directories()
    storage.settings_path.write_text('{"recall": []}', encoding="utf-8")

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
