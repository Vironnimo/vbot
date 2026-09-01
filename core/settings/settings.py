"""Public Settings schema parsing and validation."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Any, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from tzlocal import get_localzone_name

from core.model_tasks import SUPPORTED_TASK_TYPES, TASK_TEXT_EMBEDDING
from core.model_tasks.constants import (
    SUPPORTED_TRANSCRIPTION_AUDIO_FORMATS,
    SUPPORTED_TRANSCRIPTION_AUDIO_PROFILES,
    SUPPORTED_TRANSCRIPTION_AUDIO_SAMPLE_RATES,
    TRANSCRIPTION_AUDIO_PRESETS,
)
from core.model_tasks.options import (
    TaskModelOptionValidationError,
    validate_text_embedding_options,
)
from core.search_config import (
    FIRST_PARTY_WEB_SEARCH_PROVIDERS,
    MAX_WEB_SEARCH_COUNT,
    MIN_WEB_SEARCH_COUNT,
)
from core.settings.agent_defaults import AGENT_DEFAULT_FIELDS, parse_agent_default_value

JsonObject = dict[str, Any]

# Structural shape of a recall backend name (lowercase snake_case). Whether a
# name actually resolves to a registered backend is a runtime concern checked
# against the recall registry (built-ins + extension backends) at the RPC layer,
# not here — the parser only enforces the shape.
RECALL_BACKEND_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")

ALLOWED_THINKING_EFFORTS = frozenset(
    {"", "none", "minimal", "low", "medium", "high", "xhigh", "max"}
)

# Upper bound for one Agent's ordered fallback-model chain. Shared by the agent
# store's save-time validation and the chat loop's defensive runtime slice; a
# bounded chain keeps a misconfigured cascade from stacking unbounded retries.
MAX_FALLBACK_MODELS = 5
# Single authority for the agent-id format (filesystem-safe slug): a leading
# letter or digit, then up to 63 more of letter/digit/hyphen/underscore. Shared
# by file-schema validation, the agent store, and prompt-fragment storage.
AGENT_ID_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
# Single authority for the project-id format (filesystem-safe slug). Same shape
# as the agent-id rule: a leading letter or digit, then up to 63 more of
# letter/digit/hyphen/underscore. Shared by the project store and the central
# file-schema validator. Lives here (not in core.projects) so validation.py can
# import it without an import cycle through the projects package.
PROJECT_ID_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
# The coding-agent ecosystems a project may declare as its single source format
# (GLOSSARY → Source Format): where its Team agents *and* its project skills come
# from. Exactly one per project — no mixing. Lives here (not in core.projects) for
# the same import-cycle reason as PROJECT_ID_PATTERN: validation.py needs it, and
# core.projects imports from core.settings.
PROJECT_SOURCE_FORMATS: tuple[str, ...] = ("opencode", "claude")
DEFAULT_PROJECT_SOURCE_FORMAT = "opencode"
# Tool allowlists generally use ``*`` as the all-tools wildcard, but a Project
# Tool Whitelist is a security ceiling assembled from explicit tool names. Keep
# the spelling in the low-level settings contract so both raw ``project.json``
# validation and the Project entity reject the same forbidden value without an
# import cycle through ``core.tools``.
PROJECT_TOOL_ALLOWLIST_WILDCARD = "*"
MIN_TEMPERATURE = 0.0
MAX_TEMPERATURE = 2.0
# Appearance chat-width preference (the WebUI chat reading-column width). The
# constant lives here, not in normalizers.py, so the public update parser can
# validate membership without a circular import (normalizers imports settings).
DEFAULT_APPEARANCE_CHAT_WIDTH = "comfortable"
SUPPORTED_APPEARANCE_CHAT_WIDTHS = frozenset({"comfortable", "wide", "full"})
# Appearance preference for inline Thinking/Tool detail versus compact Working
# disclosures in Chat. It is display-only and applies live in accessors.
DEFAULT_APPEARANCE_CHAT_WORKING_MODE = "normal"
SUPPORTED_APPEARANCE_CHAT_WORKING_MODES = frozenset({"normal", "compact"})
SETTINGS_UPDATE_SECTIONS = frozenset(
    {
        "appearance",
        "debug",
        "server",
        "skills",
        "subagents",
        "compaction",
        "defaults",
        "recall",
        "model_tasks",
        "providers",
        "web_search",
        "extensions",
        "reflection",
        "local_models",
        "session_titles",
        "speech",
    }
)
OPENROUTER_ROUTING_MODES = frozenset({"automatic", "allowed", "ordered"})
OPENROUTER_PROVIDER_SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*(?:/[a-z0-9][a-z0-9._-]*)*$")
OPENROUTER_PROVIDER_SLUG_MAX_LENGTH = 128
OPENROUTER_MODEL_ID_MAX_LENGTH = 256
REFLECTION_INTERVAL_FIELDS = ("memory_turn_interval", "skill_model_step_interval")
SUBAGENT_SETTING_FIELDS = (
    "max_subagent_depth",
    "max_subagents_per_turn",
    "subagent_timeout_minutes",
)


class SettingsValidationError(ValueError):
    """Raised when a public Settings payload is malformed."""


def validate_timezone_name(value: Any, *, label: str) -> str:
    """Validate and normalize one IANA timezone name."""
    if not isinstance(value, str) or not value.strip():
        raise SettingsValidationError(f"{label} must be a non-empty IANA timezone name")
    normalized = value.strip()
    try:
        ZoneInfo(normalized)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise SettingsValidationError(f"{label} is not a known IANA timezone") from error
    return normalized


def default_timezone_name() -> str:
    """Return the host IANA zone used when Settings has no explicit override."""
    try:
        return validate_timezone_name(get_localzone_name(), label="system timezone")
    except (SettingsValidationError, OSError):
        return "UTC"


def available_timezone_names() -> tuple[str, ...]:
    """Return the runtime's deterministic IANA timezone catalog."""
    return tuple(sorted(available_timezones()))


