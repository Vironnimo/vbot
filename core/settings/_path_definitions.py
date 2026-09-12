"""Settings-owned path catalog, defaults and lifecycle metadata."""

from __future__ import annotations

from typing import Any

from core.model_tasks.constants import (
    DEFAULT_TRANSCRIPTION_AUDIO_SETTINGS,
    SUPPORTED_TRANSCRIPTION_AUDIO_FORMATS,
    SUPPORTED_TRANSCRIPTION_AUDIO_PROFILES,
    SUPPORTED_TRANSCRIPTION_AUDIO_SAMPLE_RATES,
)
from core.search_config import (
    DEFAULT_SEARXNG_BASE_URL,
    DEFAULT_WEB_SEARCH_COUNT,
    DEFAULT_WEB_SEARCH_PROVIDER,
    FIRST_PARTY_WEB_SEARCH_PROVIDERS,
    MAX_WEB_SEARCH_COUNT,
    MIN_WEB_SEARCH_COUNT,
)
from core.settings._path_types import (
    _MISSING,
    APPLICATION_LIVE,
    APPLICATION_RESTART,
    DynamicRemainder,
    DynamicSegment,
    SettingDefinition,
)
from core.settings.agent_defaults import agent_default_catalog
from core.settings.normalizers import (
    COMPACTION_SETTING_DEFAULTS,
    DEBUG_SETTING_DEFAULTS,
    DEFAULT_APPEARANCE_LANGUAGE,
    DEFAULT_RECALL_SETTINGS,
    DEFAULT_SESSION_TITLE_SETTINGS,
    REFLECTION_SETTING_DEFAULTS,
    SUPPORTED_APPEARANCE_LANGUAGES,
)
from core.settings.settings import (
    DEFAULT_APPEARANCE_CHAT_WIDTH,
    DEFAULT_APPEARANCE_CHAT_WORKING_MODE,
    OPENROUTER_ROUTING_MODES,
    SUPPORTED_APPEARANCE_CHAT_WIDTHS,
    SUPPORTED_APPEARANCE_CHAT_WORKING_MODES,
    default_timezone_name,
)

DEFAULT_SERVER_PORT = 8420


DEFAULT_ATTACHMENT_MAX_SIZE_BYTES = 20_971_520


DEFAULT_SPEECH_UPLOAD_MAX_SIZE_BYTES = 104_857_600


SUBAGENT_SETTING_DEFAULTS = {
    "max_subagent_depth": 4,
    "max_subagents_per_turn": 8,
    "subagent_timeout_minutes": 60,
}


def _static(
    path: str,
    value_type: str,
    description: str,
    *,
    application: str = APPLICATION_LIVE,
    default: Any = _MISSING,
    allowed_values: tuple[Any, ...] = (),
    nullable: bool = False,
    unsettable: bool = True,
    minimum: float | None = None,
    maximum: float | None = None,
    exclusive_minimum: bool = False,
    non_empty: bool = False,
) -> SettingDefinition:
    return SettingDefinition(
        pattern=tuple(path.split(".")),
        value_type=value_type,
        description=description,
        application=application,
        default=default,
        allowed_values=allowed_values,
        nullable=nullable,
        unsettable=unsettable,
        minimum=minimum,
        maximum=maximum,
        exclusive_minimum=exclusive_minimum,
        non_empty=non_empty,
    )


