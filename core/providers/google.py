"""Google AI Studio (Gemini API) provider adapter.

Speaks native Gemini ``generateContent`` through the shared
:class:`GeminiCompatibleAdapter` codec. This module owns only what is specific
to Google's own endpoint: the ``/v1beta/models`` catalog shape and Google's
error semantics (an invalid key is HTTP 400, an exhausted daily or zero quota
is HTTP 429 like a per-minute throttle).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any, override

import httpx

from core.models.models import Capabilities, Model, ReasoningCapabilities
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

# Families whose generateContent output is not chat text (speech, images,
# music), plus agent ids Google serves only through its Interactions API
# (``antigravity-*`` answered generateContent with HTTP 400 "This model only
# supports Interactions API", live 2026-11-17; Deep Research is documented as
# an Interactions-only agent).
_NON_CHAT_ID = re.compile(
    r"(?:-tts(?:-|$)|-image(?:-|$)|^nano-banana|^gemini-nano-banana|^lyria-|^imagen-|^veo-"
    r"|^antigravity-|^deep-research-)"
)

_RETRY_DELAY = re.compile(r"^(\d+(?:\.\d+)?)s$")


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
        """Normalize one native ``models/<id>`` entry into a chat Model.

        Only entries that support ``generateContent`` and produce chat text are
        kept; embedding, live, speech, image and music Models are skipped. The
        listing reports limits and a ``thinking`` flag but no modalities, so
        input modalities stay text until models.dev enrichment widens them.
        """

        del defaults
        name = _read_non_empty_string(raw, "name")
        model_id = name.removeprefix("models/")
        methods = raw.get("supportedGenerationMethods")
        if not isinstance(methods, list) or "generateContent" not in methods:
            raise CatalogEntrySkipped(f"Skipped model without generateContent: {model_id}")
        if _NON_CHAT_ID.search(model_id):
            raise CatalogEntrySkipped(f"Skipped non-chat model: {model_id}")
        thinking = raw.get("thinking")
        return Model(
            model_id=model_id,
            name=_read_optional_non_empty_string(raw, "displayName") or model_id,
            capabilities=Capabilities(
                vision=False,
                tools=True,
                json_mode=True,
                reasoning=ReasoningCapabilities(
                    supported=thinking if isinstance(thinking, bool) else None
                ),
                input_modalities=("text",),
                output_modalities=("text",),
            ),
            context_window=_parse_optional_int(raw.get("inputTokenLimit")),
            max_output_tokens=_parse_optional_int(raw.get("outputTokenLimit")),
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


def _classify_google_status(
    status_code: int,
    *,
    detail: str,
    response_headers: httpx.Headers,
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
        idempotent=False,
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
    return None
