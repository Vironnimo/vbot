"""Curated public Settings shapes; canonical parsers remain the write authority.

These describe the structured update protocol, not the raw settings file. Dynamic
Extension configuration and Task Model options deliberately delegate their nested
schema to the owning catalog named in each description.
"""

from core.fetch_config import WEB_FETCH_MODES, WEB_FETCH_PROVIDERS
from core.model_tasks.constants import (
    SUPPORTED_TASK_TYPES,
    SUPPORTED_TRANSCRIPTION_AUDIO_FORMATS,
    SUPPORTED_TRANSCRIPTION_AUDIO_PROFILES,
    SUPPORTED_TRANSCRIPTION_AUDIO_SAMPLE_RATES,
)
from core.search_config import FIRST_PARTY_WEB_SEARCH_PROVIDERS
from core.settings.agent_defaults import AGENT_DEFAULT_FIELDS
from core.settings.settings import NOTIFICATION_FIELDS, SUBAGENT_SETTING_FIELDS

from .common import JsonObject, array, boolean, enum, integer, nullable, obj, params, string
from .conversation import AGENT_FIELDS

TASK_OPTIONS = {
    **obj(additional=True),
    "description": (
        "Target-owned options; query task_model.options with task_type and "
        "target for fields, types and defaults."
    ),
}
TASK_BINDING = {
    **obj(
        {
            "target": string("Task Model target id from task_model.list_targets."),
            "options": TASK_OPTIONS,
            "fallbacks": {
                **array(obj({"target": string(), "options": TASK_OPTIONS}, ("target",))),
                "maxItems": 8,
            },
        }
    ),
    "minProperties": 1,
}
MODEL_TASKS = obj(dict.fromkeys(sorted(SUPPORTED_TASK_TYPES), TASK_BINDING))

_LAYOUT_ID = {**string(), "minLength": 1, "maxLength": 512}
UI_LAYOUT = nullable(
    {
        **obj(
            additional={
                **obj(
                    {
                        "hidden": {**array(_LAYOUT_ID), "maxItems": 2048},
                        "order": {**array(_LAYOUT_ID), "maxItems": 2048},
                        "replacements": {
                            **obj(additional={**string(), "maxLength": 512}),
                            "propertyNames": _LAYOUT_ID,
                            "maxProperties": 2048,
                        },
                    },
                    additional=True,
                ),
                "description": (
                    "Contribution layout for one surface; unknown keys are preserved for "
                    "future clients."
                ),
            }
        ),
        "maxProperties": 256,
        "description": (
            "Shared surface layout, at most 1 MiB of JSON; null uses the built-in layout."
        ),
    }
)
_ROUTING_POLICY = obj(
    {
        "mode": enum("automatic", "allowed", "ordered"),
        "providers": array(string()),
        "blocked": array(string()),
        "allow_fallbacks": boolean(),
    }
)