def effective_timezone_name(settings: Mapping[str, Any]) -> str:
    """Resolve the configured application timezone with the host zone as default."""
    configured = settings.get("timezone")
    if configured is None:
        return default_timezone_name()
    return validate_timezone_name(configured, label="settings.timezone")


def parse_settings_update(params: Mapping[str, Any]) -> JsonObject:
    """Parse and validate a public ``settings.update`` payload."""
    unsupported_sections = sorted(set(params) - SETTINGS_UPDATE_SECTIONS)
    if unsupported_sections:
        raise SettingsValidationError(
            f"unsupported settings sections: {', '.join(unsupported_sections)}"
        )

    if not params:
        raise SettingsValidationError("settings.update requires a section")

    parsed_update: JsonObject = {}

    if "appearance" in params:
        parsed_update["appearance"] = _parse_appearance_update(params["appearance"])

    if "skills" in params:
        parsed_update["skills"] = _parse_skills_update(params["skills"])

    if "subagents" in params:
        parsed_update["subagents"] = _parse_subagents_update(params["subagents"])

    if "compaction" in params:
        parsed_update["compaction"] = _parse_compaction_update(params["compaction"])

    if "debug" in params:
        parsed_update["debug"] = _parse_debug_update(params["debug"])

    if "server" in params:
        parsed_update["server"] = _parse_server_update(params["server"])

    if "defaults" in params:
        parsed_update["defaults"] = _parse_defaults_update(params["defaults"])

    if "recall" in params:
        parsed_update["recall"] = _parse_recall_update(params["recall"])

    if "model_tasks" in params:
        parsed_update["model_tasks"] = _parse_model_tasks_update(params["model_tasks"])

    if "providers" in params:
        parsed_update["providers"] = _parse_providers_update(params["providers"])

    if "web_search" in params:
        parsed_update["web_search"] = _parse_web_search_update(params["web_search"])

    if "extensions" in params:
        parsed_update["extensions"] = _parse_extensions_update(params["extensions"])

    if "reflection" in params:
        parsed_update["reflection"] = _parse_reflection_update(params["reflection"])

    if "local_models" in params:
        parsed_update["local_models"] = _parse_local_models_update(params["local_models"])

    if "session_titles" in params:
        parsed_update["session_titles"] = _parse_session_titles_update(params["session_titles"])

    if "speech" in params:
        parsed_update["speech"] = _parse_speech_update(params["speech"])

    return parsed_update


