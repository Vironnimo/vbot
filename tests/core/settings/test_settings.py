"""Tests for the ``settings.update`` section schema and stored-section normalization."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

import pytest

from core.settings import SettingsValidationError, is_valid_agent_id, parse_settings_update
from core.settings.agent_defaults import normalize_agent_default_value
from core.settings.normalizers import (
    REFLECTION_SETTING_DEFAULTS,
    normalize_appearance_settings,
    normalize_compaction_settings,
    normalize_librarian_settings,
    normalize_notification_settings,
    normalize_reflection_settings,
)
from core.utils.errors import StorageError


def test_parse_settings_update_normalizes_all_supported_sections() -> None:
    finite_values = [None, True, -2, 1.25, {"number": 1e300}]
    parsed = parse_settings_update(
        {
            "appearance": {"language": "en", "chat_width": "wide", "chat_working_mode": "compact"},
            "skills": {"directories": ["~/skills", " C:/skills/team "]},
            "subagents": {
                "max_subagent_depth": 6,
                "max_active_subagents": 12,
                "max_active_subagents_total": 40,
            },
            "compaction": {
                "enabled": False,
                "trigger": {"type": "context_ratio", "threshold": 1, "tokens": 200_000},
                "strategy": {
                    "type": "summary_tail",
                    "tail_tokens": 12_000,
                    "summary_model": "openai/gpt-5.2",
                },
            },
            "defaults": {
                "agent": {
                    "model": "openai/gpt-5.2",
                    "fallback_models": ["openai/gpt-5.1"],
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
                    "options": {"language": "auto", "nested": finite_values},
                }
            },
            "session_titles": {"enabled": True, "model": " openai/gpt-4.1-mini::api-key "},
            "speech": {
                "transcription_audio": {
                    "profile": "custom",
                    "format": "flac",
                    "sample_rate_hz": 24_000,
                }
            },
            "extensions": {
                "disabled": [" legacy ", "old"],
                "config": {"guard_bash": {"deny": ["rm -rf"], "nested": finite_values}},
            },
            "debug": {"enabled": True, "trace_limit": 100},
            "archive": {"retention_days": 14},
            "reflection": {
                "enabled": True,
                "memory_turn_interval": 5,
                "skill_model_step_interval": 40,
            },
            "librarian": {
                "enabled": False,
                "interval_days": 14,
                "archive_after_days": 30,
                "consolidate": False,
            },
            # ``null`` marks a context window for removal.
            "local_models": {
                "context_windows": {"ollama/ministral-3:8b": 16384, "ollama/old:1b": None}
            },
            "server": {"keep_awake": True, "timezone": "Europe/Berlin"},
            "notifications": {"run_completed": False, "server_stopped": True},
        }
    )

    assert parsed == {
        "appearance": {"language": "en", "chat_width": "wide", "chat_working_mode": "compact"},
        "skills": {"directories": ["~/skills", " C:/skills/team "]},
        "subagents": {
            "max_subagent_depth": 6,
            "max_active_subagents": 12,
            "max_active_subagents_total": 40,
        },
        "compaction": {
            "enabled": False,
            "trigger": {"type": "context_ratio", "threshold": 1.0, "tokens": 200_000},
            "strategy": {
                "type": "summary_tail",
                "tail_tokens": 12_000,
                "summary_model": "openai/gpt-5.2",
            },
        },
        "defaults": {
            "agent": {
                "model": "openai/gpt-5.2",
                "fallback_models": ["openai/gpt-5.1"],
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
                "options": {"language": "auto", "nested": finite_values},
            }
        },
        "session_titles": {"enabled": True, "model": "openai/gpt-4.1-mini::api-key"},
        "speech": {
            "transcription_audio": {
                "profile": "custom",
                "format": "flac",
                "sample_rate_hz": 24_000,
            }
        },
        "extensions": {
            "disabled": ["legacy", "old"],
            "config": {"guard_bash": {"deny": ["rm -rf"], "nested": finite_values}},
        },
        "debug": {"enabled": True, "trace_limit": 100},
        "archive": {"retention_days": 14},
        "reflection": {
            "enabled": True,
            "memory_turn_interval": 5,
            "skill_model_step_interval": 40,
        },
        "librarian": {
            "enabled": False,
            "interval_days": 14,
            "archive_after_days": 30,
            "consolidate": False,
        },
        "local_models": {
            "context_windows": {"ollama/ministral-3:8b": 16384, "ollama/old:1b": None}
        },
        "server": {"keep_awake": True, "timezone": "Europe/Berlin"},
        "notifications": {"run_completed": False, "server_stopped": True},
    }


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        # Sparse sections keep only the fields they name.
        pytest.param({"appearance": {"language": "en"}}, None, id="appearance-language-only"),
        pytest.param(
            {"appearance": {"language": "en", "chat_width": "comfortable"}},
            None,
            id="chat-width-comfortable",
        ),
        pytest.param(
            {"appearance": {"language": "en", "chat_width": "full"}}, None, id="chat-width-full"
        ),
        pytest.param(
            {"appearance": {"language": "en", "chat_working_mode": "normal"}},
            None,
            id="chat-working-mode-normal",
        ),
        pytest.param({"debug": {}}, None, id="empty-debug"),
        pytest.param({"debug": {"trace_limit": 1}}, None, id="trace-limit-minimum"),
        pytest.param({"debug": {"trace_limit": 500}}, None, id="trace-limit-maximum"),
        pytest.param({"archive": {}}, None, id="empty-archive"),
        pytest.param({"archive": {"retention_days": 1}}, None, id="retention-minimum"),
        pytest.param({"archive": {"retention_days": 3650}}, None, id="retention-maximum"),
        # ``null`` keeps archived items until they are deleted.
        pytest.param({"archive": {"retention_days": None}}, None, id="retention-disabled"),
        pytest.param({"reflection": {}}, None, id="empty-reflection"),
        pytest.param({"reflection": {"memory_turn_interval": 3}}, None, id="one-interval"),
        pytest.param({"librarian": {"interval_days": 1}}, None, id="librarian-one-field"),
        pytest.param({"server": {}}, None, id="empty-server"),
        pytest.param({"notifications": {}}, None, id="empty-notifications"),
        pytest.param({"local_models": {"context_windows": {}}}, None, id="no-context-windows"),
        # The extensions section is a full replacement.
        pytest.param(
            {"extensions": {}},
            {"extensions": {"disabled": [], "config": {}}},
            id="extensions-default-to-empty",
        ),
    ],
)
def test_parse_settings_update_keeps_the_semantics_of_each_section(
    params: dict[str, Any], expected: dict[str, Any] | None
) -> None:
    assert parse_settings_update(params) == (params if expected is None else expected)


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


def _routing(routing: dict[str, Any]) -> dict[str, Any]:
    return {"providers": {"openrouter": {"routing": routing}}}


def _compaction_threshold(threshold: object) -> dict[str, Any]:
    return {
        "compaction": {
            "enabled": True,
            "trigger": {"type": "context_ratio", "threshold": threshold},
            "strategy": {"type": "continuation"},
        }
    }


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({}, "settings.update requires a section"),
        ({"general": {}}, "unsupported settings sections: general"),
        ({"appearance": []}, "params.appearance must be an object"),
        # A partial section never resets the language.
        ({"appearance": {}}, "params.appearance.language must be a non-empty string"),
        (
            {"appearance": {"language": "en", "chat_width": "huge"}},
            "params.appearance.chat_width must be one of: comfortable, full, wide",
        ),
        # A container value is rejected, not looked up.
        (
            {"appearance": {"language": "en", "chat_width": []}},
            "params.appearance.chat_width must be one of",
        ),
        (
            {"appearance": {"language": "en", "chat_working_mode": "dense"}},
            "params.appearance.chat_working_mode must be one of: compact, normal",
        ),
        (
            {"appearance": {"language": "en", "theme": "dark"}},
            "unsupported appearance settings: theme",
        ),
        ({"session_titles": []}, "params.session_titles must be an object"),
        ({"session_titles": {"enabled": "yes"}}, "params.session_titles.enabled must be a boolean"),
        (
            {"session_titles": {"enabled": True, "model": 5}},
            "params.session_titles.model must be a string",
        ),
        ({"skills": []}, "params.skills must be an object"),
        ({"skills": {"extra": []}}, "unsupported skills settings: extra"),
        ({"skills": {"directories": [1]}}, "params.skills.directories must be a list of strings"),
        ({"subagents": []}, "params.subagents must be an object"),
        ({"subagents": {"extra": 1}}, "unsupported sub-agent settings: extra"),
        (
            {
                "subagents": {
                    "max_subagent_depth": 0,
                    "max_active_subagents": 8,
                    "max_active_subagents_total": 50,
                }
            },
            "params.subagents.max_subagent_depth must be a positive integer",
        ),
        (
            {"subagents": {"max_subagent_depth": 4}},
            "missing sub-agent settings: max_active_subagents, max_active_subagents_total",
        ),
        (_compaction_threshold(1.5), "params.compaction.trigger.threshold must be in (0, 1]"),
        # Bounds are checked before float conversion; NaN never compares in range.
        (_compaction_threshold(10**400), "params.compaction.trigger.threshold must be in (0, 1]"),
        (
            _compaction_threshold(float("nan")),
            "params.compaction.trigger.threshold must be in (0, 1]",
        ),
        (
            {"defaults": {"agent": {"unknown_field": True}}},
            "unsupported defaults.agent settings: unknown_field",
        ),
        (
            {"defaults": {"agent": {"temperature": 10**400}}},
            "params.defaults.agent.temperature must be between 0 and 2",
        ),
        ({"recall": []}, "params.recall must be an object"),
        ({"recall": {"extra": True}}, "unsupported recall settings: extra"),
        ({"recall": {"backend": "Bad Backend"}}, "params.recall.backend must use lowercase"),
        ({"recall": {"backend": ""}}, "params.recall.backend must be a non-empty string"),
        ({"web_search": []}, "params.web_search must be an object"),
        (
            {"web_search": {"provider": "unknown"}},
            "params.web_search.provider must be one of: brave, duckduckgo, exa, firecrawl, "
            "perplexity, searxng, serper, tavily",
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
        *[
            pytest.param(
                {
                    "model_tasks": {
                        "speech_to_text": {"options": {"nested": [{"amount": float(number)}]}}
                    }
                },
                None,
                id=f"model-task-options-{number}",
            )
            for number in ["nan", "inf", "-inf"]
        ],
        (
            {"model_tasks": {"text_embedding": {"options": {"dimensions": 0}}}},
            "params.model_tasks.text_embedding dimensions must be a positive integer or null",
        ),
        (
            {"model_tasks": {"text_embedding": {"options": {"extra_options": {"input": "x"}}}}},
            "params.model_tasks.text_embedding extra_options cannot override reserved fields: "
            "input",
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
            "params.speech.transcription_audio must use format='wav' and sample_rate_hz=16000",
        ),
        ({"extensions": []}, "params.extensions must be an object"),
        ({"extensions": {"unknown": True}}, "unsupported extensions settings: unknown"),
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
        (
            _routing({"default": {"mode": "allowed", "providers": []}}),
            "params.providers.openrouter.routing.default.providers must not be empty",
        ),
        (
            _routing(
                {
                    "default": {
                        "mode": "ordered",
                        "providers": ["deepinfra/turbo"],
                        "blocked": ["deepinfra"],
                    }
                }
            ),
            "routing.default.providers contains blocked provider 'deepinfra/turbo'",
        ),
        (
            _routing(
                {
                    "default": {"blocked": ["google-vertex"]},
                    "models": {
                        "anthropic/claude-sonnet-4": {
                            "mode": "allowed",
                            "providers": ["google-vertex/europe"],
                        }
                    },
                }
            ),
            "contains globally blocked provider 'google-vertex/europe'",
        ),
        (
            _routing({"default": {"blocked": ["not a slug"]}}),
            "routing.default.blocked entries must be valid OpenRouter provider slugs",
        ),
        ({"debug": []}, "params.debug must be an object"),
        ({"debug": {"enabled": True, "b": 2, "a": 1}}, "unsupported debug settings: a, b"),
        ({"debug": {"enabled": 1}}, "params.debug.enabled must be a boolean"),
        ({"debug": {"trace_limit": True}}, "params.debug.trace_limit must be a positive integer"),
        ({"debug": {"trace_limit": 0}}, "params.debug.trace_limit must be a positive integer"),
        ({"debug": {"trace_limit": 501}}, "params.debug.trace_limit must not exceed 500"),
        ({"archive": []}, "params.archive must be an object"),
        ({"archive": {"days": 7}}, "unsupported archive settings: days"),
        *[
            pytest.param(
                {"archive": {"retention_days": value}},
                "params.archive.retention_days must be an integer from 1 to 3650, "
                "or null to keep archived items until they are deleted",
                id=f"retention-{value!r}",
            )
            for value in (0, 3651, True, "30", 7.5)
        ],
        ({"reflection": []}, "params.reflection must be an object"),
        ({"reflection": {"extra": 1}}, "unsupported reflection settings: extra"),
        ({"reflection": {"enabled": "yes"}}, "params.reflection.enabled must be a boolean"),
        (
            {"reflection": {"memory_turn_interval": "five"}},
            "params.reflection.memory_turn_interval must be a positive integer",
        ),
        (
            {"reflection": {"skill_model_step_interval": 0}},
            "params.reflection.skill_model_step_interval must be a positive integer",
        ),
        ({"librarian": []}, "params.librarian must be an object"),
        ({"librarian": {"extra": 1}}, "unsupported librarian settings: extra"),
        ({"librarian": {"consolidate": 1}}, "params.librarian.consolidate must be a boolean"),
        # The Librarian is an Agent: its Model is set on the Agent, not here.
        ({"librarian": {"model": "openai/gpt-5.2"}}, "unsupported librarian settings: model"),
        (
            {"librarian": {"archive_after_days": 0}},
            "params.librarian.archive_after_days must be an integer from 1 to 3650",
        ),
        (
            {"librarian": {"interval_days": 3651}},
            "params.librarian.interval_days must be an integer from 1 to 3650",
        ),
        ({"local_models": []}, "params.local_models must be an object"),
        ({"local_models": {}}, "params.local_models requires context_windows"),
        (
            {"local_models": {"context_windows": {}, "extra": 1}},
            "unsupported local_models settings: extra",
        ),
        (
            {"local_models": {"context_windows": []}},
            "params.local_models.context_windows must be an object",
        ),
        (
            {"local_models": {"context_windows": {"no-slash": 4096}}},
            "params.local_models.context_windows keys must be '<provider>/<model_id>' strings",
        ),
        (
            {"local_models": {"context_windows": {"ollama/m": "16384"}}},
            "params.local_models.context_windows['ollama/m'] must be a positive integer",
        ),
        ({"server": []}, "params.server must be an object"),
        ({"server": {"extra_key": 1}}, "unsupported server settings: extra_key"),
        ({"server": {"keep_awake": "yes"}}, "params.server.keep_awake must be a boolean"),
        ({"notifications": []}, "params.notifications must be an object"),
        ({"notifications": {"sound": True}}, "unsupported notifications settings: sound"),
        (
            {"notifications": {"run_failed": "no"}},
            "params.notifications.run_failed must be a boolean",
        ),
        (
            {"server": {"timezone": "Berlin"}},
            "params.server.timezone is not a known IANA timezone",
        ),
        (
            {"server": {"timezone": ""}},
            "params.server.timezone must be a non-empty IANA timezone name",
        ),
    ],
)
def test_parse_settings_update_rejects_invalid_payloads(
    params: dict[str, Any], message: str | None
) -> None:
    with pytest.raises(
        SettingsValidationError, match=re.escape(message) if message is not None else None
    ):
        parse_settings_update(params)


@pytest.mark.parametrize(
    ("section", "expected"),
    [
        pytest.param(None, REFLECTION_SETTING_DEFAULTS, id="absent"),
        pytest.param(
            {"enabled": False},
            {**REFLECTION_SETTING_DEFAULTS, "enabled": False},
            id="partial-fills-defaults",
        ),
        pytest.param(
            {"enabled": True, "memory_turn_interval": 3, "skill_model_step_interval": 7},
            {"enabled": True, "memory_turn_interval": 3, "skill_model_step_interval": 7},
            id="complete",
        ),
    ],
)
def test_stored_reflection_section_fills_defaults(
    section: dict[str, Any] | None, expected: dict[str, Any]
) -> None:
    assert REFLECTION_SETTING_DEFAULTS["enabled"] is True
    assert normalize_reflection_settings(section) == expected


def test_stored_librarian_section_fills_defaults() -> None:
    defaults = {
        "enabled": True,
        "interval_days": 7,
        "archive_after_days": 90,
        "consolidate": True,
    }

    assert normalize_librarian_settings(None) == defaults
    # The model of an earlier vBot loads without a trace, so the next write drops it.
    assert normalize_librarian_settings(
        {"consolidate": False, "interval_days": 3, "model": " openai/gpt-5.2 "}
    ) == {**defaults, "consolidate": False, "interval_days": 3}


@pytest.mark.parametrize(
    ("normalize", "value", "message"),
    [
        (normalize_reflection_settings, "on", "Expected settings.reflection to be an object"),
        (normalize_reflection_settings, {"enabled": "yes"}, "enabled must be a boolean"),
        (
            normalize_reflection_settings,
            {"memory_turn_interval": "five"},
            "memory_turn_interval must be an integer",
        ),
        (
            normalize_reflection_settings,
            {"skill_model_step_interval": True},
            "skill_model_step_interval must be an integer",
        ),
        (
            normalize_reflection_settings,
            {"memory_turn_interval": 0},
            "memory_turn_interval must be positive",
        ),
        (normalize_librarian_settings, "on", "Expected settings.librarian to be an object"),
        (normalize_librarian_settings, {"consolidate": "yes"}, "consolidate must be a boolean"),
        (normalize_librarian_settings, {"interval_days": 1.5}, "interval_days must be an integer"),
        (
            normalize_librarian_settings,
            {"archive_after_days": 3651},
            "archive_after_days must be an integer from 1 to 3650",
        ),
        (
            normalize_notification_settings,
            {"update_result": 1},
            "Notification setting update_result must be a boolean",
        ),
        (
            normalize_compaction_settings,
            _compaction_threshold(10**400)["compaction"],
            "threshold must be in (0, 1]",
        ),
        (
            normalize_compaction_settings,
            _compaction_threshold(float("nan"))["compaction"],
            "threshold must be in (0, 1]",
        ),
        (
            lambda value: normalize_agent_default_value("temperature", value),
            10**400,
            "temperature",
        ),
    ],
)
def test_stored_sections_reject_unusable_values(
    normalize: Callable[[Any], object], value: object, message: str
) -> None:
    with pytest.raises(StorageError, match=re.escape(message)):
        normalize(value)


@pytest.mark.parametrize(
    ("field", "value", "default"),
    [("chat_width", [], "comfortable"), ("chat_working_mode", {}, "normal")],
)
def test_stored_appearance_display_preferences_fall_back_to_defaults(
    field: str, value: object, default: str
) -> None:
    assert normalize_appearance_settings({"language": "en", field: value})[field] == default


@pytest.mark.parametrize(
    ("agent_id", "valid"),
    [
        ("coder", True),
        ("a", True),
        ("Agent_1", True),
        ("x-y_z", True),
        ("0", True),
        ("a" * 64, True),
        ("", False),
        (".hidden", False),
        ("../escape", False),
        ("with space", False),
        ("slash/name", False),
        ("_leading", False),
        ("-leading", False),
        ("a" * 65, False),
        (123, False),
        (None, False),
    ],
)
def test_is_valid_agent_id_accepts_only_filesystem_safe_slugs(
    agent_id: object, valid: bool
) -> None:
    assert is_valid_agent_id(agent_id) is valid