SETTINGS_SECTIONS: JsonObject = {
    "appearance": obj(
        {
            "language": enum("en", "de"),
            "chat_width": enum("comfortable", "wide", "full"),
            "chat_working_mode": enum("normal", "compact"),
            "ui_layout": UI_LAYOUT,
        }
    ),
    "archive": obj({"retention_days": nullable(integer(1, 3650))}),
    "debug": obj(
        {
            "enabled": boolean(),
            "trace_limit": integer(1, 500),
            "speech_recording_limit": integer(1, 500),
        }
    ),
    "server": obj({"keep_awake": boolean(), "timezone": string("IANA timezone name.")}),
    "skills": obj({"directories": array(string())}, ("directories",)),
    "subagents": obj(
        {name: integer(1) for name in SUBAGENT_SETTING_FIELDS}, tuple(SUBAGENT_SETTING_FIELDS)
    ),
    "compaction": obj(
        {
            "enabled": boolean(),
            "trigger": {
                "oneOf": [
                    obj(
                        {
                            "type": enum("context_ratio"),
                            "threshold": {"type": "number", "exclusiveMinimum": 0, "maximum": 1},
                            "tokens": integer(1),
                        },
                        ("type", "threshold"),
                    ),
                    obj({"type": enum("input_tokens"), "tokens": integer(1)}, ("type", "tokens")),
                ]
            },
            "strategy": {
                "oneOf": [
                    obj(
                        {
                            "type": enum("summary_tail"),
                            "tail_tokens": integer(1),
                            "summary_model": nullable(string()),
                        },
                        ("type", "tail_tokens", "summary_model"),
                    ),
                    obj({"type": enum("continuation")}, ("type",)),
                ]
            },
        },
        ("enabled", "trigger", "strategy"),
    ),
    "defaults": obj(
        {
            "agent": obj(
                {name: nullable(AGENT_FIELDS[name]) for name in sorted(AGENT_DEFAULT_FIELDS)}
            )
        },
        ("agent",),
    ),
    "recall": obj({"backend": {**string(), "pattern": "^[a-z][a-z0-9_]*$"}}, ("backend",)),
    "model_tasks": MODEL_TASKS,
    "providers": {
        **obj(
            {
                "openrouter": obj(
                    {
                        "routing": obj(
                            {"default": _ROUTING_POLICY, "models": obj(additional=_ROUTING_POLICY)}
                        )
                    },
                    ("routing",),
                ),
                "claude_agent": obj({"executable_path": string()}),
            }
        ),
        "minProperties": 1,
    },
    "web_search": obj(
        {
            "provider": enum(*sorted(FIRST_PARTY_WEB_SEARCH_PROVIDERS)),
            "default_count": integer(1, 20),
            "searxng": obj({"base_url": string()}, ("base_url",)),
        },
        ("provider",),
    ),
    "web_fetch": obj(
        {
            "provider": enum(*WEB_FETCH_PROVIDERS),
            "mode": enum(*WEB_FETCH_MODES),
            "browser": boolean(),
        }
    ),
    "extensions": {
        **obj(
            {
                "disabled": array({**string(), "minLength": 1}),
                "config": obj(
                    additional={
                        **obj(additional=True),
                        "description": (
                            "Public config only; fields are declared by extensions.list "
                            "settings_schema. Secret fields are rejected."
                        ),
                    }
                ),
            }
        ),
        "description": (
            "Complete Extension section: omitted disabled/config becomes empty. Use "
            "settings.patch for individual paths."
        ),
    },
    "reflection": obj(
        {
            "enabled": boolean(),
            "memory_turn_interval": integer(1),
            "skill_model_step_interval": integer(1),
        }
    ),
    "librarian": obj(
        {
            "enabled": boolean(),
            "consolidate": boolean(),
            "interval_days": integer(1, 3650),
            "archive_after_days": integer(1, 3650),
        }
    ),
    "local_models": obj(
        {"context_windows": obj(additional=nullable(integer(1)))}, ("context_windows",)
    ),
    "session_titles": obj({"enabled": boolean(), "model": string()}, ("enabled",)),
    "speech": obj(
        {
            "transcription_audio": obj(
                {
                    "profile": enum(*sorted(SUPPORTED_TRANSCRIPTION_AUDIO_PROFILES)),
                    "format": enum(*sorted(SUPPORTED_TRANSCRIPTION_AUDIO_FORMATS)),
                    "sample_rate_hz": enum(*sorted(SUPPORTED_TRANSCRIPTION_AUDIO_SAMPLE_RATES)),
                },
                ("profile", "format", "sample_rate_hz"),
            )
        },
        ("transcription_audio",),
    ),
    "notifications": obj({name: boolean() for name in NOTIFICATION_FIELDS}),
}

SETTINGS_UPDATE = {
    **params({**SETTINGS_SECTIONS, "base": nullable(obj(SETTINGS_SECTIONS))}),
    "anyOf": [{"required": [name]} for name in SETTINGS_SECTIONS],
}
SETTINGS_PATCH = params(
    {
        "operations": {
            **array(
                {
                    "oneOf": [
                        obj(
                            {
                                "op": enum("set"),
                                "path": string(
                                    "Public path from settings.catalog; bracket notation "
                                    "supports dynamic keys."
                                ),
                                "value": {
                                    "description": (
                                        "Value validated by this path's type and constraints from "
                                        "settings.catalog."
                                    )
                                },
                            },
                            ("op", "path", "value"),
                        ),
                        obj({"op": enum("unset"), "path": string()}, ("op", "path")),
                    ]
                }
            ),
            "minItems": 1,
        }
    },
    ("operations",),
)