def _parse_speech_update(speech: Any) -> JsonObject:
    if not isinstance(speech, Mapping):
        raise SettingsValidationError("params.speech must be an object")
    unsupported_fields = sorted(set(speech) - {"transcription_audio"})
    if unsupported_fields:
        raise SettingsValidationError(
            f"unsupported speech settings: {', '.join(unsupported_fields)}"
        )
    if "transcription_audio" not in speech:
        raise SettingsValidationError("params.speech requires transcription_audio")

    audio = speech["transcription_audio"]
    if not isinstance(audio, Mapping):
        raise SettingsValidationError("params.speech.transcription_audio must be an object")
    required_fields = {"profile", "format", "sample_rate_hz"}
    unsupported_audio_fields = sorted(set(audio) - required_fields)
    if unsupported_audio_fields:
        raise SettingsValidationError(
            "unsupported speech.transcription_audio settings: "
            f"{', '.join(unsupported_audio_fields)}"
        )
    missing_fields = sorted(required_fields - set(audio))
    if missing_fields:
        raise SettingsValidationError(
            f"params.speech.transcription_audio requires {', '.join(missing_fields)}"
        )

    profile = audio["profile"]
    if not isinstance(profile, str) or profile not in SUPPORTED_TRANSCRIPTION_AUDIO_PROFILES:
        allowed = ", ".join(sorted(SUPPORTED_TRANSCRIPTION_AUDIO_PROFILES))
        raise SettingsValidationError(
            f"params.speech.transcription_audio.profile must be one of: {allowed}"
        )
    audio_format = audio["format"]
    if (
        not isinstance(audio_format, str)
        or audio_format not in SUPPORTED_TRANSCRIPTION_AUDIO_FORMATS
    ):
        allowed = ", ".join(sorted(SUPPORTED_TRANSCRIPTION_AUDIO_FORMATS))
        raise SettingsValidationError(
            f"params.speech.transcription_audio.format must be one of: {allowed}"
        )
    sample_rate_hz = audio["sample_rate_hz"]
    if (
        not isinstance(sample_rate_hz, int)
        or isinstance(sample_rate_hz, bool)
        or sample_rate_hz not in SUPPORTED_TRANSCRIPTION_AUDIO_SAMPLE_RATES
    ):
        allowed = ", ".join(
            str(sample_rate) for sample_rate in sorted(SUPPORTED_TRANSCRIPTION_AUDIO_SAMPLE_RATES)
        )
        raise SettingsValidationError(
            f"params.speech.transcription_audio.sample_rate_hz must be one of: {allowed}"
        )

    preset = TRANSCRIPTION_AUDIO_PRESETS.get(profile)
    if preset is not None and (
        audio_format != preset["format"] or sample_rate_hz != preset["sample_rate_hz"]
    ):
        raise SettingsValidationError(
            f"params.speech.transcription_audio must use format={preset['format']!r} "
            f"and sample_rate_hz={preset['sample_rate_hz']} for profile {profile!r}"
        )

    return {
        "transcription_audio": {
            "profile": profile,
            "format": audio_format,
            "sample_rate_hz": sample_rate_hz,
        }
    }


def _parse_providers_update(providers: Any) -> JsonObject:
    """Parse the public Provider-settings subset.

    Connection enablement remains owned by ``connection.set_enabled``. The
    public Settings update surface accepts only OpenRouter routing policy so a
    routing save cannot accidentally replace Connection state.
    """

    if not isinstance(providers, Mapping):
        raise SettingsValidationError("params.providers must be an object")
    unsupported_fields = sorted(set(providers) - {"openrouter"})
    if unsupported_fields:
        raise SettingsValidationError(
            f"unsupported providers settings: {', '.join(unsupported_fields)}"
        )
    if "openrouter" not in providers:
        raise SettingsValidationError("params.providers requires openrouter")

    openrouter = providers["openrouter"]
    if not isinstance(openrouter, Mapping):
        raise SettingsValidationError("params.providers.openrouter must be an object")
    unsupported_openrouter_fields = sorted(set(openrouter) - {"routing"})
    if unsupported_openrouter_fields:
        raise SettingsValidationError(
            f"unsupported providers.openrouter settings: {', '.join(unsupported_openrouter_fields)}"
        )
    if "routing" not in openrouter:
        raise SettingsValidationError("params.providers.openrouter requires routing")
    return {"openrouter": {"routing": parse_openrouter_routing(openrouter["routing"])}}


def parse_openrouter_routing(value: Any) -> JsonObject:
    """Validate and normalize the complete OpenRouter routing configuration."""

    path = "params.providers.openrouter.routing"
    if not isinstance(value, Mapping):
        raise SettingsValidationError(f"{path} must be an object")
    unsupported_fields = sorted(set(value) - {"default", "models"})
    if unsupported_fields:
        raise SettingsValidationError(
            f"unsupported {path} settings: {', '.join(unsupported_fields)}"
        )

    default_policy = _parse_openrouter_routing_policy(
        value.get("default", {}),
        path=f"{path}.default",
    )
    raw_models = value.get("models", {})
    if not isinstance(raw_models, Mapping):
        raise SettingsValidationError(f"{path}.models must be an object")

    models: JsonObject = {}
    for raw_model_id, raw_policy in raw_models.items():
        if not isinstance(raw_model_id, str):
            raise SettingsValidationError(f"{path}.models keys must be strings")
        model_id = raw_model_id.strip()
        if not model_id or len(model_id) > OPENROUTER_MODEL_ID_MAX_LENGTH:
            raise SettingsValidationError(
                f"{path}.models keys must be 1-{OPENROUTER_MODEL_ID_MAX_LENGTH} characters"
            )
        if model_id in models:
            raise SettingsValidationError(f"{path}.models contains duplicate model '{model_id}'")
        policy = _parse_openrouter_routing_policy(
            raw_policy,
            path=f"{path}.models['{model_id}']",
        )
        for provider_slug in policy["providers"]:
            if any(
                _openrouter_slug_is_blocked(provider_slug, blocked_slug)
                for blocked_slug in default_policy["blocked"]
            ):
                raise SettingsValidationError(
                    f"{path}.models['{model_id}'].providers contains globally blocked "
                    f"provider '{provider_slug}'"
                )
        models[model_id] = policy

    return {"default": default_policy, "models": models}