_DEFINITIONS: tuple[SettingDefinition, ...] = (
    _static(
        "server.port",
        "integer",
        "TCP port used when the vBot server next starts.",
        application=APPLICATION_RESTART,
        default=DEFAULT_SERVER_PORT,
        minimum=1,
        maximum=65535,
    ),
    _static(
        "server.keep_awake",
        "boolean",
        "Prevent automatic system sleep while the server runs.",
        default=False,
    ),
    _static(
        "live_voice.enabled",
        "boolean",
        "Show the Live voice control in the sidebar.",
        default=False,
    ),
    _static(
        "server.timezone",
        "string",
        "IANA timezone used by Agents, schedules, calendars, and Accessors.",
        default=default_timezone_name(),
        non_empty=True,
    ),
    _static(
        "appearance.language",
        "string",
        "Language used by the interactive interface.",
        default=DEFAULT_APPEARANCE_LANGUAGE,
        allowed_values=tuple(sorted(SUPPORTED_APPEARANCE_LANGUAGES)),
    ),
    _static(
        "appearance.chat_width",
        "string",
        "Reading-column width used by Chat.",
        default=DEFAULT_APPEARANCE_CHAT_WIDTH,
        allowed_values=tuple(sorted(SUPPORTED_APPEARANCE_CHAT_WIDTHS)),
    ),
    _static(
        "appearance.chat_working_mode",
        "string",
        "Presentation mode for Thinking and Tool activity in Chat.",
        default=DEFAULT_APPEARANCE_CHAT_WORKING_MODE,
        allowed_values=tuple(sorted(SUPPORTED_APPEARANCE_CHAT_WORKING_MODES)),
    ),
    _static(
        "skills.directories",
        "array",
        "Additional absolute or home-relative Skill directories.",
        default=[],
    ),
    _static(
        "extensions.directories",
        "array",
        "Additional absolute or home-relative Extension directories.",
        default=[],
    ),
    _static(
        "attachments.max_size_bytes",
        "integer",
        "Maximum accepted attachment size in bytes.",
        application=APPLICATION_RESTART,
        default=DEFAULT_ATTACHMENT_MAX_SIZE_BYTES,
        minimum=1,
    ),
    _static(
        "speech.upload_max_size_bytes",
        "integer",
        "Maximum accepted speech upload size in bytes.",
        application=APPLICATION_RESTART,
        default=DEFAULT_SPEECH_UPLOAD_MAX_SIZE_BYTES,
        minimum=1,
    ),
    _static(
        "speech.transcription_audio.profile",
        "string",
        "Provider-facing transcription audio profile.",
        default=DEFAULT_TRANSCRIPTION_AUDIO_SETTINGS["profile"],
        allowed_values=tuple(sorted(SUPPORTED_TRANSCRIPTION_AUDIO_PROFILES)),
    ),
    _static(
        "speech.transcription_audio.format",
        "string",
        "Provider-facing transcription audio container and codec.",
        default=DEFAULT_TRANSCRIPTION_AUDIO_SETTINGS["format"],
        allowed_values=tuple(sorted(SUPPORTED_TRANSCRIPTION_AUDIO_FORMATS)),
    ),
    _static(
        "speech.transcription_audio.sample_rate_hz",
        "integer",
        "Provider-facing transcription audio sample rate in hertz.",
        default=DEFAULT_TRANSCRIPTION_AUDIO_SETTINGS["sample_rate_hz"],
        allowed_values=tuple(sorted(SUPPORTED_TRANSCRIPTION_AUDIO_SAMPLE_RATES)),
    ),
    *(
        _static(
            f"subagents.{field}",
            "integer",
            description,
            default=SUBAGENT_SETTING_DEFAULTS[field],
            minimum=1,
        )
        for field, description in (
            ("max_subagent_depth", "Maximum nested Sub-Agent depth."),
            ("max_subagents_per_turn", "Maximum Sub-Agents started in one turn."),
            ("subagent_timeout_minutes", "Sub-Agent execution timeout in minutes."),
        )
    ),
    _static(
        "compaction.enabled",
        "boolean",
        "Whether automatic Session Compaction is enabled.",
        default=COMPACTION_SETTING_DEFAULTS["enabled"],
    ),
    _static(
        "compaction.trigger",
        "object",
        "Complete Compaction Trigger configuration.",
        default=COMPACTION_SETTING_DEFAULTS["trigger"],
    ),
    _static(
        "compaction.trigger.type",
        "string",
        "Compaction Trigger type.",
        default="context_ratio",
        allowed_values=("context_ratio", "input_tokens"),
        unsettable=False,
    ),
    _static(
        "compaction.trigger.threshold",
        "number",
        "Context ratio that triggers Compaction.",
        default=0.8,
        minimum=0,
        maximum=1,
        exclusive_minimum=True,
    ),
    _static(
        "compaction.trigger.tokens",
        "integer",
        "Absolute input-token Trigger or optional cap for a context-ratio Trigger.",
        minimum=1,
    ),
    _static(
        "compaction.strategy",
        "object",
        "Complete Compaction Strategy configuration.",
        default=COMPACTION_SETTING_DEFAULTS["strategy"],
    ),
    _static(
        "compaction.strategy.type",
        "string",
        "Compaction Strategy type.",
        default="summary_tail",
        allowed_values=("summary_tail", "continuation"),
        unsettable=False,
    ),
    _static(
        "compaction.strategy.tail_tokens",
        "integer",
        "Tail tokens retained by the summary-tail Strategy.",
        default=15_000,
        minimum=1,
    ),
    _static(
        "compaction.strategy.summary_model",
        "string",
        "Optional Model binding used to summarize a compacted Session.",
        default=None,
        nullable=True,
    ),
    *(
        _static(
            f"defaults.agent.{field}",
            value_type,
            description,
            default=None,
            nullable=True,
            allowed_values=allowed_values,
            minimum=minimum,
            maximum=maximum,
        )
        for field, value_type, description, allowed_values, minimum, maximum in (
            agent_default_catalog()
        )
    ),
    _static(
        "recall.backend",
        "string",
        "Recall backend used by Session search.",
        default=DEFAULT_RECALL_SETTINGS["backend"],
        non_empty=True,
    ),
    _static(
        "reflection.enabled",
        "boolean",
        "Whether automatic background reflection is enabled.",
        default=REFLECTION_SETTING_DEFAULTS["enabled"],
    ),
    _static(
        "reflection.memory_turn_interval",
        "integer",
        "Visible turns between automatic Memory reflections.",
        default=REFLECTION_SETTING_DEFAULTS["memory_turn_interval"],
        minimum=1,
    ),
    _static(
        "reflection.skill_model_step_interval",
        "integer",
        "Model steps between automatic Skill reflections.",
        default=REFLECTION_SETTING_DEFAULTS["skill_model_step_interval"],
        minimum=1,
    ),
    _static(
        "web_search.provider",
        "string",
        "Provider used by the web_search Tool.",
        default=DEFAULT_WEB_SEARCH_PROVIDER,
        allowed_values=tuple(sorted(FIRST_PARTY_WEB_SEARCH_PROVIDERS)),
    ),
    _static(
        "web_search.default_count",
        "integer",
        "Default number of results returned by web_search.",
        default=DEFAULT_WEB_SEARCH_COUNT,
        minimum=MIN_WEB_SEARCH_COUNT,
        maximum=MAX_WEB_SEARCH_COUNT,
    ),
    _static(
        "web_search.searxng.base_url",
        "string",
        "Base URL of the SearXNG instance used by web_search.",
        default=DEFAULT_SEARXNG_BASE_URL,
        non_empty=True,
    ),
    _static(
        "debug.enabled",
        "boolean",
        "Whether Provider wire debug traces are recorded.",
        default=DEBUG_SETTING_DEFAULTS["enabled"],
    ),
    _static(
        "debug.trace_limit",
        "integer",
        "Maximum number of retained debug traces.",
        default=DEBUG_SETTING_DEFAULTS["trace_limit"],
        minimum=1,
        maximum=500,
    ),
    _static(
        "session_titles.enabled",
        "boolean",
        "Whether new Sessions receive an automatic title.",
        default=DEFAULT_SESSION_TITLE_SETTINGS["enabled"],
    ),
    _static(
        "session_titles.model",
        "string",
        "Optional Model binding used to generate Session titles.",
        default=DEFAULT_SESSION_TITLE_SETTINGS["model"],
    ),
    _static(
        "extensions.disabled",
        "array",
        "Complete list of disabled Extensions.",
        default=[],
    ),
    SettingDefinition(
        pattern=("extensions", "config", DynamicSegment("extension")),
        value_type="object",
        description="Complete non-secret configuration for one Extension.",
        default={},
    ),
    SettingDefinition(
        pattern=(
            "extensions",
            "config",
            DynamicSegment("extension"),
            DynamicSegment("field"),
        ),
        value_type="json",
        description="One non-secret Extension setting declared by its schema.",
    ),
    SettingDefinition(
        pattern=("providers", "connections", DynamicSegment("connection")),
        value_type="boolean",
        description="Enabled override for one Provider Connection.",
    ),
    _static(
        "providers.openrouter.routing.default",
        "object",
        "Default OpenRouter routing policy.",
    ),
    *(
        _static(
            f"providers.openrouter.routing.default.{field}",
            value_type,
            description,
            default=default,
            allowed_values=allowed_values,
        )
        for field, value_type, description, default, allowed_values in (
            (
                "mode",
                "string",
                "Default OpenRouter routing mode.",
                "automatic",
                tuple(sorted(OPENROUTER_ROUTING_MODES)),
            ),
            ("providers", "array", "Preferred or allowed OpenRouter provider slugs.", [], ()),
            ("blocked", "array", "Blocked OpenRouter provider slugs.", [], ()),
            ("allow_fallbacks", "boolean", "Whether OpenRouter may use fallbacks.", True, ()),
        )
    ),
    SettingDefinition(
        pattern=(
            "providers",
            "openrouter",
            "routing",
            "models",
            DynamicSegment("model"),
        ),
        value_type="object",
        description="Complete OpenRouter routing policy for one Model.",
    ),
    *(
        SettingDefinition(
            pattern=(
                "providers",
                "openrouter",
                "routing",
                "models",
                DynamicSegment("model"),
                field,
            ),
            value_type=value_type,
            description=description,
            allowed_values=allowed_values,
        )
        for field, value_type, description, allowed_values in (
            (
                "mode",
                "string",
                "OpenRouter routing mode for this Model.",
                tuple(sorted(OPENROUTER_ROUTING_MODES)),
            ),
            ("providers", "array", "Preferred or allowed provider slugs for this Model.", ()),
            ("blocked", "array", "Blocked provider slugs for this Model.", ()),
            ("allow_fallbacks", "boolean", "Whether this Model may use fallbacks.", ()),
        )
    ),
    SettingDefinition(
        pattern=("local_models", "context_windows", DynamicSegment("model")),
        value_type="integer",
        description="Effective context window override for one local Model.",
        minimum=1,
    ),
    SettingDefinition(
        pattern=("model_tasks", DynamicSegment("task")),
        value_type="object",
        description="Complete Task Model binding for one task type.",
    ),
    SettingDefinition(
        pattern=("model_tasks", DynamicSegment("task"), "target"),
        value_type="string",
        description="Target bound to one specialized model task.",
        non_empty=True,
    ),
    SettingDefinition(
        pattern=("model_tasks", DynamicSegment("task"), "options"),
        value_type="object",
        description="Complete option object for one Task Model binding.",
        default={},
    ),
    SettingDefinition(
        pattern=(
            "model_tasks",
            DynamicSegment("task"),
            "options",
            DynamicRemainder("option_path"),
        ),
        value_type="json",
        description="One nested Task Model option.",
    ),
)
