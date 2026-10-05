"""Settings sections: accessor defaults, sparse section merges, rejections and transactions.

Section value normalization is owned by ``core.settings`` (``tests/core/settings``);
these tests cover what Storage adds: reading each section from the file, merging a
public section update into the persisted file, and writing it in one transaction.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from core.storage import SettingsConflictError, StorageError, StorageManager

APPEARANCE_DEFAULTS = {"language": "en", "chat_width": "comfortable", "chat_working_mode": "normal"}
REFLECTION_DEFAULTS = {"enabled": True, "memory_turn_interval": 10, "skill_model_step_interval": 10}
COMPACTION_DEFAULTS = {
    "enabled": True,
    "trigger": {"type": "context_ratio", "threshold": 0.8},
    "strategy": {"type": "summary_tail", "tail_tokens": 15_000, "summary_model": None},
}
WEB_SEARCH_DEFAULTS = {
    "provider": "brave",
    "default_count": 12,
    "searxng": {"base_url": "http://localhost:8888"},
}
NOTIFICATION_DEFAULTS = {
    "run_completed": True,
    "run_failed": True,
    "automation_failed": True,
    "update_result": True,
    "server_stopped": True,
}

Read = Callable[[StorageManager], Any]


def _read_server(storage: StorageManager) -> dict[str, Any]:
    """The public ``server`` section persists as flat top-level keys."""
    settings = storage.load_settings()
    return {"keep_awake": settings["keep_awake"], "timezone": settings["timezone"]}


# (accessor, an invalid persisted section, the accessor's defaults)
ACCESSOR_DEFAULTS: list[tuple[Read, dict[str, Any], Any]] = [
    (StorageManager.load_appearance_settings, {"appearance": []}, APPEARANCE_DEFAULTS),
    (
        StorageManager.load_speech_settings,
        {"speech": []},
        {
            "transcription_audio": {
                "profile": "compatibility",
                "format": "wav",
                "sample_rate_hz": 16_000,
            }
        },
    ),
    (
        StorageManager.load_subagent_settings,
        {"max_subagent_depth": "deep"},
        {"max_subagent_depth": 4, "max_active_subagents": 8},
    ),
    (StorageManager.load_skill_directory_settings, {"skill_directories": "~/skills"}, []),
    (
        StorageManager.load_compaction_settings,
        {"compaction": {"enabled": "yes"}},
        COMPACTION_DEFAULTS,
    ),
    (StorageManager.load_defaults, {"defaults": []}, {}),
    (
        StorageManager.load_session_title_settings,
        {"session_titles": []},
        {"enabled": False, "model": ""},
    ),
    (StorageManager.load_recall_settings, {"recall": []}, {"backend": "sqlite_fts"}),
    (
        StorageManager.load_local_models_settings,
        {"local_models": {"context_windows": {"ollama/m": 0}}},
        {"context_windows": {}},
    ),
    (StorageManager.load_providers_settings, {"providers": []}, {"connections": {}}),
    (
        StorageManager.load_providers_settings,
        {"providers": {"connections": {"ollama": True, "ollama:local": "yes"}}},
        {"connections": {}},
    ),
    (
        StorageManager.load_debug_settings,
        {"debug": {"trace_limit": 501}},
        {"enabled": False, "trace_limit": 50},
    ),
    (
        StorageManager.load_archive_settings,
        {"archive": {"retention_days": 0}},
        {"retention_days": 30},
    ),
    (
        StorageManager.load_reflection_settings,
        {"reflection": {"memory_turn_interval": 0}},
        REFLECTION_DEFAULTS,
    ),
    (
        StorageManager.load_librarian_settings,
        {"librarian": {"interval_days": 0}},
        {
            "enabled": True,
            "interval_days": 7,
            "archive_after_days": 90,
            "consolidate": True,
        },
    ),
    (
        StorageManager.load_web_search_settings,
        {"web_search": {"provider": "unknown"}},
        WEB_SEARCH_DEFAULTS,
    ),
    (StorageManager.load_model_task_settings, {"model_tasks": []}, {}),
    (
        StorageManager.load_notification_settings,
        {"notifications": {"run_failed": "yes"}},
        NOTIFICATION_DEFAULTS,
    ),
    (
        StorageManager.load_extensions_settings,
        {"extensions": []},
        {"disabled": [], "config": {}},
    ),
]


@pytest.mark.parametrize(
    ("read", "invalid_section", "defaults"),
    [pytest.param(*row, id=row[0].__name__) for row in ACCESSOR_DEFAULTS],
)
def test_section_accessor_returns_defaults_for_a_missing_or_invalid_section(
    tmp_path: Path, read: Read, invalid_section: dict[str, Any], defaults: Any
) -> None:
    storage = StorageManager(tmp_path)
    assert read(storage) == defaults

    storage.ensure_directories()
    storage.settings_path.write_text(
        json.dumps({"format_version": 1, **invalid_section}), encoding="utf-8"
    )

    assert read(storage) == defaults


@pytest.mark.parametrize(
    "stored",
    [
        pytest.param('{"format_version": 1, "archive": {"retention_days": 0}}', id="invalid"),
        pytest.param('{"format_version": 1, "archive": {"retention_days": "never"}}', id="text"),
        pytest.param('{"format_version": 1, "archive": {"retention_days": null', id="syntax"),
        pytest.param('{"format_version": 99, "archive": {"retention_days": null}}', id="newer"),
    ],
)
def test_a_strict_archive_read_never_falls_back_to_the_default_period(
    tmp_path: Path, stored: str
) -> None:
    # Retention reads strictly: a default period could delete entries the user
    # meant to keep longer. Settings still show the tolerant read.
    storage = StorageManager(tmp_path)
    assert storage.load_archive_settings(strict=True) == {"retention_days": 30}
    storage.ensure_directories()

    storage.settings_path.write_text(stored, encoding="utf-8")
    with pytest.raises(StorageError):
        storage.load_archive_settings(strict=True)
    assert storage.load_archive_settings() == {"retention_days": 30}

    # Another invalid section does not matter.
    storage.settings_path.write_text(
        json.dumps(
            {
                "format_version": 1,
                "archive": {"retention_days": None},
                "debug": {"trace_limit": 501},
            }
        ),
        encoding="utf-8",
    )
    assert storage.load_archive_settings(strict=True) == {"retention_days": None}


# (stored settings, one-section update, read-back, expected section)
SECTION_UPDATES: dict[str, tuple[dict[str, Any], dict[str, Any], Read, Any]] = {
    "appearance-display-preferences": (
        {},
        {"appearance": {"language": "en", "chat_width": "wide", "chat_working_mode": "compact"}},
        StorageManager.load_appearance_settings,
        {"language": "en", "chat_width": "wide", "chat_working_mode": "compact"},
    ),
    "appearance-replaces-deprecated-and-unknown-values": (
        {"appearance": {"language": "en", "show_token_counts": False, "theme": "dark"}},
        {"appearance": {"language": "en", "chat_width": "bogus"}},
        lambda storage: storage.load_settings()["appearance"],
        APPEARANCE_DEFAULTS,
    ),
    "speech-replaces-section": (
        {},
        {
            "speech": {
                "transcription_audio": {
                    "profile": "custom",
                    "format": "flac",
                    "sample_rate_hz": 24_000,
                }
            }
        },
        StorageManager.load_speech_settings,
        {"transcription_audio": {"profile": "custom", "format": "flac", "sample_rate_hz": 24_000}},
    ),
    "skills-persist-trimmed-directories": (
        {},
        {"skills": {"directories": ["~/skills", " C:/skills/team "]}},
        lambda storage: {"directories": storage.load_skill_directory_settings()},
        {"directories": ["~/skills", "C:/skills/team"]},
    ),
    "subagents-persist-flat-keys": (
        {},
        {"subagents": {"max_subagent_depth": 6, "max_active_subagents": 12}},
        lambda storage: {
            key: storage.load_settings()[key]
            for key in ("max_subagent_depth", "max_active_subagents")
        },
        {"max_subagent_depth": 6, "max_active_subagents": 12},
    ),
    "compaction-merges-into-stored-section": (
        {
            "compaction": {
                "enabled": False,
                "trigger": {"type": "context_ratio", "threshold": 0.9},
                "strategy": {"type": "summary_tail", "tail_tokens": 12_000, "summary_model": None},
            }
        },
        {
            "compaction": {
                "strategy": {
                    "type": "summary_tail",
                    "tail_tokens": 8_000,
                    "summary_model": "openai/gpt-4.1-mini",
                }
            }
        },
        StorageManager.load_compaction_settings,
        {
            "enabled": False,
            "trigger": {"type": "context_ratio", "threshold": 0.9},
            "strategy": {
                "type": "summary_tail",
                "tail_tokens": 8_000,
                "summary_model": "openai/gpt-4.1-mini",
            },
        },
    ),
    "defaults-null-removes-one-field": (
        {
            "defaults": {
                "agent": {
                    "model": "openrouter/anthropic/claude-sonnet-4",
                    "fallback_models": ["openai/gpt-4.1-mini"],
                    "temperature": 0.7,
                    "thinking_effort": "high",
                }
            }
        },
        {"defaults": {"agent": {"temperature": None}}},
        StorageManager.load_defaults,
        {
            "agent": {
                "model": "openrouter/anthropic/claude-sonnet-4",
                "fallback_models": ["openai/gpt-4.1-mini"],
                "thinking_effort": "high",
            }
        },
    ),
    "recall-trims-backend": (
        {},
        {"recall": {"backend": " sqlite_fts "}},
        StorageManager.load_recall_settings,
        {"backend": "sqlite_fts"},
    ),
    "web-search-trims-url": (
        {},
        {
            "web_search": {
                "provider": "searxng",
                "default_count": 15,
                "searxng": {"base_url": " http://localhost:9999/ "},
            }
        },
        StorageManager.load_web_search_settings,
        {
            "provider": "searxng",
            "default_count": 15,
            "searxng": {"base_url": "http://localhost:9999/"},
        },
    ),
    "web-search-provider-switch-keeps-searxng-url": (
        {
            "web_search": {
                "provider": "searxng",
                "default_count": 15,
                "searxng": {"base_url": "http://localhost:9999"},
            }
        },
        {"web_search": {"provider": "brave"}},
        StorageManager.load_web_search_settings,
        {
            "provider": "brave",
            "default_count": 15,
            "searxng": {"base_url": "http://localhost:9999"},
        },
    ),
    "model-task-binding-with-options": (
        {},
        {"model_tasks": {"speech_to_text": {"target": "openai/a::key", "options": {"x": "auto"}}}},
        StorageManager.load_model_task_settings,
        {"speech_to_text": {"target": "openai/a::key", "options": {"x": "auto"}}},
    ),
    "model-task-new-target-resets-options": (
        {"model_tasks": {"text_to_speech": {"target": "openai/a::key", "options": {"voice": "v"}}}},
        {"model_tasks": {"text_to_speech": {"target": "openai/b::key"}}},
        StorageManager.load_model_task_settings,
        {"text_to_speech": {"target": "openai/b::key", "options": {}}},
    ),
    "model-task-same-target-keeps-options": (
        {"model_tasks": {"text_to_speech": {"target": "openai/a::key", "options": {"voice": "v"}}}},
        {"model_tasks": {"text_to_speech": {"target": "openai/a::key"}}},
        StorageManager.load_model_task_settings,
        {"text_to_speech": {"target": "openai/a::key", "options": {"voice": "v"}}},
    ),
    "model-task-empty-target-removes-binding": (
        {"model_tasks": {"speech_to_text": {"target": "openai/a::key", "options": {}}}},
        {"model_tasks": {"speech_to_text": {"target": ""}}},
        lambda storage: storage.load_settings()["model_tasks"],
        {},
    ),
    "debug-merges-into-stored-section": (
        {"debug": {"enabled": True, "trace_limit": 100}},
        {"debug": {"trace_limit": 200}},
        StorageManager.load_debug_settings,
        {"enabled": True, "trace_limit": 200},
    ),
    "archive-null-disables-retention": (
        {"archive": {"retention_days": 14}},
        {"archive": {"retention_days": None}},
        StorageManager.load_archive_settings,
        {"retention_days": None},
    ),
    "archive-empty-update-keeps-disabled-retention": (
        {"archive": {"retention_days": None}},
        {"archive": {}},
        StorageManager.load_archive_settings,
        {"retention_days": None},
    ),
    "server-keeps-unmentioned-flat-key": (
        {"keep_awake": True},
        {"server": {"timezone": "America/New_York"}},
        _read_server,
        {"keep_awake": True, "timezone": "America/New_York"},
    ),
    "server-disable-overrides-stored-value": (
        {"keep_awake": True, "timezone": "Europe/Berlin"},
        {"server": {"keep_awake": False}},
        _read_server,
        {"keep_awake": False, "timezone": "Europe/Berlin"},
    ),
    "extensions-deduplicate-disabled": (
        {},
        {
            "extensions": {
                "disabled": [" legacy ", "legacy", "old"],
                "config": {"guard_bash": {"deny": ["rm -rf"]}},
            }
        },
        StorageManager.load_extensions_settings,
        {"disabled": ["legacy", "old"], "config": {"guard_bash": {"deny": ["rm -rf"]}}},
    ),
    "reflection-merges-into-stored-section": (
        {
            "reflection": {
                "enabled": False,
                "memory_turn_interval": 7,
                "skill_model_step_interval": 33,
            }
        },
        {"reflection": {"memory_turn_interval": 12}},
        StorageManager.load_reflection_settings,
        {"enabled": False, "memory_turn_interval": 12, "skill_model_step_interval": 33},
    ),
    # The Librarian's Model belongs to its Agent; a stored `model` from before is ignored.
    "librarian-merges-into-stored-section": (
        {"librarian": {"enabled": False, "interval_days": 3, "model": "openai/gpt-5.2"}},
        {"librarian": {"consolidate": False}},
        StorageManager.load_librarian_settings,
        {
            "enabled": False,
            "interval_days": 3,
            "archive_after_days": 90,
            "consolidate": False,
        },
    ),
    "local-models-merge-set-and-remove-windows": (
        {"local_models": {"context_windows": {"ollama/a": 8192, "ollama/b": 16384}}},
        {
            "local_models": {
                "context_windows": {"ollama/a": None, "ollama/c": 4096, "ollama/never-set": None}
            }
        },
        StorageManager.load_local_models_settings,
        {"context_windows": {"ollama/b": 16384, "ollama/c": 4096}},
    ),
    "notifications-merge-into-stored-section": (
        {"notifications": {"run_completed": False}},
        {"notifications": {"server_stopped": False}},
        StorageManager.load_notification_settings,
        {**NOTIFICATION_DEFAULTS, "run_completed": False, "server_stopped": False},
    ),
    "session-titles-trim-model": (
        {},
        {"session_titles": {"enabled": True, "model": " openai/gpt-4.1-mini::api-key "}},
        StorageManager.load_session_title_settings,
        {"enabled": True, "model": "openai/gpt-4.1-mini::api-key"},
    ),
}


@pytest.mark.parametrize(
    ("stored", "update", "read", "expected"),
    SECTION_UPDATES.values(),
    ids=SECTION_UPDATES.keys(),
)
def test_section_update_merges_into_stored_settings_and_returns_what_is_read_back(
    tmp_path: Path,
    stored: dict[str, Any],
    update: dict[str, Any],
    read: Read,
    expected: Any,
) -> None:
    storage = StorageManager(tmp_path)
    storage.save_settings({"server_port": 8500, **stored})
    [section] = update

    updated = storage.update_settings_sections(update)

    assert updated == {section: expected}
    assert read(storage) == expected
    assert storage.load_settings()["server_port"] == 8500


@pytest.mark.parametrize(
    "update",
    [
        pytest.param([], id="not-a-mapping"),
        pytest.param({"bogus": {}}, id="unknown-section"),
        pytest.param({"appearance": "en"}, id="appearance-not-a-mapping"),
        pytest.param({"appearance": {}}, id="appearance-without-language"),
        pytest.param(
            {"appearance": {"language": "en", "show_token_counts": False}},
            id="appearance-unknown-field",
        ),
        pytest.param({"appearance": {"language": ""}}, id="appearance-empty-language"),
        pytest.param({"appearance": {"language": "fr"}}, id="appearance-unknown-language"),
        pytest.param({"skills": {"directories": "~/skills"}}, id="skills-not-a-list"),
        pytest.param({"skills": {"directories": [""]}}, id="skills-empty-directory"),
        pytest.param({"skills": {"directories": ["relative/skills"]}}, id="skills-relative"),
        pytest.param({"defaults": {"agent": {"temperature": 2.5}}}, id="defaults-out-of-range"),
        pytest.param({"recall": {"backend": "sqlite_fts", "x": 1}}, id="recall-unknown-field"),
        pytest.param(
            {"extensions": {"config": {"guard_bash": {"bad": {1, 2}}}}},
            id="extensions-non-json-config",
        ),
        pytest.param(
            {"extensions": {"config": {"audit": {"nested": [{"amount": float("nan")}]}}}},
            id="extensions-nonfinite-config",
        ),
        pytest.param(
            {
                "model_tasks": {
                    "speech_to_text": {
                        "target": "openai/a::key",
                        "options": {"nested": [{"amount": float("inf")}]},
                    }
                }
            },
            id="model-tasks-nonfinite-options",
        ),
        pytest.param({"web_search": []}, id="web-search-not-a-mapping"),
        pytest.param({"web_search": {"provider": "unknown"}}, id="web-search-unknown-provider"),
        pytest.param(
            {"web_search": {"provider": "searxng", "searxng": {"base_url": ""}}},
            id="web-search-empty-searxng-url",
        ),
        pytest.param(
            {"web_search": {"provider": "searxng", "searxng": []}},
            id="web-search-searxng-not-an-object",
        ),
        pytest.param(
            {"web_search": {"provider": "brave", "default_count": 21}},
            id="web-search-count-out-of-range",
        ),
        pytest.param({"debug": "not a dict"}, id="debug-not-a-mapping"),
        pytest.param({"debug": {"enabled": True, "extra": 1}}, id="debug-unknown-field"),
        pytest.param({"archive": {"retention_days": 3651}}, id="archive-out-of-range"),
        pytest.param({"archive": {"days": 7}}, id="archive-unknown-field"),
        pytest.param({"reflection": {"enabled": True, "unknown": 1}}, id="reflection-unknown"),
        pytest.param({"local_models": {"context_windows": {"ollama/m": -5}}}, id="window"),
        pytest.param({"local_models": {"context_windows": {}, "extra": 1}}, id="local-unknown"),
        pytest.param({"server": {"keep_awake": "yes"}}, id="server-non-boolean"),
        pytest.param({"notifications": {"run_failed": "yes"}}, id="notifications-non-boolean"),
        pytest.param({"server": {"port": 8421}}, id="server-unknown-field"),
        pytest.param({"server": {"timezone": "Mars/Olympus"}}, id="server-unknown-timezone"),
        pytest.param(
            {
                "debug": {"enabled": True},
                "compaction": {"trigger": {"type": "context_ratio", "threshold": 2}},
            },
            id="valid-section-with-failing-section",
        ),
    ],
)
def test_rejected_section_update_leaves_the_file_unchanged(tmp_path: Path, update: Any) -> None:
    storage = StorageManager(tmp_path)
    storage.save_settings({"server_port": 8500, "debug": {"enabled": False, "trace_limit": 50}})
    original = storage.settings_path.read_bytes()

    with pytest.raises(StorageError):
        storage.update_settings_sections(update)

    assert storage.settings_path.read_bytes() == original


_ALL_NOTIFICATIONS_ON = dict.fromkeys(NOTIFICATION_DEFAULTS, True)


# (stored, update, base, the paths a conflict names or None when the update applies)
BASED_UPDATES: dict[str, tuple[dict[str, Any], dict[str, Any], dict[str, Any], Any]] = {
    "unchanged-since-base": (
        {"debug": {"enabled": False, "trace_limit": 50}},
        {"debug": {"enabled": False, "trace_limit": 100}},
        {"debug": {"enabled": False, "trace_limit": 50}},
        None,
    ),
    # A full section would write back the old value of a leaf changed meanwhile.
    "stale-leaf-would-be-reverted": (
        {"notifications": {**_ALL_NOTIFICATIONS_ON, "run_failed": False}},
        {"notifications": {**_ALL_NOTIFICATIONS_ON, "run_completed": False}},
        {"notifications": _ALL_NOTIFICATIONS_ON},
        ("notifications.run_failed",),
    ),
    "same-leaf-changed-meanwhile": (
        {"debug": {"enabled": False, "trace_limit": 80}},
        {"debug": {"enabled": False, "trace_limit": 100}},
        {"debug": {"enabled": False, "trace_limit": 50}},
        ("debug.trace_limit",),
    ),
    # Nothing the update writes differs from the current value.
    "same-value-written-meanwhile": (
        {"notifications": {**_ALL_NOTIFICATIONS_ON, "run_failed": False}},
        {"notifications": {**_ALL_NOTIFICATIONS_ON, "run_failed": False}},
        {"notifications": _ALL_NOTIFICATIONS_ON},
        None,
    ),
    # Per-key sections change only the keys they name.
    "other-key-changed-meanwhile": (
        {"local_models": {"context_windows": {"ollama/a": 8192}}},
        {"local_models": {"context_windows": {"ollama/b": 2048}}},
        {"local_models": {"context_windows": {"ollama/b": None}}},
        None,
    ),
    # A base binding with an empty target is the caller's view of an unbound task.
    "task-bound-meanwhile": (
        {"model_tasks": {"text_to_speech": {"target": "openai/a::key", "options": {}}}},
        {"model_tasks": {"text_to_speech": {"target": "openai/b::key", "options": {}}}},
        {"model_tasks": {"text_to_speech": {"target": ""}}},
        ("model_tasks.text_to_speech.target",),
    ),
}


@pytest.mark.parametrize(
    ("stored", "update", "base", "stale_paths"),
    BASED_UPDATES.values(),
    ids=BASED_UPDATES.keys(),
)
def test_section_update_with_base_refuses_only_changes_over_newer_values(
    tmp_path: Path,
    stored: dict[str, Any],
    update: dict[str, Any],
    base: dict[str, Any],
    stale_paths: tuple[str, ...] | None,
) -> None:
    storage = StorageManager(tmp_path)
    storage.save_settings({"server_port": 8500, **stored})
    original = storage.settings_path.read_bytes()
    [section] = update

    if stale_paths is None:
        updated = storage.update_settings_sections(update, base=base)
        assert storage.load_settings()[section] == updated[section]
        return

    with pytest.raises(SettingsConflictError) as conflict:
        storage.update_settings_sections(update, base=base)

    assert conflict.value.paths == stale_paths
    assert storage.settings_path.read_bytes() == original


def test_multiple_sections_update_in_one_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    storage = StorageManager(tmp_path)
    storage.save_settings({"server_port": 8500})
    saves: list[dict[str, Any]] = []
    save_settings = storage.save_settings

    def count_save(settings: dict[str, Any]) -> None:
        saves.append(settings)
        save_settings(settings)

    monkeypatch.setattr(storage, "save_settings", count_save)

    updated = storage.update_settings_sections(
        {
            "appearance": {"language": "en"},
            "subagents": {"max_subagent_depth": 6, "max_active_subagents": 12},
            "recall": {"backend": "sqlite_fts"},
            "debug": {"enabled": True, "trace_limit": 200},
        }
    )

    assert len(saves) == 1
    assert set(updated) == {"appearance", "subagents", "recall", "debug"}
    assert storage.load_settings() == {
        "appearance": APPEARANCE_DEFAULTS,
        "debug": {"enabled": True, "trace_limit": 200},
        "max_active_subagents": 12,
        "max_subagent_depth": 6,
        "recall": {"backend": "sqlite_fts"},
        "server_port": 8500,
    }