def _parse_openrouter_routing_policy(value: Any, *, path: str) -> JsonObject:
    if not isinstance(value, Mapping):
        raise SettingsValidationError(f"{path} must be an object")
    unsupported_fields = sorted(set(value) - {"mode", "providers", "blocked", "allow_fallbacks"})
    if unsupported_fields:
        raise SettingsValidationError(
            f"unsupported {path} settings: {', '.join(unsupported_fields)}"
        )

    mode = value.get("mode", "automatic")
    if not isinstance(mode, str) or mode not in OPENROUTER_ROUTING_MODES:
        allowed = ", ".join(sorted(OPENROUTER_ROUTING_MODES))
        raise SettingsValidationError(f"{path}.mode must be one of: {allowed}")
    providers = _parse_openrouter_provider_slugs(
        value.get("providers", []), path=f"{path}.providers"
    )
    blocked = _parse_openrouter_provider_slugs(value.get("blocked", []), path=f"{path}.blocked")
    allow_fallbacks = value.get("allow_fallbacks", True)
    if not isinstance(allow_fallbacks, bool):
        raise SettingsValidationError(f"{path}.allow_fallbacks must be a boolean")

    if mode == "automatic" and providers:
        raise SettingsValidationError(f"{path}.providers must be empty in automatic mode")
    if mode != "automatic" and not providers:
        raise SettingsValidationError(f"{path}.providers must not be empty in {mode} mode")
    for provider_slug in providers:
        if any(
            _openrouter_slug_is_blocked(provider_slug, blocked_slug) for blocked_slug in blocked
        ):
            raise SettingsValidationError(
                f"{path}.providers contains blocked provider '{provider_slug}'"
            )

    return {
        "mode": mode,
        "providers": providers,
        "blocked": blocked,
        "allow_fallbacks": allow_fallbacks,
    }


def _parse_openrouter_provider_slugs(value: Any, *, path: str) -> list[str]:
    if not isinstance(value, list):
        raise SettingsValidationError(f"{path} must be a list")

    slugs: list[str] = []
    seen: set[str] = set()
    for raw_slug in value:
        if not isinstance(raw_slug, str):
            raise SettingsValidationError(f"{path} entries must be strings")
        slug = raw_slug.strip().lower()
        if (
            not slug
            or len(slug) > OPENROUTER_PROVIDER_SLUG_MAX_LENGTH
            or OPENROUTER_PROVIDER_SLUG_PATTERN.fullmatch(slug) is None
        ):
            raise SettingsValidationError(f"{path} entries must be valid OpenRouter provider slugs")
        if slug not in seen:
            slugs.append(slug)
            seen.add(slug)
    return slugs


def _openrouter_slug_is_blocked(provider_slug: str, blocked_slug: str) -> bool:
    """Mirror OpenRouter base-slug matching for local conflict validation."""

    return provider_slug == blocked_slug or provider_slug.startswith(f"{blocked_slug}/")


def _parse_session_titles_update(session_titles: Any) -> JsonObject:
    """Parse the complete automatic Session-title settings section."""
    if not isinstance(session_titles, dict):
        raise SettingsValidationError("params.session_titles must be an object")

    unsupported_fields = sorted(set(session_titles) - {"enabled", "model"})
    if unsupported_fields:
        raise SettingsValidationError(
            f"unsupported session_titles settings: {', '.join(unsupported_fields)}"
        )

    enabled = session_titles.get("enabled")
    if not isinstance(enabled, bool):
        raise SettingsValidationError("params.session_titles.enabled must be a boolean")

    model = session_titles.get("model", "")
    if not isinstance(model, str):
        raise SettingsValidationError("params.session_titles.model must be a string")
    return {"enabled": enabled, "model": model.strip()}


def _parse_local_models_update(local_models: Any) -> JsonObject:
    """Parse the local-models section (sparse per-key merge, ``null`` removes).

    ``context_windows`` maps ``"<provider>/<model_id>"`` to the user-configured
    effective context window. A ``null`` value removes the key so the model
    falls back to the default cap; unmentioned keys are preserved by storage.
    """
    if not isinstance(local_models, dict):
        raise SettingsValidationError("params.local_models must be an object")

    unsupported_fields = sorted(set(local_models) - {"context_windows"})
    if unsupported_fields:
        raise SettingsValidationError(
            f"unsupported local_models settings: {', '.join(unsupported_fields)}"
        )
    if "context_windows" not in local_models:
        raise SettingsValidationError("params.local_models requires context_windows")

    context_windows = local_models["context_windows"]
    if not isinstance(context_windows, dict):
        raise SettingsValidationError("params.local_models.context_windows must be an object")

    parsed_windows: JsonObject = {}
    for key, value in context_windows.items():
        if not isinstance(key, str) or "/" not in key or not key.strip():
            raise SettingsValidationError(
                "params.local_models.context_windows keys must be '<provider>/<model_id>' strings"
            )
        if value is None:
            parsed_windows[key] = None
            continue
        parsed_windows[key] = _positive_integer(
            value, f"params.local_models.context_windows['{key}']"
        )
    return {"context_windows": parsed_windows}


