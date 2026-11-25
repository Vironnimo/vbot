"""Render only route adaptations supported by an attributable pre-output rejection."""

from __future__ import annotations

import base64
import binascii
import copy
import json
import re
from collections.abc import Iterator, Mapping
from functools import cache
from typing import TYPE_CHECKING, Any

from core.providers.errors import (
    ProviderError,
    ProviderInBandError,
    ProviderRouteStreamingUnsupportedError,
    ProviderStreamingUnsupportedError,
)
from core.providers.wire_profiles import WireBinding

if TYPE_CHECKING:
    from core.attachments.images import ImageConverter

_OUTPUT_FIELDS = ("max_tokens", "max_completion_tokens")
_REJECTION = (
    r"(?:is not supported|not supported|unsupported|unrecognized|unknown|"
    r"not available|not allowed|does not support|not permitted|not enabled)"
)
_STREAM_REFUSAL = re.compile(
    r"\b(?:streaming (?:is (?:not supported|disabled|not enabled|not available)|not supported)"
    r"|(?:does not support|doesn't support) streaming|stream(?:ing)? must be (?:set to )?false)\b"
)
_IMAGE_FORMATS = ("image/webp", "image/gif")


def _field_rejected(detail: str, field: str) -> bool:
    token = rf"(?<![\w])[`'\"]?{re.escape(field)}[`'\"]?(?![\w])"
    return bool(
        re.search(
            rf"{_REJECTION} (?:request )?(?:parameter|argument|field)[ :='\"]+{token}"
            rf"|{token} (?:parameter )?{_REJECTION}",
            detail,
        )
    )


def _error_message(error: ProviderError) -> str:
    detail = str(error)
    start = detail.find("{")
    if start >= 0:
        try:
            body = json.loads(detail[start:])
        except ValueError, RecursionError:
            body = None
        if isinstance(body, dict):
            item = body.get("error", body)
            if isinstance(item, dict):
                item = item.get("message")
            if isinstance(item, str):
                return item.strip().lower()
    return re.sub(
        r"^(?:(?:provider|authentication) error: )?(?:http )?\d{3}[: ]+", "", detail.lower().strip()
    )


_ROUTE_STREAM_REFUSAL = re.compile(
    r"(?:streaming is (?:not supported|disabled|not enabled|not available)"
    r"(?: (?:for|on) this (?:model|account|endpoint))?|"
    r"(?:this|the) (?:model|account|endpoint) (?:does not|doesn't) support streaming|"
    r"your organization must be verified to stream this model)[.!]?"
)


def rejected_shape(
    error: ProviderError, payload: Mapping[str, Any], protocol: str, *, plain_available: bool = True
) -> tuple[str, int] | None:
    if (
        error.status_code not in {400, 403, 415, 422}
        and not isinstance(error, ProviderInBandError)
        and not error._in_band_request_rejection
    ):
        return None
    detail = _error_message(error)
    if (
        plain_available
        and payload.get("stream") is True
        and (_STREAM_REFUSAL.search(detail) or "must be verified to stream" in detail)
    ):
        cls = (
            ProviderRouteStreamingUnsupportedError
            if _ROUTE_STREAM_REFUSAL.fullmatch(detail)
            else ProviderStreamingUnsupportedError
        )
        raise cls(str(error)) from error
    if error.status_code == 403:
        return None
    if protocol == "chat_completions":
        if payload.get("stream_options") == {"include_usage": True} and _field_rejected(
            detail, "stream_options"
        ):
            return "stream_options", 1
        for i, field in enumerate(_OUTPUT_FIELDS):
            alternate = _OUTPUT_FIELDS[1 - i]
            if (
                type(payload.get(field)) is int
                and alternate not in payload
                and _field_rejected(detail, field)
                and re.search(rf"\b(?:use|replace (?:it )?with) [`'\"]?{alternate}\b", detail)
            ):
                return "output_field", 2 - i
    if (
        protocol in {"chat_completions", "responses"}
        and payload.get("service_tier") == "priority"
        and (
            _field_rejected(detail, "service_tier")
            or re.search(
                r"priority (?:service )?tier (?:is )?(?:not (?:supported|available|enabled)"
                r"|unsupported)",
                detail,
            )
        )
    ):
        return "service_tier", 1
    if (
        protocol == "ollama_chat"
        and payload.get("think") is False
        and re.fullmatch(
            r"(?:(?:this|the) model|(?:model )?[`'\"]?(?:registry\.ollama\.ai/library/)?"
            + re.escape(str(payload.get("model", "")).lower())
            + r"(?::latest)?[`'\"]?) (?:does not|doesn't) support thinking[.!]?",
            detail,
        )
    ):
        # A Model without thinking already satisfies Off without a switch.
        return "think", 1
    for kind in _IMAGE_FORMATS:
        escaped = re.escape(kind)
        if re.search(
            rf"(?:unsupported|unrecognized) (?:image |media |mime )?(?:format|type)["
            rf" :='\"]+{escaped}\b"
            rf"|{escaped}[`'\"]? (?:is )?(?:not supported|unsupported)\b",
            detail,
        ) and any(source == kind for _, _, source, _ in _image_parts(payload, protocol)):
            return kind, 1
    return None


