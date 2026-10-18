"""Provider, Task Model, local Model and speech update parsing."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.model_tasks.constants import (
    SUPPORTED_TASK_TYPES,
    SUPPORTED_TRANSCRIPTION_AUDIO_FORMATS,
    SUPPORTED_TRANSCRIPTION_AUDIO_PROFILES,
    SUPPORTED_TRANSCRIPTION_AUDIO_SAMPLE_RATES,
    TASK_TEXT_EMBEDDING,
    TRANSCRIPTION_AUDIO_PRESETS,
)
from core.model_tasks.options import TaskModelOptionValidationError, validate_text_embedding_options
from core.settings._json_settings import normalize_json_object
from core.settings._rules import (
    OPENROUTER_MODEL_ID_MAX_LENGTH,
    OPENROUTER_PROVIDER_SLUG_MAX_LENGTH,
    OPENROUTER_PROVIDER_SLUG_PATTERN,
    OPENROUTER_ROUTING_FIELDS,
    OPENROUTER_ROUTING_MODES,
    OPENROUTER_ROUTING_POLICY_FIELDS,
    JsonObject,
    SettingsValidationError,
    _positive_integer,
)
from core.utils.errors import StorageError


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
    unsupported_fields = sorted(set(value) - OPENROUTER_ROUTING_FIELDS)
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
    unsupported_fields = sorted(set(value) - OPENROUTER_ROUTING_POLICY_FIELDS)
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
            try:
                parsed_binding["options"] = normalize_json_object(
                    options, f"params.model_tasks.{task_type}.options"
                )
            except StorageError as error:
                raise SettingsValidationError(str(error)) from error

        if not parsed_binding:
            raise SettingsValidationError(
                f"params.model_tasks.{task_type} must include target or options"
            )
        parsed[task_type] = parsed_binding

    return parsed