def _parse_reflection_update(reflection: Any) -> JsonObject:
    """Parse the background-reflection section (partial update, like debug)."""
    if not isinstance(reflection, dict):
        raise SettingsValidationError("params.reflection must be an object")

    supported_fields = {"enabled", *REFLECTION_INTERVAL_FIELDS}
    unsupported_fields = sorted(set(reflection) - supported_fields)
    if unsupported_fields:
        raise SettingsValidationError(
            f"unsupported reflection settings: {', '.join(unsupported_fields)}"
        )

    parsed: JsonObject = {}
    if "enabled" in reflection:
        enabled = reflection["enabled"]
        if not isinstance(enabled, bool):
            raise SettingsValidationError("params.reflection.enabled must be a boolean")
        parsed["enabled"] = enabled
    for field in REFLECTION_INTERVAL_FIELDS:
        if field in reflection:
            parsed[field] = _positive_integer(reflection[field], f"params.reflection.{field}")
    return parsed


def _parse_extensions_update(extensions: Any) -> JsonObject:
    """Parse the restart-applied ``extensions`` section (disabled list + config).

    Full-section write: ``disabled`` and ``config`` default to empty when
    omitted, so callers send the complete section. Shape only — the runtime
    reads this at the next ``Runtime.start()`` (decision #9, restart-applied);
    deep JSON normalization of ``config`` happens in storage.
    """
    if not isinstance(extensions, dict):
        raise SettingsValidationError("params.extensions must be an object")

    unsupported_fields = sorted(set(extensions) - {"disabled", "config"})
    if unsupported_fields:
        raise SettingsValidationError(
            f"unsupported extensions settings: {', '.join(unsupported_fields)}"
        )

    disabled = extensions.get("disabled", [])
    if not isinstance(disabled, list) or not all(
        isinstance(name, str) and name.strip() for name in disabled
    ):
        raise SettingsValidationError(
            "params.extensions.disabled must be a list of non-empty strings"
        )

    config = extensions.get("config", {})
    if not isinstance(config, dict) or not all(
        isinstance(value, dict) for value in config.values()
    ):
        raise SettingsValidationError("params.extensions.config must be an object of objects")

    return {
        "disabled": [name.strip() for name in disabled],
        "config": {name: dict(value) for name, value in config.items()},
    }


def _parse_web_search_update(web_search: Any) -> JsonObject:
    if not isinstance(web_search, dict):
        raise SettingsValidationError("params.web_search must be an object")

    unsupported_fields = sorted(set(web_search) - {"provider", "default_count", "searxng"})
    if unsupported_fields:
        raise SettingsValidationError(
            f"unsupported web_search settings: {', '.join(unsupported_fields)}"
        )

    provider = web_search.get("provider")
    if not isinstance(provider, str) or provider not in FIRST_PARTY_WEB_SEARCH_PROVIDERS:
        allowed = ", ".join(sorted(FIRST_PARTY_WEB_SEARCH_PROVIDERS))
        raise SettingsValidationError(f"params.web_search.provider must be one of: {allowed}")

    parsed: JsonObject = {"provider": provider}
    if "default_count" in web_search:
        default_count = web_search["default_count"]
        if (
            isinstance(default_count, bool)
            or not isinstance(default_count, int)
            or not (MIN_WEB_SEARCH_COUNT <= default_count <= MAX_WEB_SEARCH_COUNT)
        ):
            raise SettingsValidationError(
                "params.web_search.default_count must be an integer between "
                f"{MIN_WEB_SEARCH_COUNT} and {MAX_WEB_SEARCH_COUNT}"
            )
        parsed["default_count"] = default_count
    if "searxng" in web_search:
        parsed["searxng"] = _parse_searxng_settings(web_search["searxng"])
    return parsed


def _parse_searxng_settings(searxng: Any) -> JsonObject:
    if not isinstance(searxng, dict):
        raise SettingsValidationError("params.web_search.searxng must be an object")

    unsupported_fields = sorted(set(searxng) - {"base_url"})
    if unsupported_fields:
        raise SettingsValidationError(
            f"unsupported SearXNG settings: {', '.join(unsupported_fields)}"
        )

    base_url = searxng.get("base_url")
    if not isinstance(base_url, str) or not base_url.strip():
        raise SettingsValidationError("params.web_search.searxng.base_url must be a string")
    return {"base_url": base_url.strip()}


