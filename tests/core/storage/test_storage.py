"""Tests for the storage manager."""

import os
from pathlib import Path
from typing import Any

import pytest

from core.storage import (
    DEFAULT_APPEARANCE_LANGUAGE,
    PHASE_TWO_DIRECTORIES,
    StorageError,
    StorageManager,
)


def create_prompt_resources(resources_dir: Path, *, include_compaction: bool = True) -> None:
    prompts_dir = resources_dir / "prompts"
    prompts_dir.mkdir(parents=True)
    prompt_names = ["system.md", "runtime.md", "tools.md", "channels.md", "skills.md"]
    if include_compaction:
        prompt_names.append("compaction.md")

    for name in prompt_names:
        prompts_dir.joinpath(name).write_text(f"{name} bundled", encoding="utf-8")


class ConfigWithDataDir:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir

    def get(self, key: str, default=None):
        return default


class ConfigWithValues:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values

    def get(self, key: str, default=None):
        return self.values.get(key, default)


def test_ensure_directories_creates_phase_two_structure(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)

    storage.ensure_directories()

    assert tmp_path.is_dir()
    assert all((tmp_path / directory).is_dir() for directory in PHASE_TWO_DIRECTORIES)


def test_load_environment_reads_data_dir_env_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    storage = StorageManager(tmp_path)
    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=sk-or-from-data-dir\n", encoding="utf-8")

    loaded = storage.load_environment()

    assert loaded == {"OPENROUTER_API_KEY": "sk-or-from-data-dir"}
    assert "OPENROUTER_API_KEY" not in os.environ


def test_load_environment_does_not_overwrite_existing_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-from-process")
    storage = StorageManager(tmp_path)
    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=sk-or-from-data-dir\n", encoding="utf-8")

    loaded = storage.load_environment()

    assert loaded == {"OPENROUTER_API_KEY": "sk-or-from-data-dir"}
    assert os.environ["OPENROUTER_API_KEY"] == "sk-or-from-process"


def test_build_environment_snapshot_prefers_process_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-from-process")
    monkeypatch.setenv("PROCESS_ONLY", "from-process")
    monkeypatch.delenv("DATA_ONLY", raising=False)
    storage = StorageManager(tmp_path)
    (tmp_path / ".env").write_text(
        "OPENROUTER_API_KEY=sk-or-from-data-dir\nDATA_ONLY=from-data-dir\n",
        encoding="utf-8",
    )

    snapshot = storage.build_environment_snapshot()

    assert snapshot["OPENROUTER_API_KEY"] == "sk-or-from-process"
    assert snapshot["PROCESS_ONLY"] == "from-process"
    assert snapshot["DATA_ONLY"] == "from-data-dir"


def test_set_data_dir_credential_writes_new_env_key(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)

    storage.set_data_dir_credential("OPENROUTER_API_KEY", "sk-or-test")

    assert (tmp_path / ".env").read_text(encoding="utf-8") == "OPENROUTER_API_KEY=sk-or-test\n"
    assert storage.load_environment() == {"OPENROUTER_API_KEY": "sk-or-test"}


def test_set_data_dir_credential_replaces_existing_key_and_preserves_other_lines(
    tmp_path: Path,
) -> None:
    storage = StorageManager(tmp_path)
    (tmp_path / ".env").write_text(
        "# Provider keys\nOPENROUTER_API_KEY=old\nOTHER_KEY=value\nOPENROUTER_API_KEY=duplicate\n",
        encoding="utf-8",
    )

    storage.set_data_dir_credential("OPENROUTER_API_KEY", "new")

    assert (tmp_path / ".env").read_text(encoding="utf-8") == (
        "# Provider keys\nOPENROUTER_API_KEY=new\nOTHER_KEY=value\n"
    )


@pytest.mark.parametrize("key", ["", "1BAD", "BAD-NAME", "BAD NAME"])
def test_set_data_dir_credential_rejects_invalid_env_key(tmp_path: Path, key: str) -> None:
    storage = StorageManager(tmp_path)

    with pytest.raises(StorageError):
        storage.set_data_dir_credential(key, "secret")


@pytest.mark.parametrize("value", ["", "line\nbreak", "line\rbreak"])
def test_set_data_dir_credential_rejects_invalid_value(tmp_path: Path, value: str) -> None:
    storage = StorageManager(tmp_path)

    with pytest.raises(StorageError):
        storage.set_data_dir_credential("OPENROUTER_API_KEY", value)


