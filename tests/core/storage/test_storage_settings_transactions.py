"""Tests for storage settings transactions."""





from pathlib import Path
from typing import Any

import pytest

from core.storage import (
    StorageError,
    StorageManager,
)


def test_update_settings_sections_persists_multiple_sections_with_one_save(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    storage = StorageManager(tmp_path)
    storage.save_settings({"server_port": 8500})
    save_count = 0
    original_save_settings = storage.save_settings

    def count_save(settings: dict[str, Any]) -> None:
        nonlocal save_count
        save_count += 1
        original_save_settings(settings)

    monkeypatch.setattr(storage, "save_settings", count_save)

    updated = storage.update_settings_sections(
        {
            "appearance": {"language": "en"},
            "subagents": {
                "max_subagent_depth": 6,
                "max_subagents_per_turn": 12,
                "subagent_timeout_minutes": 90,
            },
            "compaction": {
                "enabled": False,
                "trigger": {"type": "context_ratio", "threshold": 0.9},
                "strategy": {
                    "type": "summary_tail",
                    "tail_tokens": 12_000,
                    "summary_model": None,
                },
            },
            "recall": {"backend": "sqlite_fts"},
        }
    )

    assert save_count == 1
    assert updated == {
        "appearance": {
            "language": "en",
            "chat_width": "comfortable",
            "chat_working_mode": "normal",
        },
        "subagents": {
            "max_subagent_depth": 6,
            "max_subagents_per_turn": 12,
            "subagent_timeout_minutes": 90,
        },
        "compaction": {
            "enabled": False,
            "trigger": {"type": "context_ratio", "threshold": 0.9},
            "strategy": {
                "type": "summary_tail",
                "tail_tokens": 12_000,
                "summary_model": None,
            },
        },
        "recall": {"backend": "sqlite_fts"},
    }
    assert storage.load_settings() == {
        "appearance": {
            "language": "en",
            "chat_width": "comfortable",
            "chat_working_mode": "normal",
        },
        "compaction": {
            "enabled": False,
            "trigger": {"type": "context_ratio", "threshold": 0.9},
            "strategy": {
                "type": "summary_tail",
                "tail_tokens": 12_000,
                "summary_model": None,
            },
        },
        "max_subagent_depth": 6,
        "max_subagents_per_turn": 12,
        "recall": {"backend": "sqlite_fts"},
        "server_port": 8500,
        "subagent_timeout_minutes": 90,
    }


def test_update_settings_sections_leaves_file_unchanged_when_section_fails(
    tmp_path: Path,
) -> None:
    storage = StorageManager(tmp_path)
    original_settings = {"server_port": 8500}
    storage.save_settings(original_settings)

    with pytest.raises(StorageError):
        storage.update_settings_sections(
            {
                "appearance": {"language": "en"},
                "compaction": {"trigger": {"type": "context_ratio", "threshold": 2}},
            }
        )

    assert storage.load_settings() == original_settings


def test_update_appearance_settings_persists_language_and_preserves_other_settings(
    tmp_path: Path,
) -> None:
    storage = StorageManager(tmp_path)
    storage.save_settings({"server_port": 8500, "appearance": {}})

    updated = storage.update_settings_sections({"appearance": {"language": "en"}})

    assert updated["appearance"] == {
        "language": "en",
        "chat_width": "comfortable",
        "chat_working_mode": "normal",
    }
    assert storage.load_settings() == {
        "appearance": {
            "language": "en",
            "chat_width": "comfortable",
            "chat_working_mode": "normal",
        },
        "server_port": 8500,
    }


def test_update_appearance_settings_persists_chat_display_preferences(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)
    storage.save_settings({"appearance": {}})

    updated = storage.update_settings_sections(
        {
            "appearance": {
                "language": "en",
                "chat_width": "wide",
                "chat_working_mode": "compact",
            }
        }
    )

    assert updated["appearance"] == {
        "language": "en",
        "chat_width": "wide",
        "chat_working_mode": "compact",
    }
    assert storage.load_appearance_settings() == updated["appearance"]


def test_update_appearance_settings_coerces_unknown_chat_width_to_default(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)

    updated = storage.update_settings_sections(
        {"appearance": {"language": "en", "chat_width": "bogus"}}
    )

    assert updated["appearance"] == {
        "language": "en",
        "chat_width": "comfortable",
        "chat_working_mode": "normal",
    }


def test_update_appearance_settings_drops_deprecated_appearance_keys(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)
    storage.save_settings(
        {
            "server_port": 8500,
            "appearance": {
                "language": "en",
                "show_token_counts": False,
                "theme": "dark",
            },
        }
    )

    updated = storage.update_settings_sections({"appearance": {"language": "en"}})

    assert updated["appearance"] == {
        "language": "en",
        "chat_width": "comfortable",
        "chat_working_mode": "normal",
    }
    assert storage.load_settings() == {
        "appearance": {
            "language": "en",
            "chat_width": "comfortable",
            "chat_working_mode": "normal",
        },
        "server_port": 8500,
    }


@pytest.mark.parametrize(
    ("appearance", "_message"),
    [
        ("en", "Appearance settings must be a mapping"),
        ({}, "Appearance settings must include language"),
        ({"language": "en", "show_token_counts": False}, "Unsupported appearance settings"),
        ({"language": ""}, "Appearance language must be a non-empty string"),
        ({"language": "fr"}, "Unsupported appearance language: fr"),
    ],
)
def test_update_appearance_settings_rejects_invalid_payloads(
    tmp_path: Path,
    appearance: Any,
    _message: str,
) -> None:
    storage = StorageManager(tmp_path)

    with pytest.raises(StorageError):
        storage.update_settings_sections({"appearance": appearance})


def test_update_skill_directory_settings_persists_list_and_preserves_other_settings(
    tmp_path: Path,
) -> None:
    storage = StorageManager(tmp_path)
    storage.save_settings({"server_port": 8500, "appearance": {"language": "en"}})
    absolute_skills = tmp_path / "team-skills"

    updated = storage.update_settings_sections(
        {"skills": {"directories": ["~/skills", f" {absolute_skills} "]}}
    )

    assert updated["skills"]["directories"] == ["~/skills", str(absolute_skills)]
    assert storage.load_skill_directory_settings() == ["~/skills", str(absolute_skills)]
    assert storage.load_settings() == {
        "appearance": {"language": "en"},
        "server_port": 8500,
        "skill_directories": ["~/skills", str(absolute_skills)],
    }


@pytest.mark.parametrize(
    ("directories", "_message"),
    [
        ("~/skills", "settings.skill_directories must be a list"),
        ([""], "Skill directories must be non-empty strings"),
        ([1], "Skill directories must be non-empty strings"),
        (["relative/skills"], "absolute paths or home-relative paths"),
        (["./skills"], "absolute paths or home-relative paths"),
    ],
)
def test_update_skill_directory_settings_rejects_invalid_payloads(
    tmp_path: Path,
    directories: Any,
    _message: str,
) -> None:
    storage = StorageManager(tmp_path)

    with pytest.raises(StorageError):
        storage.update_settings_sections({"skills": {"directories": directories}})


def test_update_skill_directory_settings_accepts_windows_absolute_paths(
    tmp_path: Path,
) -> None:
    storage = StorageManager(tmp_path)

    updated = storage.update_settings_sections({"skills": {"directories": ["C:/skills/team"]}})

    assert updated["skills"]["directories"] == ["C:/skills/team"]
