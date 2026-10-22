"""Generation 1 raw Settings document shape and section fields."""

from __future__ import annotations

from core.fetch_config import WEB_FETCH_FIELDS
from core.json_documents import (
    FORMAT_VERSION_FIELD,
    OPAQUE,
    JsonShape,
    json_document,
    json_map,
    json_object,
)
from core.model_tasks.constants import SUPPORTED_TASK_TYPES
from core.settings._provider_settings import (
    CUSTOM_MODEL_CAPABILITY_FIELDS,
    CUSTOM_PROVIDER_FIELDS,
    CUSTOM_PROVIDER_MODEL_FIELDS,
)
from core.settings._rules import (
    APPEARANCE_FIELDS,
    ARCHIVE_FIELDS,
    COMPACTION_FIELDS,
    DEBUG_FIELDS,
    DEFAULTS_SECTIONS,
    EXTENSIONS_FIELDS,
    LIBRARIAN_FIELDS,
    LOCAL_MODELS_FIELDS,
    MODEL_TASK_BINDING_FIELDS,
    NOTIFICATION_FIELDS,
    OPENROUTER_PROVIDER_FIELDS,
    OPENROUTER_ROUTING_FIELDS,
    OPENROUTER_ROUTING_POLICY_FIELDS,
    RECALL_FIELDS,
    REFLECTION_FIELDS,
    SESSION_TITLE_FIELDS,
    SPEECH_FIELDS,
    TRANSCRIPTION_AUDIO_FIELDS,
    WEB_SEARCH_FIELDS,
    WEB_SEARCH_SEARXNG_FIELDS,
)
from core.settings._rules import (
    MAX_TRACE_LIMIT as MAX_TRACE_LIMIT,
)
from core.settings._rules import (
    REFLECTION_INTERVAL_FIELDS as REFLECTION_INTERVAL_FIELDS,
)
from core.settings._rules import (
    SUBAGENT_SETTING_FIELDS as SUBAGENT_SETTING_FIELDS,
)
from core.settings.agent_defaults import AGENT_DEFAULT_FIELDS
from core.tools.tool_loading import TOOL_LOADING_FIELDS

SETTINGS_FORMAT_VERSION = 1


KNOWN_RAW_SETTINGS_KEYS = frozenset(
    {
        FORMAT_VERSION_FIELD,
        "PORT",
        "SERVER_PORT",
        "appearance",
        "archive",
        "attachment_max_size_bytes",
        "compaction",
        "debug",
        "defaults",
        "extension_directories",
        "extensions",
        "keep_awake",
        "librarian",
        "local_models",
        "max_active_subagents",
        "max_active_subagents_total",
        "max_subagent_depth",
        "model_tasks",
        "notifications",
        "port",
        "providers",
        "recall",
        "reflection",
        "server_port",
        "session_titles",
        "skill_directories",
        "speech",
        "speech_upload_max_size_bytes",
        "timezone",
        "web_search",
        "web_fetch",
    }
)


NOTIFICATION_FIELD_SET = frozenset(NOTIFICATION_FIELDS)


# ``librarian.model`` of an earlier vBot: loading ignores it, validation explains it.
_RETIRED_LIBRARIAN_MODEL_WARNING = (
    "no longer used: the Librarian is an Agent with its own Model now; set it with: "
    "vbot agent update librarian --model <provider/model>"
)


PROVIDERS_FIELDS = frozenset({"connections", "custom", "openrouter"})


COMPACTION_TRIGGER_FIELDS = frozenset({"type", "threshold", "tokens"})


COMPACTION_STRATEGY_FIELDS = frozenset({"type", "tail_tokens", "summary_model"})


COMPACTION_POLICY_SHAPE = json_object(
    COMPACTION_FIELDS,
    {
        "trigger": json_object(COMPACTION_TRIGGER_FIELDS),
        "strategy": json_object(COMPACTION_STRATEGY_FIELDS),
    },
)


WEB_FETCH_SHAPE = json_object(WEB_FETCH_FIELDS)


CUSTOM_PROVIDER_SHAPE = json_object(
    CUSTOM_PROVIDER_FIELDS,
    {
        "defaults": OPAQUE,
        "models": json_map(
            json_object(
                CUSTOM_PROVIDER_MODEL_FIELDS,
                {"capabilities": json_object(CUSTOM_MODEL_CAPABILITY_FIELDS)},
            )
        ),
        "wire": OPAQUE,
    },
)


_OPENROUTER_ROUTING_POLICY_SHAPE = json_object(OPENROUTER_ROUTING_POLICY_FIELDS)


OPENROUTER_ROUTING_SHAPE = json_object(
    OPENROUTER_ROUTING_FIELDS,
    {
        "default": _OPENROUTER_ROUTING_POLICY_SHAPE,
        "models": json_map(_OPENROUTER_ROUTING_POLICY_SHAPE),
    },
)


SETTINGS_SHAPE: JsonShape = json_document(
    KNOWN_RAW_SETTINGS_KEYS,
    {
        "appearance": json_object(APPEARANCE_FIELDS),
        "archive": json_object(ARCHIVE_FIELDS),
        "compaction": COMPACTION_POLICY_SHAPE,
        "debug": json_object(DEBUG_FIELDS),
        "defaults": json_object(
            DEFAULTS_SECTIONS,
            {
                "agent": json_object(
                    AGENT_DEFAULT_FIELDS,
                    {
                        "tool_loading": json_object(TOOL_LOADING_FIELDS),
                        # An unknown part makes the default invalid instead of
                        # being left out, so the owner writes the whole set.
                        "permission_rules": OPAQUE,
                    },
                )
            },
        ),
        "extensions": json_object(EXTENSIONS_FIELDS, {"config": json_map(OPAQUE)}),
        "local_models": json_object(LOCAL_MODELS_FIELDS),
        "notifications": json_object(NOTIFICATION_FIELD_SET),
        "model_tasks": json_object(
            SUPPORTED_TASK_TYPES,
            {
                task_type: json_object(MODEL_TASK_BINDING_FIELDS)
                for task_type in SUPPORTED_TASK_TYPES
            },
        ),
        "providers": json_object(
            PROVIDERS_FIELDS,
            {
                "custom": json_map(CUSTOM_PROVIDER_SHAPE),
                "openrouter": json_object(
                    OPENROUTER_PROVIDER_FIELDS, {"routing": OPENROUTER_ROUTING_SHAPE}
                ),
            },
        ),
        "recall": json_object(RECALL_FIELDS),
        "reflection": json_object(REFLECTION_FIELDS),
        "librarian": json_object(LIBRARIAN_FIELDS, retired={"model"}),
        "session_titles": json_object(SESSION_TITLE_FIELDS),
        "speech": json_object(
            SPEECH_FIELDS, {"transcription_audio": json_object(TRANSCRIPTION_AUDIO_FIELDS)}
        ),
        "web_fetch": WEB_FETCH_SHAPE,
        "web_search": json_object(
            WEB_SEARCH_FIELDS, {"searxng": json_object(WEB_SEARCH_SEARXNG_FIELDS)}
        ),
    },
)