def _parse_model_tasks_update(model_tasks: Any) -> JsonObject:
    if not isinstance(model_tasks, dict):
        raise SettingsValidationError("params.model_tasks must be an object")

    parsed: JsonObject = {}
    for task_type, raw_binding in model_tasks.items():
        if not isinstance(task_type, str) or task_type not in SUPPORTED_TASK_TYPES:
            allowed = ", ".join(sorted(SUPPORTED_TASK_TYPES))
            raise SettingsValidationError(
                f"params.model_tasks contains unsupported task type {task_type!r}; "
                f"supported: {allowed}"
            )
        if not isinstance(raw_binding, dict):
            raise SettingsValidationError(f"params.model_tasks.{task_type} must be an object")

        unsupported_fields = sorted(set(raw_binding) - {"target", "options"})
        if unsupported_fields:
            raise SettingsValidationError(
                f"unsupported model task settings for {task_type}: {', '.join(unsupported_fields)}"
            )

        parsed_binding: JsonObject = {}
        if "target" in raw_binding:
            target = raw_binding["target"]
            if not isinstance(target, str):
                raise SettingsValidationError(
                    f"params.model_tasks.{task_type}.target must be a string"
                )
            parsed_binding["target"] = target.strip()

        if "options" in raw_binding:
            options = raw_binding["options"]
            if not isinstance(options, dict):
                raise SettingsValidationError(
                    f"params.model_tasks.{task_type}.options must be an object"
                )
            if task_type == TASK_TEXT_EMBEDDING:
                try:
                    validate_text_embedding_options(options)
                except TaskModelOptionValidationError as error:
                    raise SettingsValidationError(f"params.model_tasks.{error}") from error
            parsed_binding["options"] = dict(options)

        if not parsed_binding:
            raise SettingsValidationError(
                f"params.model_tasks.{task_type} must include target or options"
            )
        parsed[task_type] = parsed_binding

    return parsed


def _parse_recall_update(recall: Any) -> JsonObject:
    if not isinstance(recall, dict):
        raise SettingsValidationError("params.recall must be an object")

    unsupported_fields = sorted(set(recall) - {"backend"})
    if unsupported_fields:
        raise SettingsValidationError(
            f"unsupported recall settings: {', '.join(unsupported_fields)}"
        )

    backend = recall.get("backend")
    if not isinstance(backend, str) or not backend.strip():
        raise SettingsValidationError("params.recall.backend must be a non-empty string")
    backend = backend.strip()
    if RECALL_BACKEND_PATTERN.fullmatch(backend) is None:
        raise SettingsValidationError("params.recall.backend must use lowercase snake_case")

    return {"backend": backend}


def _parse_defaults_update(defaults: Any) -> JsonObject:
    if not isinstance(defaults, dict):
        raise SettingsValidationError("params.defaults must be an object")

    unsupported_sections = sorted(set(defaults) - {"agent"})
    if unsupported_sections:
        raise SettingsValidationError(
            f"unsupported defaults settings: {', '.join(unsupported_sections)}"
        )

    if "agent" not in defaults:
        raise SettingsValidationError("params.defaults must include an agent object")

    raw_agent_defaults = defaults["agent"]
    if not isinstance(raw_agent_defaults, dict):
        raise SettingsValidationError("params.defaults.agent must be an object")

    unsupported_agent_fields = sorted(set(raw_agent_defaults) - AGENT_DEFAULT_FIELDS)
    if unsupported_agent_fields:
        raise SettingsValidationError(
            f"unsupported defaults.agent settings: {', '.join(unsupported_agent_fields)}"
        )

    agent_defaults: JsonObject = {}
    for field, value in raw_agent_defaults.items():
        if value is None:
            agent_defaults[field] = None
            continue
        agent_defaults[field] = parse_agent_default_value(
            field, value, label=f"params.defaults.agent.{field}"
        )

    return {"agent": agent_defaults}