def _image_parts(
    payload: Mapping[str, Any], protocol: str, *, data: bool = False
) -> Iterator[tuple[dict[str, Any], str, str, str]]:
    """Only inline input image carriers. Never visit Tool arguments or arbitrary JSON."""
    messages = payload.get({"responses": "input", "gemini": "contents"}.get(protocol, "messages"))
    if not isinstance(messages, list):
        return
    for message in messages:
        if not isinstance(message, dict):
            continue
        parts = message.get("parts" if protocol == "gemini" else "content")
        queue = list(parts) if isinstance(parts, list) else []
        while queue:
            part = queue.pop()
            if not isinstance(part, dict):
                continue
            kind = part.get("type")
            if kind == "tool_result" and isinstance(part.get("content"), list):
                queue.extend(part["content"])
            elif kind in ("image_url", "input_image"):
                source = part.get("image_url")
                carrier = source if isinstance(source, dict) else part
                field = "url" if isinstance(source, dict) else "image_url"
                url = carrier.get(field)
                if isinstance(url, str) and url.startswith("data:image/"):
                    end = url.find(";base64,", 5, 128)
                    if end != -1:
                        yield carrier, field, url[5:end], url[end + 8 :] if data else ""
            elif kind == "image" or protocol == "gemini":
                source = part.get("source") if kind == "image" else part.get("inlineData")
                mime = "media_type" if kind == "image" else "mimeType"
                if (
                    isinstance(source, dict)
                    and isinstance(source.get("data"), str)
                    and isinstance(source.get(mime), str)
                ):
                    yield source, mime, source[mime], source["data"]


@cache
def _converter() -> ImageConverter:
    from core.attachments.images import ImageConverter

    return ImageConverter()


def apply_shape(
    payload: dict[str, Any], protocol: str, changes: Mapping[str, int], supported: frozenset[str]
) -> dict[str, Any]:
    if changes.get("stream_options") and payload.get("stream_options") == {"include_usage": True}:
        payload.pop("stream_options")
    if changes.get("service_tier") and payload.get("service_tier") == "priority":
        payload.pop("service_tier")
    if changes.get("think") and protocol == "ollama_chat" and payload.get("think") is False:
        payload.pop("think", None)
    if (selected := changes.get("output_field")) in (1, 2) and protocol == "chat_completions":
        to = _OUTPUT_FIELDS[selected - 1]
        other = _OUTPUT_FIELDS[2 - selected]
        if type(payload.get(other)) is int and to not in payload:
            payload[to] = payload.pop(other)
    if "image/png" not in supported or not any(kind in changes for kind in _IMAGE_FORMATS):
        return payload
    if not any(mime in changes for _, _, mime, _ in _image_parts(payload, protocol)):
        return payload
    from core.attachments.images import ImageConversionError, ImageTarget

    original = payload
    payload = copy.deepcopy(payload)
    for carrier, field, mime, encoded in _image_parts(payload, protocol, data=True):
        if mime not in _IMAGE_FORMATS or mime not in changes:
            continue
        try:
            converted = _converter().prepare(
                base64.b64decode(encoded, validate=True),
                mime,
                ImageTarget(frozenset({"image/png"})),
            )
        except ImageConversionError, binascii.Error:
            return original
        if converted.lossy or converted.size != converted.original_size:
            return original
        data = base64.b64encode(converted.data).decode("ascii")
        if field in {"url", "image_url"}:
            carrier[field] = "data:image/png;base64," + data
        else:
            carrier[field] = "image/png"
            carrier["data"] = data

    return payload


def cached_shapes(wire: WireBinding, model_id: str, protocol: str) -> dict[str, int]:
    return {
        name: value
        for name in ("stream_options", "service_tier", "think", "output_field", *_IMAGE_FORMATS)
        if (value := wire.route_fact(model_id, name, protocol=protocol)) is not None
    }