def test_resolves_data_dir_from_config_attribute(tmp_path: Path) -> None:
    data_dir = tmp_path / "configured"
    storage = StorageManager(config=ConfigWithDataDir(data_dir))

    assert storage.data_dir == data_dir


def test_resolves_data_dir_from_config_value(tmp_path: Path) -> None:
    data_dir = tmp_path / "from-value"
    storage = StorageManager(config=ConfigWithValues({"DATA_DIR": str(data_dir)}))

    assert storage.data_dir == data_dir


def test_load_settings_returns_empty_when_missing(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)

    assert storage.load_settings() == {}


def test_save_and_load_settings_round_trip(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)
    settings = {"port": 8420, "feature": True, "name": "vBot"}

    storage.save_settings(settings)

    assert storage.load_settings() == settings
    assert storage.settings_path.read_text(encoding="utf-8").endswith("\n")


def test_load_settings_rejects_non_object_json(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)
    storage.ensure_directories()
    storage.settings_path.write_text("[]", encoding="utf-8")

    with pytest.raises(StorageError, match="Expected a JSON object"):
        storage.load_settings()


def test_load_settings_rejects_invalid_json(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)
    storage.ensure_directories()
    storage.settings_path.write_text("{", encoding="utf-8")

    with pytest.raises(StorageError, match="Invalid JSON"):
        storage.load_settings()


def test_load_settings_rejects_invalid_schema_fields(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)
    storage.ensure_directories()
    storage.settings_path.write_text('{"compaction": {"auto": "yes"}}', encoding="utf-8")

    with pytest.raises(StorageError, match=r"\$\.compaction\.auto: must be a boolean"):
        storage.load_settings()


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
                    "fallback_model": "openai/gpt-4.1-mini",
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
            "fallback_model": "openai/gpt-4.1-mini",
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

    updated = storage.update_defaults("agent", {"temperature": None})

    assert updated == {
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
    ("values", "message"),
    [
        ({"temperature": 2.5}, "Agent default temperature must be between 0 and 2"),
        ({"thinking_effort": "ultra"}, "Agent default thinking_effort must be one of"),
        ({"model": 123}, "Agent default model must be a string"),
    ],
)
def test_update_defaults_rejects_invalid_agent_values(
    tmp_path: Path,
    values: dict[str, Any],
    message: str,
) -> None:
    storage = StorageManager(tmp_path)

    with pytest.raises(StorageError, match=message):
        storage.update_defaults("agent", values)


def test_save_settings_rejects_unserializable_values(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)

    with pytest.raises(StorageError, match="cannot be serialized"):
        storage.save_settings({"path": object()})

    assert not storage.settings_path.exists()


def test_load_appearance_settings_returns_default_language_when_missing(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)

    assert storage.load_appearance_settings() == {"language": DEFAULT_APPEARANCE_LANGUAGE}


def test_load_appearance_settings_rejects_non_object_section(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)
    storage.save_settings({"appearance": []})

    with pytest.raises(StorageError, match=r"\$\.appearance: must be an object"):
        storage.load_appearance_settings()


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


def test_load_recall_settings_defaults_to_jsonl_scan(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)

    assert storage.load_recall_settings() == {"backend": "jsonl_scan"}


def test_load_recall_settings_reads_configured_backend(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)
    storage.save_settings({"recall": {"backend": "sqlite_fts"}})

    assert storage.load_recall_settings() == {"backend": "sqlite_fts"}


def test_update_recall_settings_persists_under_recall_key(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)
    storage.save_settings({"server_port": 8500})

    updated = storage.update_recall_settings({"backend": " sqlite_fts "})

    assert updated == {"backend": "sqlite_fts"}
    assert storage.load_settings() == {
        "server_port": 8500,
        "recall": {"backend": "sqlite_fts"},
    }


def test_update_recall_settings_rejects_unsupported_fields(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)

    with pytest.raises(StorageError, match="Unsupported recall settings: unknown"):
        storage.update_recall_settings({"backend": "sqlite_fts", "unknown": True})