def _parse_appearance_update(appearance: Any) -> JsonObject:
    if not isinstance(appearance, dict):
        raise SettingsValidationError("params.appearance must be an object")

    unsupported_fields = sorted(set(appearance) - {"language", "chat_width", "chat_working_mode"})
    if unsupported_fields:
        raise SettingsValidationError(
            f"unsupported appearance settings: {', '.join(unsupported_fields)}"
        )

    # `language` stays required so a partial update never resets it: the
    # appearance section is normalized as a whole (full replace), so callers
    # send the complete section. Display preferences are optional and validated
    # when present; missing values normalize to their defaults.
    language = appearance.get("language")
    if not isinstance(language, str) or not language:
        raise SettingsValidationError("params.appearance.language must be a non-empty string")

    parsed: JsonObject = {"language": language}

    if "chat_width" in appearance:
        chat_width = appearance.get("chat_width")
        if chat_width not in SUPPORTED_APPEARANCE_CHAT_WIDTHS:
            supported = ", ".join(sorted(SUPPORTED_APPEARANCE_CHAT_WIDTHS))
            raise SettingsValidationError(
                f"params.appearance.chat_width must be one of: {supported}"
            )
        parsed["chat_width"] = chat_width

    if "chat_working_mode" in appearance:
        chat_working_mode = appearance.get("chat_working_mode")
        if chat_working_mode not in SUPPORTED_APPEARANCE_CHAT_WORKING_MODES:
            supported = ", ".join(sorted(SUPPORTED_APPEARANCE_CHAT_WORKING_MODES))
            raise SettingsValidationError(
                f"params.appearance.chat_working_mode must be one of: {supported}"
            )
        parsed["chat_working_mode"] = chat_working_mode

    return parsed


def _parse_skills_update(skills: Any) -> JsonObject:
    if not isinstance(skills, dict):
        raise SettingsValidationError("params.skills must be an object")

    unsupported_fields = sorted(set(skills) - {"directories"})
    if unsupported_fields:
        raise SettingsValidationError(
            f"unsupported skills settings: {', '.join(unsupported_fields)}"
        )

    directories = skills.get("directories")
    if not isinstance(directories, list) or not all(
        isinstance(directory, str) for directory in directories
    ):
        raise SettingsValidationError("params.skills.directories must be a list of strings")

    return {"directories": list(directories)}


def _parse_subagents_update(subagents: Any) -> JsonObject:
    if not isinstance(subagents, dict):
        raise SettingsValidationError("params.subagents must be an object")

    supported_fields = set(SUBAGENT_SETTING_FIELDS)
    unsupported_fields = sorted(set(subagents) - supported_fields)
    if unsupported_fields:
        raise SettingsValidationError(
            f"unsupported sub-agent settings: {', '.join(unsupported_fields)}"
        )

    missing_fields = [field for field in SUBAGENT_SETTING_FIELDS if field not in subagents]
    if missing_fields:
        raise SettingsValidationError(f"missing sub-agent settings: {', '.join(missing_fields)}")

    return {
        field: _positive_integer(subagents[field], f"params.subagents.{field}")
        for field in SUBAGENT_SETTING_FIELDS
    }


def _parse_compaction_update(compaction: Any) -> JsonObject:
    if not isinstance(compaction, dict):
        raise SettingsValidationError("params.compaction must be an object")

    supported_fields = {"enabled", "trigger", "strategy"}
    unsupported_fields = sorted(set(compaction) - supported_fields)
    if unsupported_fields:
        raise SettingsValidationError(
            f"unsupported compaction settings: {', '.join(unsupported_fields)}"
        )

    required_fields = ("enabled", "trigger", "strategy")
    missing_fields = [field for field in required_fields if field not in compaction]
    if missing_fields:
        raise SettingsValidationError(f"missing compaction settings: {', '.join(missing_fields)}")

    enabled = compaction["enabled"]
    if not isinstance(enabled, bool):
        raise SettingsValidationError("params.compaction.enabled must be a boolean")
    trigger = _parse_compaction_trigger(compaction["trigger"])
    strategy = _parse_compaction_strategy(compaction["strategy"])
    return {"enabled": enabled, "trigger": trigger, "strategy": strategy}


def _parse_compaction_trigger(value: Any) -> JsonObject:
    if not isinstance(value, dict):
        raise SettingsValidationError("params.compaction.trigger must be an object")
    trigger_type = value.get("type")
    if trigger_type == "context_ratio":
        if set(value) != {"type", "threshold"}:
            raise SettingsValidationError(
                "context_ratio trigger requires exactly type and threshold"
            )
        threshold_value = value["threshold"]
        if isinstance(threshold_value, bool) or not isinstance(threshold_value, int | float):
            raise SettingsValidationError("params.compaction.trigger.threshold must be a number")
        threshold = float(threshold_value)
        if threshold <= 0 or threshold > 1:
            raise SettingsValidationError("params.compaction.trigger.threshold must be in (0, 1]")
        return {"type": trigger_type, "threshold": threshold}
    if trigger_type == "input_tokens":
        if set(value) != {"type", "tokens"}:
            raise SettingsValidationError("input_tokens trigger requires exactly type and tokens")
        return {
            "type": trigger_type,
            "tokens": _positive_integer(value["tokens"], "params.compaction.trigger.tokens"),
        }
    raise SettingsValidationError(
        "params.compaction.trigger.type must be context_ratio or input_tokens"
    )


