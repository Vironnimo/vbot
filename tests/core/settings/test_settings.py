"""Tests for public Settings schema parsing."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.settings import (
    SettingsValidationError,
    SettingsValidationReport,
    is_valid_agent_id,
    parse_settings_update,
    validate_settings_file,
)


def diagnostics_as_tuples(report: SettingsValidationReport) -> list[tuple[str, str, str]]:
    return [
        (diagnostic.severity, diagnostic.path, diagnostic.message)
        for diagnostic in report.diagnostics
    ]


def test_parse_settings_update_normalizes_all_supported_sections() -> None:
    parsed = parse_settings_update(
        {
            "appearance": {"language": "en"},
            "skills": {"directories": ["~/skills", " C:/skills/team "]},
            "subagents": {
                "max_subagent_depth": 6,
                "max_subagents_per_turn": 12,
                "subagent_timeout_minutes": 90,
            },
            "compaction": {
                "enabled": False,
                "trigger": {"type": "context_ratio", "threshold": 1},
                "strategy": {
                    "type": "summary_tail",
                    "tail_tokens": 12_000,
                    "summary_model": "openai/gpt-5.2",
                },
            },
            "defaults": {
                "agent": {
                    "model": "openai/gpt-5.2",
                    "fallback_model": "openai/gpt-5.1",
                    "temperature": 1,
                    "thinking_effort": "",
                }
            },
            "recall": {"backend": "sqlite_fts"},
            "web_search": {
                "provider": "searxng",
                "default_count": 15,
                "searxng": {"base_url": "http://localhost:8888"},
            },
            "model_tasks": {
                "speech_to_text": {
                    "target": "openrouter/openai/gpt-4o-transcribe::api-key",
                    "options": {"language": "auto"},
                }
            },
            "session_titles": {
                "enabled": True,
                "model": "openai/gpt-4.1-mini::api-key",
            },
            "speech": {
                "transcription_audio": {
                    "profile": "custom",
                    "format": "flac",
                    "sample_rate_hz": 24_000,
                }
            },
        }
    )

    assert parsed == {
        "appearance": {"language": "en"},
        "skills": {"directories": ["~/skills", " C:/skills/team "]},
        "subagents": {
            "max_subagent_depth": 6,
            "max_subagents_per_turn": 12,
            "subagent_timeout_minutes": 90,
        },
        "compaction": {
            "enabled": False,
            "trigger": {"type": "context_ratio", "threshold": 1.0},
            "strategy": {
                "type": "summary_tail",
                "tail_tokens": 12_000,
                "summary_model": "openai/gpt-5.2",
            },
        },
        "defaults": {
            "agent": {
                "model": "openai/gpt-5.2",
                "fallback_model": "openai/gpt-5.1",
                "temperature": 1.0,
                "thinking_effort": "",
            }
        },
        "recall": {"backend": "sqlite_fts"},
        "web_search": {
            "provider": "searxng",
            "default_count": 15,
            "searxng": {"base_url": "http://localhost:8888"},
        },
        "model_tasks": {
            "speech_to_text": {
                "target": "openrouter/openai/gpt-4o-transcribe::api-key",
                "options": {"language": "auto"},
            }
        },
        "session_titles": {
            "enabled": True,
            "model": "openai/gpt-4.1-mini::api-key",
        },
        "speech": {
            "transcription_audio": {
                "profile": "custom",
                "format": "flac",
                "sample_rate_hz": 24_000,
            }
        },
    }


@pytest.mark.parametrize("chat_width", ["comfortable", "wide", "full"])
def test_parse_settings_update_accepts_each_supported_chat_width(chat_width: str) -> None:
    parsed = parse_settings_update({"appearance": {"language": "en", "chat_width": chat_width}})

    assert parsed == {"appearance": {"language": "en", "chat_width": chat_width}}


@pytest.mark.parametrize("chat_working_mode", ["normal", "compact"])
def test_parse_settings_update_accepts_each_supported_chat_working_mode(
    chat_working_mode: str,
) -> None:
    parsed = parse_settings_update(
        {"appearance": {"language": "en", "chat_working_mode": chat_working_mode}}
    )

    assert parsed == {"appearance": {"language": "en", "chat_working_mode": chat_working_mode}}


def test_parse_settings_update_omits_absent_chat_width() -> None:
    parsed = parse_settings_update({"appearance": {"language": "en"}})

    assert parsed == {"appearance": {"language": "en"}}


def test_parse_settings_update_normalizes_openrouter_routing() -> None:
    parsed = parse_settings_update(
        {
            "providers": {
                "openrouter": {
                    "routing": {
                        "default": {
                            "mode": "allowed",
                            "providers": [" Anthropic ", "google-vertex"],
                            "blocked": [" DeepInfra "],
                            "allow_fallbacks": False,
                        },
                        "models": {
                            "anthropic/claude-sonnet-4": {
                                "mode": "ordered",
                                "providers": ["anthropic", "amazon-bedrock/eu-west-1"],
                                "blocked": ["google-vertex"],
                                "allow_fallbacks": True,
                            }
                        },
                    }
                }
            }
        }
    )

    assert parsed["providers"] == {
        "openrouter": {
            "routing": {
                "default": {
                    "mode": "allowed",
                    "providers": ["anthropic", "google-vertex"],
                    "blocked": ["deepinfra"],
                    "allow_fallbacks": False,
                },
                "models": {
                    "anthropic/claude-sonnet-4": {
                        "mode": "ordered",
                        "providers": ["anthropic", "amazon-bedrock/eu-west-1"],
                        "blocked": ["google-vertex"],
                        "allow_fallbacks": True,
                    }
                },
            }
        }
    }


@pytest.mark.parametrize(
    ("routing", "message"),
    [
        (
            {"default": {"mode": "allowed", "providers": []}},
            "providers must not be empty",
        ),
        (
            {
                "default": {
                    "mode": "ordered",
                    "providers": ["deepinfra/turbo"],
                    "blocked": ["deepinfra"],
                }
            },
            "contains blocked provider",
        ),
        (
            {
                "default": {"blocked": ["google-vertex"]},
                "models": {
                    "anthropic/claude-sonnet-4": {
                        "mode": "allowed",
                        "providers": ["google-vertex/europe"],
                    }
                },
            },
            "globally blocked provider",
        ),
        (
            {"default": {"blocked": ["not a slug"]}},
            "valid OpenRouter provider slugs",
        ),
    ],
)
def test_parse_settings_update_rejects_conflicting_openrouter_routing(
    routing: dict,
    message: str,
) -> None:
    with pytest.raises(SettingsValidationError, match=message):
        parse_settings_update({"providers": {"openrouter": {"routing": routing}}})


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({}, "settings.update requires a section"),
        ({"general": {}}, "unsupported settings sections: general"),
        ({"appearance": []}, "params.appearance must be an object"),
        ({"session_titles": []}, "params.session_titles must be an object"),
        (
            {"session_titles": {"enabled": "yes"}},
            "params.session_titles.enabled must be a boolean",
        ),
        (
            {"session_titles": {"enabled": True, "model": 5}},
            "params.session_titles.model must be a string",
        ),
        (
            {"appearance": {"language": "en", "chat_width": "huge"}},
            "params.appearance.chat_width must be one of",
        ),
        (
            {"appearance": {"language": "en", "chat_working_mode": "dense"}},
            "params.appearance.chat_working_mode must be one of",
        ),
        (
            {"appearance": {"language": "en", "theme": "dark"}},
            "unsupported appearance settings: theme",
        ),
        ({"skills": {"directories": [1]}}, "params.skills.directories"),
        (
            {
                "subagents": {
                    "max_subagent_depth": 4,
                    "max_subagents_per_turn": 8,
                }
            },
            "missing sub-agent settings: subagent_timeout_minutes",
        ),
        (
            {
                "compaction": {
                    "enabled": True,
                    "trigger": {"type": "context_ratio", "threshold": 1.5},
                    "strategy": {
                        "type": "summary_tail",
                        "tail_tokens": 15_000,
                        "summary_model": None,
                    },
                }
            },
            "params.compaction.trigger.threshold must be in",
        ),
        (
            {"defaults": {"agent": {"unknown_field": True}}},
            "unsupported defaults.agent settings: unknown_field",
        ),
        ({"recall": []}, "params.recall must be an object"),
        (
            {"recall": {"backend": "Bad Backend"}},
            "params.recall.backend must use lowercase snake_case",
        ),
        (
            {"recall": {"backend": ""}},
            "params.recall.backend must be a non-empty string",
        ),
        ({"web_search": []}, "params.web_search must be an object"),
        (
            {"web_search": {"provider": "unknown"}},
            "params.web_search.provider must be one of",
        ),
        (
            {"web_search": {"provider": "searxng", "searxng": {"base_url": ""}}},
            "params.web_search.searxng.base_url must be a string",
        ),
        (
            {"web_search": {"provider": "brave", "default_count": 0}},
            "params.web_search.default_count must be an integer between 1 and 20",
        ),
        (
            {"web_search": {"provider": "brave", "default_count": True}},
            "params.web_search.default_count must be an integer between 1 and 20",
        ),
        ({"model_tasks": []}, "params.model_tasks must be an object"),
        (
            {"model_tasks": {"speech_to_text": {"target": 1}}},
            "params.model_tasks.speech_to_text.target must be a string",
        ),
        (
            {"model_tasks": {"speech_to_text": {"options": []}}},
            "params.model_tasks.speech_to_text.options must be an object",
        ),
        (
            {"model_tasks": {"text_embedding": {"options": {"dimensions": 0}}}},
            "params.model_tasks.text_embedding.dimensions must be a positive integer or null",
        ),
        (
            {"model_tasks": {"text_embedding": {"options": {"extra_options": {"input": "wrong"}}}}},
            "params.model_tasks.text_embedding.extra_options cannot override reserved fields",
        ),
        (
            {
                "speech": {
                    "transcription_audio": {
                        "profile": "compatibility",
                        "format": "flac",
                        "sample_rate_hz": 16_000,
                    }
                }
            },
            "must use format='wav'",
        ),
    ],
)
def test_parse_settings_update_rejects_invalid_payloads(
    params: dict,
    message: str,
) -> None:
    with pytest.raises(SettingsValidationError, match=message):
        parse_settings_update(params)


def test_parse_settings_update_normalizes_extensions_section() -> None:
    parsed = parse_settings_update(
        {
            "extensions": {
                "disabled": [" legacy ", "old"],
                "config": {"guard_bash": {"deny": ["rm -rf"]}},
            }
        }
    )

    assert parsed == {
        "extensions": {
            "disabled": ["legacy", "old"],
            "config": {"guard_bash": {"deny": ["rm -rf"]}},
        }
    }


def test_parse_settings_update_defaults_empty_extensions_fields() -> None:
    assert parse_settings_update({"extensions": {}}) == {
        "extensions": {"disabled": [], "config": {}}
    }


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({"extensions": []}, "params.extensions must be an object"),
        (
            {"extensions": {"unknown": True}},
            "unsupported extensions settings: unknown",
        ),
        (
            {"extensions": {"disabled": ["ok", ""]}},
            "params.extensions.disabled must be a list of non-empty strings",
        ),
        (
            {"extensions": {"disabled": "one"}},
            "params.extensions.disabled must be a list of non-empty strings",
        ),
        (
            {"extensions": {"config": {"ext": "not-an-object"}}},
            "params.extensions.config must be an object of objects",
        ),
    ],
)
def test_parse_settings_update_rejects_invalid_extensions(
    params: dict,
    message: str,
) -> None:
    with pytest.raises(SettingsValidationError, match=message):
        parse_settings_update(params)


def test_validate_settings_file_accepts_missing_settings(tmp_path: Path) -> None:
    report = validate_settings_file(tmp_path / "settings.json")

    assert report.ok is True
    assert report.exists is False
    assert report.diagnostics == ()


def test_validate_settings_file_accepts_known_settings(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "server_port": 8500,
                "appearance": {
                    "language": "en",
                    "chat_width": "wide",
                    "chat_working_mode": "compact",
                },
                "skill_directories": ["~/skills"],
                "extension_directories": ["C:/vbot/extensions"],
                "attachment_max_size_bytes": 1024,
                "speech_upload_max_size_bytes": 2048,
                "speech": {
                    "transcription_audio": {
                        "profile": "custom",
                        "format": "flac",
                        "sample_rate_hz": 24_000,
                    }
                },
                "max_subagent_depth": 4,
                "max_subagents_per_turn": 8,
                "subagent_timeout_minutes": 60,
                "compaction": {
                    "enabled": True,
                    "trigger": {"type": "context_ratio", "threshold": 0.8},
                    "strategy": {
                        "type": "summary_tail",
                        "tail_tokens": 15_000,
                        "summary_model": None,
                    },
                },
                "recall": {"backend": "sqlite_fts"},
                "extensions": {
                    "disabled": ["legacy-ext"],
                    "config": {"weather": {"api_key": "x", "units": "metric"}},
                },
                "web_search": {
                    "provider": "searxng",
                    "default_count": 12,
                    "searxng": {"base_url": "http://localhost:8888"},
                },
                "defaults": {
                    "agent": {
                        "model": "openai/gpt-5.2",
                        "fallback_model": "",
                        "temperature": 0.7,
                        "thinking_effort": "medium",
                    }
                },
                "model_tasks": {
                    "speech_to_text": {
                        "target": "openrouter/openai/gpt-4o-transcribe::api-key",
                        "options": {"language": "auto"},
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    report = validate_settings_file(settings_path)

    assert report.ok is True
    assert report.exists is True
    assert report.diagnostics == ()


def test_validate_settings_file_reports_invalid_json(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text("{", encoding="utf-8")

    report = validate_settings_file(settings_path)

    assert report.ok is False
    assert diagnostics_as_tuples(report) == [
        (
            "error",
            "$",
            "Invalid JSON: Expecting property name enclosed in double quotes at line 1 column 2",
        )
    ]


def test_validate_settings_file_reports_wrong_root_type(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text("[]", encoding="utf-8")

    report = validate_settings_file(settings_path)

    assert report.ok is False
    assert diagnostics_as_tuples(report) == [("error", "$", "Expected a JSON object, got list")]


def test_validate_settings_file_reports_invalid_fields(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "server_port": 70000,
                "skill_directories": ["relative/path"],
                "attachment_max_size_bytes": 0,
                "speech_upload_max_size_bytes": 0,
                "compaction": {
                    "enabled": True,
                    "trigger": {"type": "context_ratio", "threshold": 2},
                    "strategy": {"type": "summary_tail", "tail_tokens": False},
                },
                "defaults": {"agent": {"temperature": "warm", "unknown": True}},
                "web_search": {
                    "provider": "unknown",
                    "default_count": 25,
                    "searxng": {"base_url": ""},
                },
                "model_tasks": {"speech_to_text": {"target": "", "options": []}},
                "typo": True,
            }
        ),
        encoding="utf-8",
    )

    report = validate_settings_file(settings_path)

    assert report.ok is False
    assert diagnostics_as_tuples(report) == [
        ("warning", "$.typo", "unknown settings key: typo"),
        ("error", "$.server_port", "must be between 1 and 65535"),
        ("error", "$.skill_directories[0]", "must be an absolute or home-relative path"),
        ("error", "$.attachment_max_size_bytes", "must be a positive integer"),
        ("error", "$.speech_upload_max_size_bytes", "must be a positive integer"),
        ("error", "$.compaction.trigger.threshold", "must be in (0, 1]"),
        ("error", "$.compaction.strategy.tail_tokens", "must be a positive integer"),
        (
            "error",
            "$.defaults.agent.unknown",
            "unsupported defaults.agent setting: unknown",
        ),
        ("error", "$.defaults.agent.temperature", "must be a number"),
        ("error", "$.web_search.provider", "must be one of: brave, searxng"),
        ("error", "$.web_search.default_count", "must be an integer between 1 and 20"),
        ("error", "$.web_search.searxng.base_url", "must be a non-empty string"),
        ("error", "$.model_tasks.speech_to_text.target", "must be a non-empty string"),
        ("error", "$.model_tasks.speech_to_text.options", "must be an object"),
    ]


def test_validate_settings_file_reports_invalid_chat_width(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps({"appearance": {"language": "en", "chat_width": "huge"}}),
        encoding="utf-8",
    )

    report = validate_settings_file(settings_path)

    assert report.ok is False
    assert diagnostics_as_tuples(report) == [
        (
            "error",
            "$.appearance.chat_width",
            "unsupported chat width; supported: comfortable, full, wide",
        )
    ]


def test_validate_settings_file_reports_invalid_chat_working_mode(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps({"appearance": {"language": "en", "chat_working_mode": "dense"}}),
        encoding="utf-8",
    )

    report = validate_settings_file(settings_path)

    assert report.ok is False
    assert diagnostics_as_tuples(report) == [
        (
            "error",
            "$.appearance.chat_working_mode",
            "unsupported chat working mode; supported: compact, normal",
        )
    ]


def test_validate_settings_file_reports_invalid_recall_backend(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({"recall": {"backend": "SQLite FTS"}}), encoding="utf-8")

    report = validate_settings_file(settings_path)

    assert report.ok is False
    assert diagnostics_as_tuples(report) == [
        ("error", "$.recall.backend", "must use lowercase snake_case")
    ]


def test_validate_settings_file_rejects_non_object_extensions(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({"extensions": []}), encoding="utf-8")

    report = validate_settings_file(settings_path)

    assert report.ok is False
    assert diagnostics_as_tuples(report) == [("error", "$.extensions", "must be an object")]


def test_validate_settings_file_reports_invalid_extensions_fields(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "extensions": {
                    "disabled": ["ok", "", 5],
                    "config": {"good": {}, "bad": ["x"]},
                    "weird": True,
                }
            }
        ),
        encoding="utf-8",
    )

    report = validate_settings_file(settings_path)

    assert report.ok is False
    assert diagnostics_as_tuples(report) == [
        ("warning", "$.extensions.weird", "unknown extensions field: weird"),
        ("error", "$.extensions.disabled[1]", "must be a non-empty string"),
        ("error", "$.extensions.disabled[2]", "must be a non-empty string"),
        ("error", "$.extensions.config.bad", "must be an object"),
    ]


def test_validate_settings_file_rejects_non_list_disabled_extensions(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({"extensions": {"disabled": "solo"}}), encoding="utf-8")

    report = validate_settings_file(settings_path)

    assert report.ok is False
    assert diagnostics_as_tuples(report) == [("error", "$.extensions.disabled", "must be a list")]


@pytest.mark.parametrize("agent_id", ["coder", "a", "Agent_1", "x-y_z", "0", "a" * 64])
def test_is_valid_agent_id_accepts_filesystem_safe_slugs(agent_id: str) -> None:
    assert is_valid_agent_id(agent_id) is True


@pytest.mark.parametrize(
    "agent_id",
    ["", ".hidden", "../escape", "with space", "slash/name", "_leading", "-leading", "a" * 65],
)
def test_is_valid_agent_id_rejects_unsafe_values(agent_id: str) -> None:
    assert is_valid_agent_id(agent_id) is False


def test_is_valid_agent_id_rejects_non_string() -> None:
    assert is_valid_agent_id(123) is False
    assert is_valid_agent_id(None) is False
