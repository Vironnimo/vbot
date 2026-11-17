"""Google AI Studio (Gemini API) provider adapter.

Speaks native Gemini ``generateContent`` through the shared
:class:`GeminiCompatibleAdapter` codec. This module owns only what is specific
to Google's own endpoint: the ``/v1beta/models`` catalog shape (which also
classifies Google's task Models for their Task Model wires) and Google's
error semantics (an invalid key is HTTP 400, an exhausted daily or zero quota
is HTTP 429 like a per-minute throttle).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any, override

import httpx

from core.models.models import (
    Capabilities,
    Model,
    ReasoningCapabilities,
    text_embedding_capabilities,
)
from core.providers._chat_completions_catalog import (
    _parse_optional_int,
    _read_non_empty_string,
    _read_optional_non_empty_string,
)
from core.providers.errors import CatalogEntrySkipped, ProviderAuthError, ProviderError
from core.providers.gemini_compatible import GeminiCompatibleAdapter
from core.providers.http import classify_http_status
from core.providers.providers import ConnectionConfig

__all__ = ["GoogleAdapter"]

# ``GET /v1beta/models`` pages at 50 entries by default; 1000 is the documented
# maximum and returns the whole public lineup in one page.
_MODELS_PAGE_SIZE = "1000"

# Agents Google serves only through its Interactions API (``antigravity-*``
# answered generateContent with HTTP 400 "This model only supports
# Interactions API", live 2026-11-17; Deep Research is documented as an
# Interactions-only agent).
_INTERACTIONS_ONLY = re.compile(r"^(?:antigravity-|deep-research-)")
_TTS = re.compile(r"-tts(?:-|$)")
_TRANSCRIBE = re.compile(r"-transcribe(?:-|$)")
_IMAGE = re.compile(r"(?:-image(?:-|$)|^nano-banana|^gemini-nano-banana)")
_MUSIC = re.compile(r"^lyria-")
_VIDEO = re.compile(r"^gemini-omni-")
# Bidi Live Models that hold a voice conversation; Live transcription,
# speech-to-speech translation and robotics streaming are other jobs.
_LIVE_VOICE = re.compile(r"(?:-live(?:-|$)|-native-audio-)")
_LIVE_OTHER = re.compile(r"(?:-transcribe-|-translate-|^gemini-robotics-)")

_RETRY_DELAY = re.compile(r"^(\d+(?:\.\d+)?)s$")
# Throttle messages name the wait ("Please retry in 41.5s."); the Decisions API
# sends only that message, with a string ``code`` and no RetryInfo detail.
_RETRY_IN = re.compile(r"retry in (\d+(?:\.\d+)?)\s*s(?![a-z])", re.IGNORECASE)


class GoogleAdapter(GeminiCompatibleAdapter):
    """Native Gemini API at ``generativelanguage.googleapis.com``.

    Request rules (reasoning ladder, media, limits) come from
    ``resources/wire/google.json`` like every other Gemini route.
    """

    @classmethod
    def discovery_params(cls, *, connection: ConnectionConfig | None = None) -> dict[str, str]:
        """Request the complete Model listing in one page."""

        del connection
        return {"pageSize": _MODELS_PAGE_SIZE}

    @classmethod
    @override
    def normalize_catalog_entry(
        cls,
        raw: Mapping[str, Any],
        defaults: Mapping[str, Any] | None = None,
    ) -> Model:
        """Normalize one native ``models/<id>`` entry into a vBot Model.

        Each entry becomes the Model of the vBot task its API method and id
        family serve (Chat, embeddings, Decisions, speech, image, music, video
        or Live voice); entries without such a task are skipped. The listing
        reports limits and a ``thinking`` flag but no modalities, so a Chat
        Model's inputs stay text until models.dev enrichment widens them.
        """

        del defaults
        model_id = _read_non_empty_string(raw, "name").removeprefix("models/")
        listed = raw.get("supportedGenerationMethods")
        methods = frozenset(
            method
            for method in (listed if isinstance(listed, list) else ())
            if isinstance(method, str)
        )
        capabilities = _family_capabilities(model_id, methods, raw)
        if capabilities is None:
            raise CatalogEntrySkipped(f"Skipped model without a vBot task: {model_id}")
        return Model(
            model_id=model_id,
            name=_read_optional_non_empty_string(raw, "displayName") or model_id,
            capabilities=capabilities,
            context_window=_parse_optional_int(raw.get("inputTokenLimit")),
            max_output_tokens=_output_limit(capabilities, raw),
        )

    @override
    def _classify_http_status(
        self,
        status_code: int,
        *,
        detail: str,
        response_headers: httpx.Headers,
    ) -> None:
        _classify_google_status(status_code, detail=detail, response_headers=response_headers)

    @classmethod
    @override
    def classify_task_http_status(
        cls,
        status_code: int,
        *,
        idempotent: bool,
        extra_retryable: set[int] | None,
        detail: str,
        response_headers: httpx.Headers,
    ) -> None:
        _classify_google_status(
            status_code,
            detail=detail,
            response_headers=response_headers,
            idempotent=idempotent,
            extra_retryable=extra_retryable,
        )


def _family_capabilities(
    model_id: str, methods: frozenset[str], raw: Mapping[str, Any]
) -> Capabilities | None:
    """Map one listing entry to the capabilities of the vBot task it serves.

    The listing names the API method of each Model but no modalities, so the
    method and Google's id families decide the task. ``None`` skips the entry.
    """

    if "embedContent" in methods:
        return _embedding_capabilities()
    if "createDecision" in methods:
        return _decision_capabilities()
    if "bidiGenerateContent" in methods:
        if _LIVE_VOICE.search(model_id) and not _LIVE_OTHER.search(model_id):
            return _live_voice_capabilities()
        return None
    if "generateContent" not in methods or _INTERACTIONS_ONLY.search(model_id):
        return None
    if _TTS.search(model_id):
        return _speech_synthesis_capabilities()
    if _TRANSCRIBE.search(model_id):
        return _transcription_capabilities()
    if _IMAGE.search(model_id):
        return _image_generation_capabilities()
    if _MUSIC.search(model_id):
        return _music_capabilities()
    if _VIDEO.search(model_id):
        return _video_capabilities()
    return _chat_capabilities(raw)


def _output_limit(capabilities: Capabilities, raw: Mapping[str, Any]) -> int | None:
    """Return the text output ceiling; vector and judgment outputs have none."""

    if {"embeddings", "decisions"} & set(capabilities.output_modalities):
        return None
    return _parse_optional_int(raw.get("outputTokenLimit"))


def _chat_capabilities(raw: Mapping[str, Any]) -> Capabilities:
    thinking = raw.get("thinking")
    return Capabilities(
        vision=False,
        tools=True,
        json_mode=True,
        reasoning=ReasoningCapabilities(supported=thinking if isinstance(thinking, bool) else None),
        input_modalities=("text",),
        output_modalities=("text",),
    )


def _task_capabilities(
    inputs: tuple[str, ...],
    outputs: tuple[str, ...],
    task_types: tuple[str, ...] = (),
) -> Capabilities:
    return Capabilities(
        vision="image" in inputs,
        tools=False,
        json_mode=False,
        reasoning=ReasoningCapabilities(supported=False),
        input_modalities=inputs,
        output_modalities=outputs,
        task_types=task_types,
    )


def _embedding_capabilities() -> Capabilities:
    # Every Gemini embedding Model shortens its vector with outputDimensionality.
    return text_embedding_capabilities(("dimensions",))


def _decision_capabilities() -> Capabilities:
    """Gemini Decision 1 on ``POST /v1beta/decisions``.

    The listing reports no Decision facts, so the published ones are stated
    here (aistudio.google.com/docs/decisions-api#limits and
    /docs/pricing#gemini-decision, 2026-11-17): only input tokens are billed.
    """

    return Capabilities(
        vision=False,
        tools=False,
        json_mode=False,
        reasoning=ReasoningCapabilities(supported=False),
        input_modalities=("text",),
        output_modalities=("decisions",),
        task_options={
            "decision": {
                "limits": {
                    "max_input_tokens": 262144,
                    "max_questions": 1000,
                    "max_choices": 255,
                    "max_levels": 255,
                },
                "pricing": {"source": "google:decision", "rates": {"input": 0.1, "output": 0}},
            }
        },
    )


def _live_voice_capabilities() -> Capabilities:
    return _task_capabilities(("text", "audio"), ("text", "audio"), ("live_voice",))


def _speech_synthesis_capabilities() -> Capabilities:
    return _task_capabilities(("text",), ("speech",))


def _transcription_capabilities() -> Capabilities:
    return _task_capabilities(("audio",), ("transcription",))


def _image_generation_capabilities() -> Capabilities:
    return _task_capabilities(("text", "image"), ("text", "image"), ("image_generation",))


def _music_capabilities() -> Capabilities:
    return _task_capabilities(
        ("text", "image"), ("text", "audio"), ("music_generation", "audio_generation")
    )


def _video_capabilities() -> Capabilities:
    return _task_capabilities(("text", "image"), ("text", "video"), ("video_generation",))


def _classify_google_status(
    status_code: int,
    *,
    detail: str,
    response_headers: httpx.Headers,
    idempotent: bool = False,
    extra_retryable: set[int] | None = None,
) -> None:
    """Apply Google's error semantics on top of the shared status policy."""

    if status_code < 400:
        return
    error = _google_error(detail)
    if status_code == 400 and _error_reason(error) == "API_KEY_INVALID":
        rejected = ProviderAuthError(f"Google AI Studio rejected the API key: {detail}")
        rejected.status_code = status_code
        raise rejected
    if status_code == 429:
        if _quota_exhausted(error, detail):
            exhausted = ProviderError(
                f"Google AI Studio quota exhausted for this Model: {detail}", retryable=False
            )
            exhausted.status_code = status_code
            raise exhausted
        delay = _retry_delay_seconds(error)
        if delay is not None and "retry-after" not in response_headers:
            response_headers = httpx.Headers({**response_headers, "retry-after": delay})
    classify_http_status(
        status_code,
        idempotent=idempotent,
        extra_retryable=extra_retryable,
        detail=detail,
        response_headers=response_headers,
    )


def _google_error(detail: str) -> Mapping[str, Any]:
    """Return the ``error`` object of a Google error body inside ``detail``."""

    start = detail.find("{")
    if start < 0:
        return {}
    try:
        body = json.loads(detail[start:])
    except ValueError:
        return {}
    if isinstance(body, Mapping):
        error = body.get("error", body)
        if isinstance(error, Mapping):
            return error
    return {}


def _details(error: Mapping[str, Any], type_suffix: str) -> list[Mapping[str, Any]]:
    details = error.get("details")
    if not isinstance(details, list):
        return []
    return [
        item
        for item in details
        if isinstance(item, Mapping) and str(item.get("@type", "")).endswith(type_suffix)
    ]


def _error_reason(error: Mapping[str, Any]) -> str | None:
    for info in _details(error, "google.rpc.ErrorInfo"):
        reason = info.get("reason")
        if isinstance(reason, str):
            return reason
    return None


def _quota_exhausted(error: Mapping[str, Any], detail: str) -> bool:
    """Whether retrying cannot succeed soon: a zero limit or a daily quota."""

    if "limit: 0," in detail:
        return True
    for failure in _details(error, "google.rpc.QuotaFailure"):
        violations = failure.get("violations")
        if not isinstance(violations, list):
            continue
        for violation in violations:
            if isinstance(violation, Mapping) and "PerDay" in str(violation.get("quotaId", "")):
                return True
    return False


def _retry_delay_seconds(error: Mapping[str, Any]) -> str | None:
    for info in _details(error, "google.rpc.RetryInfo"):
        delay = info.get("retryDelay")
        if isinstance(delay, str) and (match := _RETRY_DELAY.match(delay)):
            return str(max(1, round(float(match.group(1)))))
    message = error.get("message")
    if isinstance(message, str) and (match := _RETRY_IN.search(message)):
        return str(max(1, round(float(match.group(1)))))
    return None