def _parse_compaction_strategy(value: Any) -> JsonObject:
    if not isinstance(value, dict):
        raise SettingsValidationError("params.compaction.strategy must be an object")
    strategy_type = value.get("type")
    if strategy_type == "summary_tail":
        if set(value) != {"type", "tail_tokens", "summary_model"}:
            raise SettingsValidationError(
                "summary_tail strategy requires exactly type, tail_tokens, and summary_model"
            )
        summary_model = value["summary_model"]
        if summary_model is not None and not isinstance(summary_model, str):
            raise SettingsValidationError(
                "params.compaction.strategy.summary_model must be a string or null"
            )
        return {
            "type": strategy_type,
            "tail_tokens": _positive_integer(
                value["tail_tokens"], "params.compaction.strategy.tail_tokens"
            ),
            "summary_model": summary_model,
        }
    if strategy_type == "continuation":
        if set(value) != {"type"}:
            raise SettingsValidationError("continuation strategy accepts only type")
        return {"type": strategy_type}
    raise SettingsValidationError(
        "params.compaction.strategy.type must be summary_tail or continuation"
    )


def _parse_debug_update(debug: Any) -> JsonObject:
    if not isinstance(debug, dict):
        raise SettingsValidationError("params.debug must be an object")

    unsupported_fields = sorted(set(debug) - {"enabled", "trace_limit"})
    if unsupported_fields:
        raise SettingsValidationError(
            f"unsupported debug settings: {', '.join(unsupported_fields)}"
        )

    parsed: JsonObject = {}

    if "enabled" in debug:
        enabled = debug["enabled"]
        if not isinstance(enabled, bool):
            raise SettingsValidationError("params.debug.enabled must be a boolean")
        parsed["enabled"] = enabled

    if "trace_limit" in debug:
        trace_limit = _positive_integer(debug["trace_limit"], "params.debug.trace_limit")
        if trace_limit > 500:
            raise SettingsValidationError("params.debug.trace_limit must not exceed 500")
        parsed["trace_limit"] = trace_limit

    return parsed


def _parse_server_update(server: Any) -> JsonObject:
    if not isinstance(server, dict):
        raise SettingsValidationError("params.server must be an object")

    unsupported_fields = sorted(set(server) - {"keep_awake", "timezone"})
    if unsupported_fields:
        raise SettingsValidationError(
            f"unsupported server settings: {', '.join(unsupported_fields)}"
        )

    parsed: JsonObject = {}

    if "keep_awake" in server:
        keep_awake = server["keep_awake"]
        if not isinstance(keep_awake, bool):
            raise SettingsValidationError("params.server.keep_awake must be a boolean")
        parsed["keep_awake"] = keep_awake

    if "timezone" in server:
        parsed["timezone"] = validate_timezone_name(
            server["timezone"], label="params.server.timezone"
        )

    return parsed


def _positive_integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SettingsValidationError(f"{label} must be a positive integer")
    if value <= 0:
        raise SettingsValidationError(f"{label} must be a positive integer")
    return cast("int", value)


def is_valid_agent_id(value: Any) -> bool:
    """Return whether ``value`` is a filesystem-safe agent id per the canonical rule."""
    return isinstance(value, str) and AGENT_ID_PATTERN.fullmatch(value) is not None


def is_valid_project_id(value: Any) -> bool:
    """Return whether ``value`` is a filesystem-safe project id per the canonical rule."""
    return isinstance(value, str) and PROJECT_ID_PATTERN.fullmatch(value) is not None


def validate_temperature(
    value: Any,
    *,
    label: str,
    allow_none: bool = False,
) -> float | None:
    """Validate one agent ``temperature`` value against the canonical schema rules."""
    if value is None:
        if allow_none:
            return None
        raise SettingsValidationError(f"{label} must be a number")

    if isinstance(value, bool) or not isinstance(value, int | float):
        raise SettingsValidationError(f"{label} must be a number")
    temperature = float(value)
    if not math.isfinite(temperature):
        raise SettingsValidationError(f"{label} must be finite")
    if temperature < MIN_TEMPERATURE or temperature > MAX_TEMPERATURE:
        raise SettingsValidationError(
            f"{label} must be between {MIN_TEMPERATURE:g} and {MAX_TEMPERATURE:g}"
        )
    return temperature


def validate_thinking_effort(
    value: Any,
    *,
    label: str,
    allow_none: bool = False,
) -> str | None:
    """Validate one agent ``thinking_effort`` value against the canonical schema rules."""
    if value is None:
        if allow_none:
            return None
        raise SettingsValidationError(f"{label} must be a string")

    if not isinstance(value, str):
        raise SettingsValidationError(f"{label} must be a string")
    if value not in ALLOWED_THINKING_EFFORTS:
        allowed = ", ".join(repr(item) for item in sorted(ALLOWED_THINKING_EFFORTS))
        raise SettingsValidationError(f"{label} must be one of: {allowed}")
    return value