def test_load_recall_settings_rejects_invalid_section(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)
    storage.ensure_directories()
    storage.settings_path.write_text('{"recall": []}', encoding="utf-8")

    with pytest.raises(StorageError, match=r"\$\.recall: must be an object"):
        storage.load_recall_settings()


def test_load_compaction_settings_returns_defaults_when_missing(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)

    settings = storage.load_compaction_settings()

    assert settings == {
        "auto": True,
        "threshold": 0.8,
        "tail_tokens": 15_000,
        "summary_model": None,
    }


def test_load_compaction_settings_reads_and_normalizes_values(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)
    storage.save_settings(
        {
            "compaction": {
                "auto": False,
                "threshold": 1,
                "tail_tokens": 7_500,
                "summary_model": "openrouter/anthropic/claude-sonnet-4",
            }
        }
    )

    settings = storage.load_compaction_settings()

    assert settings == {
        "auto": False,
        "threshold": 1.0,
        "tail_tokens": 7_500,
        "summary_model": "openrouter/anthropic/claude-sonnet-4",
    }


def test_update_compaction_settings_persists_under_compaction_key(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)
    storage.save_settings(
        {
            "server_port": 8500,
            "compaction": {
                "auto": False,
                "threshold": 0.9,
                "tail_tokens": 12_000,
                "summary_model": None,
            },
        }
    )

    updated = storage.update_compaction_settings(
        {
            "tail_tokens": 8_000,
            "summary_model": "openai/gpt-4.1-mini",
        }
    )

    assert updated == {
        "auto": False,
        "threshold": 0.9,
        "tail_tokens": 8_000,
        "summary_model": "openai/gpt-4.1-mini",
    }
    assert storage.load_compaction_settings() == updated
    assert storage.load_settings() == {"compaction": updated, "server_port": 8500}


def test_update_appearance_settings_persists_language_and_preserves_other_settings(
    tmp_path: Path,
) -> None:
    storage = StorageManager(tmp_path)
    storage.save_settings({"server_port": 8500, "appearance": {}})

    updated = storage.update_appearance_settings({"language": "en"})

    assert updated == {"language": "en"}
    assert storage.load_settings() == {"appearance": {"language": "en"}, "server_port": 8500}


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

    updated = storage.update_appearance_settings({"language": "en"})

    assert updated == {"language": "en"}
    assert storage.load_settings() == {"appearance": {"language": "en"}, "server_port": 8500}


@pytest.mark.parametrize(
    ("appearance", "message"),
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
    message: str,
) -> None:
    storage = StorageManager(tmp_path)

    with pytest.raises(StorageError, match=message):
        storage.update_appearance_settings(appearance)


def test_update_skill_directory_settings_persists_list_and_preserves_other_settings(
    tmp_path: Path,
) -> None:
    storage = StorageManager(tmp_path)
    storage.save_settings({"server_port": 8500, "appearance": {"language": "en"}})
    absolute_skills = tmp_path / "team-skills"

    updated = storage.update_skill_directory_settings(["~/skills", f" {absolute_skills} "])

    assert updated == ["~/skills", str(absolute_skills)]
    assert storage.load_skill_directory_settings() == ["~/skills", str(absolute_skills)]
    assert storage.load_settings() == {
        "appearance": {"language": "en"},
        "server_port": 8500,
        "skill_directories": ["~/skills", str(absolute_skills)],
    }


@pytest.mark.parametrize(
    ("directories", "message"),
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
    message: str,
) -> None:
    storage = StorageManager(tmp_path)

    with pytest.raises(StorageError, match=message):
        storage.update_skill_directory_settings(directories)


def test_update_skill_directory_settings_accepts_windows_absolute_paths(
    tmp_path: Path,
) -> None:
    storage = StorageManager(tmp_path)

    updated = storage.update_skill_directory_settings(["C:/skills/team"])

    assert updated == ["C:/skills/team"]


def test_copy_prompt_fragments_preserves_existing_user_copy(tmp_path: Path) -> None:
    resources_dir = tmp_path / "resources"
    data_dir = tmp_path / "data"
    create_prompt_resources(resources_dir)
    storage = StorageManager(data_dir, resources_dir=resources_dir)
    storage.ensure_directories()
    (data_dir / "prompts" / "system.md").write_text("custom", encoding="utf-8")

    written_paths = storage.copy_prompt_fragments()

    assert (data_dir / "prompts" / "system.md").read_text(encoding="utf-8") == "custom"
    assert sorted(path.name for path in written_paths) == [
        "channels.md",
        "compaction.md",
        "runtime.md",
        "skills.md",
        "tools.md",
    ]


def test_copy_prompt_fragments_can_overwrite_existing_user_copy(tmp_path: Path) -> None:
    resources_dir = tmp_path / "resources"
    data_dir = tmp_path / "data"
    create_prompt_resources(resources_dir)
    storage = StorageManager(data_dir, resources_dir=resources_dir)
    storage.ensure_directories()
    (data_dir / "prompts" / "system.md").write_text("custom", encoding="utf-8")

    storage.copy_prompt_fragments(overwrite=True)

    assert (data_dir / "prompts" / "system.md").read_text(encoding="utf-8") == "system.md bundled"


def test_read_prompt_fragment_prefers_user_copy(tmp_path: Path) -> None:
    resources_dir = tmp_path / "resources"
    data_dir = tmp_path / "data"
    create_prompt_resources(resources_dir)
    storage = StorageManager(data_dir, resources_dir=resources_dir)
    storage.ensure_directories()
    (data_dir / "prompts" / "runtime.md").write_text("custom runtime", encoding="utf-8")

    assert storage.read_prompt_fragment("runtime.md") == "custom runtime"


def test_read_prompt_fragment_falls_back_to_bundled_resource(tmp_path: Path) -> None:
    resources_dir = tmp_path / "resources"
    create_prompt_resources(resources_dir)
    storage = StorageManager(tmp_path / "data", resources_dir=resources_dir)

    assert storage.read_prompt_fragment("skills.md") == "skills.md bundled"


def test_read_prompt_fragment_compaction_name_passes_allowlist_check(tmp_path: Path) -> None:
    resources_dir = tmp_path / "resources"
    create_prompt_resources(resources_dir, include_compaction=False)
    storage = StorageManager(tmp_path / "data", resources_dir=resources_dir)

    with pytest.raises(StorageError, match="Cannot read prompt fragment compaction.md"):
        storage.read_prompt_fragment("compaction.md")


def test_read_prompt_fragment_rejects_path_traversal(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)

    with pytest.raises(StorageError, match="Unsafe prompt fragment"):
        storage.read_prompt_fragment("../system.md")


def test_read_prompt_fragment_rejects_unknown_names(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)

    with pytest.raises(StorageError, match="Unknown prompt fragment"):
        storage.read_prompt_fragment("other.md")


def test_reset_prompt_fragment_overwrites_user_copy_with_bundled_content(tmp_path: Path) -> None:
    resources_dir = tmp_path / "resources"
    data_dir = tmp_path / "data"
    create_prompt_resources(resources_dir)
    storage = StorageManager(data_dir, resources_dir=resources_dir)
    storage.ensure_directories()
    (data_dir / "prompts" / "system.md").write_text("user modified content", encoding="utf-8")

    written_path = storage.reset_prompt_fragment("system.md")

    assert written_path == data_dir / "prompts" / "system.md"
    assert written_path.read_text(encoding="utf-8") == "system.md bundled"


def test_reset_prompt_fragment_rejects_unknown_name(tmp_path: Path) -> None:
    resources_dir = tmp_path / "resources"
    create_prompt_resources(resources_dir)
    storage = StorageManager(tmp_path / "data", resources_dir=resources_dir)

    with pytest.raises(StorageError, match="Unknown prompt fragment"):
        storage.reset_prompt_fragment("unknown.md")


def test_write_prompt_fragment_writes_given_content(tmp_path: Path) -> None:
    resources_dir = tmp_path / "resources"
    data_dir = tmp_path / "data"
    create_prompt_resources(resources_dir)
    storage = StorageManager(data_dir, resources_dir=resources_dir)
    storage.ensure_directories()

    written_path = storage.write_prompt_fragment("tools.md", "custom tools content")

    assert written_path == data_dir / "prompts" / "tools.md"
    assert written_path.read_text(encoding="utf-8") == "custom tools content"


def test_write_prompt_fragment_rejects_unknown_name(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path / "data")

    with pytest.raises(StorageError, match="Unknown prompt fragment"):
        storage.write_prompt_fragment("unknown.md", "content")
