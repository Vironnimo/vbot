"""Provider HTTP clients for image generation task-model targets."""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping
from typing import Any, override
from urllib.parse import quote

import httpx

from core.model_tasks.image_profile import ImageWire, image_wire
from core.model_tasks.image_types import (
    GeneratedImage,
    ImageGenerationFailure,
    ImageGenerationResult,
    ImageInput,
    JsonObject,
)
from core.providers._gemini_wire import _gemini_finish_reason, _normalize_gemini_usage
from core.providers._opencode_catalog import opencode_api_base
from core.providers.adapter import TERMINAL_OUTCOME_CONTENT_FILTERED
from core.providers.errors import ProviderAuthError, ProviderContentRefusedError, ProviderError
from core.providers.http import parse_sse_json_data, split_stream_lines
from core.providers.openai import (
    CODEX_EXTRA_HEADERS,
    CODEX_RESPONSES_ENDPOINT,
    CODEX_RESPONSES_MODE,
)
from core.providers.openai_subscription_auth import extract_chatgpt_account_id
from core.providers.task_client import (
    NON_IDEMPOTENT_TASK_REQUEST_RETRY_POLICY,
    ProviderTaskClient,
    TaskRequestRetryPolicy,
    is_omittable_option,
    merge_extra_options,
)
from core.utils.logging import get_logger

_OPENROUTER_IMAGES_ENDPOINT = "/images"
_OPENAI_IMAGES_GENERATIONS_ENDPOINT = "/images/generations"
_OPENAI_IMAGES_EDITS_ENDPOINT = "/images/edits"
_DEFAULT_IMAGE_TIMEOUT = 120.0
_OPENAI_CODEX_IMAGE_TIMEOUT = 300.0
_OPENAI_CODEX_IMAGE_CARRIER_MODEL = "gpt-6-luna"
# The carrier Model sits between vBot and the image tool. Left alone it retried
# refused requests with its own rewritten prompt (a named film character became
# an original look-alike) and the calling Agent never learned of it.
_OPENAI_CODEX_IMAGE_INSTRUCTIONS = (
    "Make exactly one image_generation tool call that renders the image request in the "
    "user message. Keep its subject, named characters and content as requested; do not "
    "rewrite them. If the tool call fails, do not call the tool again; reply with one "
    "sentence that states the reason the tool reported, or that it reported none."
)
_MAX_CARRIER_REPLY_CHARS = 500
_OPENROUTER_IMAGE_RETRY_POLICY = TaskRequestRetryPolicy(
    replay_safe=False,
    verified_safe_retry_status_codes=frozenset({503}),
)
# Google documents 503 UNAVAILABLE as an overloaded service to retry with
# backoff (aistudio.google.com/docs/api-errors, read 2026-11-17); it is common
# on new Models. Other ambiguous failures may already have rendered an image.
_GOOGLE_IMAGE_RETRY_POLICY = TaskRequestRetryPolicy(
    replay_safe=False,
    verified_safe_retry_status_codes=frozenset({503}),
)
# Nano Banana renders take tens of seconds and, under load, minutes.
_GOOGLE_IMAGE_TIMEOUT = 300.0
# Text beside the image carries the Model's reason when it renders nothing.
_GOOGLE_RESPONSE_MODALITIES = ("TEXT", "IMAGE")
# Settings option -> ``generationConfig.imageConfig`` field.
_GOOGLE_IMAGE_CONFIG_KEYS: tuple[tuple[str, str], ...] = (
    ("aspect_ratio", "aspectRatio"),
    ("resolution", "imageSize"),
)
# Finish reasons with which Google renders nothing on purpose: an image
# blocked for an unnamed reason, or no image although one was expected.
_GOOGLE_DECLINED_FINISH_REASONS = frozenset({"IMAGE_OTHER", "NO_IMAGE"})
_LOGGER = get_logger("image.providers")

# OpenRouter's unified image API top-level parameters. The wire layer only
# sends a key when it is present in the merged task-model options with a
# non-empty value; absent keys are never invented, so the provider's own
# defaults stay authoritative.
_UNIFIED_IMAGE_KEYS: tuple[str, ...] = (
    "aspect_ratio",
    "resolution",
    "size",
    "quality",
    "output_format",
    "background",
    "output_compression",
    "n",
    "seed",
)

# OpenAI ``/v1/images/generations`` option keys. The wire layer sends each
# key only when the model advertises support and the user has supplied a
# value. ``model`` and ``prompt`` are always sent.
_OPENAI_IMAGE_KEYS: tuple[str, ...] = (
    "n",
    "size",
    "quality",
    "background",
    "moderation",
    "output_format",
    "output_compression",
    "style",
    "response_format",
)

# Multipart fields accepted by OpenAI's image-edit endpoint. ``input_fidelity``
# is intentionally not a curated Tool/Settings control in this iteration, but
# remains reachable through ``extra_options`` like other provider-native fields.
_OPENAI_IMAGE_EDIT_KEYS: tuple[str, ...] = (
    "n",
    "size",
    "quality",
    "background",
    "moderation",
    "output_format",
    "output_compression",
    "response_format",
)

# OpenAI subscription image generation rides through the Codex Responses wire:
# the task model remains ``gpt-image-2`` in vBot, while the request is carried
# by a subscription text model that invokes the backend image_generation tool.
_OPENAI_CODEX_IMAGE_TOOL_KEYS: tuple[str, ...] = (
    "output_format",
    "output_compression",
    "moderation",
    "background",
    "size",
    "quality",
)
_OPENAI_CODEX_IMAGE_FORBIDDEN_TOOL_KEYS: tuple[str, ...] = ("n", "model")
_OPENAI_CODEX_IMAGE_PROMPT_HINT_KEYS: tuple[str, ...] = ("size", "quality", "background")

# Option name owned by the option-schema layer that is never forwarded as a
# literal request field: it maps to the nested ``provider.options`` object.
_PROVIDER_OPTIONS_KEY = "provider_options"


class ProviderImageClient(ProviderTaskClient):
    """Small provider HTTP client bound to one image-generation target."""

    async def generate(
        self,
        prompt: str,
        *,
        options: JsonObject,
        input_images: tuple[ImageInput, ...] = (),
        wire: ImageWire | None = None,
    ) -> ImageGenerationResult:
        """Call the selected provider's image generation or edit endpoint."""

        selected = wire or image_wire(
            self._provider.id, self._provider.adapter, self._connection.mode
        )
        if self._provider.id == "opencode-zen":
            namespace = {
                "google": "google/v1beta",
                "openai_images": "openai/v1",
                "xai_images": "xai/v1",
            }.get(selected)
            if namespace is None:
                raise ProviderError(
                    "This OpenCode image Model has no supported native route.", retryable=False
                )
            self._base_url = opencode_api_base(self._base_url, namespace)
        if selected == "openrouter":
            return await self._generate_openrouter(
                prompt,
                options=options,
                input_images=input_images,
            )
        if selected == "google":
            return await self._generate_google(prompt, options=options, input_images=input_images)
        if selected == "openai_subscription":
            return await self._generate_openai_codex_responses(
                prompt, options=options, input_images=input_images
            )
        if selected == "xai_images":
            from core.model_tasks._xai_image import xai_image_request

            endpoint, payload = xai_image_request(self._model_id, prompt, options, input_images)
            return await self.post_and_parse(
                endpoint,
                timeout=_DEFAULT_IMAGE_TIMEOUT,
                json=payload,
                parse=lambda response: _parse_unified_image_response(
                    response.json(), model=self._model_id, requested_output_format="jpeg"
                ),
                retry_policy=NON_IDEMPOTENT_TASK_REQUEST_RETRY_POLICY,
            )
        if selected == "openai_images":
            if input_images:
                return await self._edit_openai(
                    prompt,
                    options=options,
                    input_images=input_images,
                )
            return await self._generate_openai(prompt, options=options)
        raise ProviderError(
            f"Image generation not supported for provider '{self._provider.id}'",
            retryable=False,
        )

    async def _generate_openrouter(
        self,
        prompt: str,
        *,
        options: JsonObject,
        input_images: tuple[ImageInput, ...],
    ) -> ImageGenerationResult:
        payload = _build_openrouter_image_payload(
            self._model_id,
            prompt,
            options,
            input_images=input_images,
        )
        requested_output_format = payload.get("output_format")

        _LOGGER.debug(
            "Image generation request: url=%s%s model=%s",
            self._base_url,
            _OPENROUTER_IMAGES_ENDPOINT,
            self._model_id,
        )

        return await self.post_and_parse(
            _OPENROUTER_IMAGES_ENDPOINT,
            timeout=_DEFAULT_IMAGE_TIMEOUT,
            parse=lambda response: _parse_unified_image_response(
                response.json(),
                model=self._model_id,
                requested_output_format=requested_output_format,
            ),
            json=payload,
            retry_policy=_OPENROUTER_IMAGE_RETRY_POLICY,
        )

    async def _generate_google(
        self,
        prompt: str,
        *,
        options: JsonObject,
        input_images: tuple[ImageInput, ...],
    ) -> ImageGenerationResult:
        """Render through Gemini ``generateContent``; source images ride as inline parts."""

        payload = _build_google_image_payload(prompt, options, input_images=input_images)
        endpoint = f"/models/{quote(self._model_id, safe='')}:generateContent"

        _LOGGER.debug(
            "Image generation request: url=%s%s model=%s inputs=%d",
            self._base_url,
            endpoint,
            self._model_id,
            len(input_images),
        )

        return await self.post_and_parse(
            endpoint,
            timeout=_GOOGLE_IMAGE_TIMEOUT,
            parse=lambda response: _parse_google_image_response(
                response.json(), model=self._model_id
            ),
            json=payload,
            headers=self._zen_google_headers if self._provider.id == "opencode-zen" else None,
            retry_policy=_GOOGLE_IMAGE_RETRY_POLICY,
        )

    async def _zen_google_headers(self) -> dict[str, str]:
        return {**self._provider.extra_headers, "x-goog-api-key": await self._credential_value()}

    async def _edit_openai(
        self,
        prompt: str,
        *,
        options: JsonObject,
        input_images: tuple[ImageInput, ...],
    ) -> ImageGenerationResult:
        form = _build_openai_image_edit_form(self._model_id, prompt, options)
        files = _openai_image_edit_files(input_images)
        requested_output_format = form.get("output_format")

        _LOGGER.debug(
            "Image edit request: url=%s%s model=%s inputs=%d",
            self._base_url,
            _OPENAI_IMAGES_EDITS_ENDPOINT,
            self._model_id,
            len(input_images),
        )

        return await self.post_and_parse(
            _OPENAI_IMAGES_EDITS_ENDPOINT,
            timeout=_DEFAULT_IMAGE_TIMEOUT,
            parse=lambda response: _parse_openai_image_response(
                response.json(),
                model=self._model_id,
                requested_output_format=requested_output_format,
            ),
            data=form,
            files=files,
            retry_policy=NON_IDEMPOTENT_TASK_REQUEST_RETRY_POLICY,
        )

    async def _generate_openai(
        self,
        prompt: str,
        *,
        options: JsonObject,
    ) -> ImageGenerationResult:
        payload = _build_openai_image_payload(self._model_id, prompt, options)
        requested_output_format = payload.get("output_format")

        _LOGGER.debug(
            "Image generation request: url=%s%s model=%s",
            self._base_url,
            _OPENAI_IMAGES_GENERATIONS_ENDPOINT,
            self._model_id,
        )

        return await self.post_and_parse(
            _OPENAI_IMAGES_GENERATIONS_ENDPOINT,
            timeout=_DEFAULT_IMAGE_TIMEOUT,
            parse=lambda response: _parse_openai_image_response(
                response.json(),
                model=self._model_id,
                requested_output_format=requested_output_format,
            ),
            json=payload,
            retry_policy=NON_IDEMPOTENT_TASK_REQUEST_RETRY_POLICY,
        )

    async def _generate_openai_codex_responses(
        self,
        prompt: str,
        *,
        options: JsonObject,
        input_images: tuple[ImageInput, ...],
    ) -> ImageGenerationResult:
        payload = _build_openai_codex_image_payload(
            prompt,
            options,
            input_images=input_images,
        )
        requested_output_format = _openai_codex_requested_output_format(payload)

        _LOGGER.debug(
            "Image generation request: url=%s%s model=%s carrier=%s",
            self._base_url,
            CODEX_RESPONSES_ENDPOINT,
            self._model_id,
            _OPENAI_CODEX_IMAGE_CARRIER_MODEL,
        )

        return await self.post_and_parse(
            CODEX_RESPONSES_ENDPOINT,
            timeout=_OPENAI_CODEX_IMAGE_TIMEOUT,
            parse=lambda response: _parse_openai_codex_image_response(
                response.text,
                model=self._model_id,
                requested_output_format=requested_output_format,
            ),
            json=payload,
            headers=self._openai_codex_headers,
            retry_policy=NON_IDEMPOTENT_TASK_REQUEST_RETRY_POLICY,
        )

    @override
    async def _observe_response_usage(self, response: httpx.Response) -> Mapping[str, Any] | None:
        if self._provider.adapter == "google":
            try:
                payload = response.json()
            except ValueError, UnicodeError:
                return None
            return _normalize_gemini_usage(
                payload.get("usageMetadata") if isinstance(payload, Mapping) else None
            )
        if self._connection.mode != CODEX_RESPONSES_MODE:
            return await super()._observe_response_usage(response)
        completed: Mapping[str, Any] | None = None
        for data in _iter_sse_data_from_text(response.text):
            if data.strip() == "[DONE]":
                continue
            try:
                event = parse_sse_json_data(data, context="OpenAI Codex image accounting")
            except ProviderError:
                # The ordinary parser owns malformed-content errors. Earlier
                # reported Usage is still valid even when later data is broken.
                continue
            if isinstance(event, Mapping) and event.get("type") == "response.completed":
                candidate = event.get("response")
                if isinstance(candidate, Mapping):
                    completed = candidate
        usage = _openai_codex_usage(completed) or {}
        carrier = usage.get("response")
        if self._usage_observer is not None and isinstance(carrier, Mapping):
            await self._usage_observer.related(_OPENAI_CODEX_IMAGE_CARRIER_MODEL, carrier)
        primary = usage.get("image_gen")
        return primary if isinstance(primary, Mapping) else None

    async def _openai_codex_headers(self) -> dict[str, str]:
        credential = await self._credential_value()
        account_id = extract_chatgpt_account_id(credential)
        if account_id is None:
            raise ProviderAuthError(
                "OpenAI Subscription OAuth token is missing a ChatGPT account id; please reconnect"
            )
        headers = self._headers_from_credential(credential)
        headers["chatgpt-account-id"] = account_id
        headers.update(CODEX_EXTRA_HEADERS)
        return headers


def _build_openrouter_image_payload(
    model_id: str,
    prompt: str,
    options: JsonObject,
    *,
    input_images: tuple[ImageInput, ...] = (),
) -> JsonObject:
    """Build the OpenRouter unified image API request payload.

    Top-level parameters are taken from the known unified keys that are
    actually present in *options* — no defaults are invented for absent keys,
    so the provider's own defaults apply when the user has not pinned a
    value. Keys whose value is an empty placeholder (see
    :func:`core.providers.task_client.is_omittable_option`) are dropped so unset optional fields are
    not forwarded. ``provider_options`` becomes the nested
    ``provider.options`` object (provider-specific passthrough keys), and
    ``extra_options`` adds only fields not already authored by this builder.
    """

    payload: JsonObject = {
        "model": model_id,
        "prompt": prompt,
    }
    for key in _UNIFIED_IMAGE_KEYS:
        if key in options and not is_omittable_option(options[key]):
            payload[key] = options[key]

    if input_images:
        payload["input_references"] = [
            {
                "type": "image_url",
                "image_url": {
                    "url": (
                        f"data:{image.media_type};base64,"
                        f"{base64.b64encode(image.data).decode('ascii')}"
                    )
                },
            }
            for image in input_images
        ]

    provider_options = options.get(_PROVIDER_OPTIONS_KEY)
    if isinstance(provider_options, dict) and provider_options:
        payload["provider"] = {"options": provider_options}

    merge_extra_options(payload, options)
    return payload


def _build_google_image_payload(
    prompt: str,
    options: JsonObject,
    *,
    input_images: tuple[ImageInput, ...] = (),
) -> JsonObject:
    """Build the Gemini ``generateContent`` image request.

    The prompt comes first, then each source image as an ``inlineData`` part
    in order. ``aspect_ratio`` and ``resolution`` become
    ``generationConfig.imageConfig``, ``thinking_level`` its ``thinkingConfig``;
    unset options are left to Google's defaults. ``extra_options`` adds
    top-level request fields such as ``safetySettings`` or ``tools``.
    """

    parts: list[JsonObject] = [{"text": prompt}]
    parts.extend(
        {
            "inlineData": {
                "mimeType": image.media_type,
                "data": base64.b64encode(image.data).decode("ascii"),
            }
        }
        for image in input_images
    )
    generation_config: JsonObject = {"responseModalities": list(_GOOGLE_RESPONSE_MODALITIES)}
    image_config = {
        wire_key: options[key]
        for key, wire_key in _GOOGLE_IMAGE_CONFIG_KEYS
        if key in options and not is_omittable_option(options[key])
    }
    if image_config:
        generation_config["imageConfig"] = image_config
    thinking_level = options.get("thinking_level")
    if not is_omittable_option(thinking_level):
        generation_config["thinkingConfig"] = {"thinkingLevel": thinking_level}
    payload: JsonObject = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": generation_config,
    }
    merge_extra_options(payload, options)
    return payload


def _parse_google_image_response(payload: Any, *, model: str) -> ImageGenerationResult:
    """Extract the rendered images of a Gemini ``generateContent`` answer.

    Final images are the ``inlineData`` image parts outside the thoughts (the
    interim thought images are drafts). When nothing was rendered, a blocked
    prompt, a safety or prohibited-content finish, ``IMAGE_OTHER``,
    ``NO_IMAGE`` or a text-only answer is a refusal whose reason is the
    Model's own text when it wrote one; anything else is unconfirmed.
    """

    if not isinstance(payload, Mapping):
        raise ProviderError("Google image response must be a JSON object", retryable=True)
    candidates = payload.get("candidates")
    candidate = (
        candidates[0]
        if isinstance(candidates, list) and candidates and isinstance(candidates[0], Mapping)
        else {}
    )
    content = candidate.get("content")
    raw_parts = content.get("parts") if isinstance(content, Mapping) else None
    parts = [
        part
        for part in (raw_parts if isinstance(raw_parts, list) else ())
        if isinstance(part, Mapping) and part.get("thought") is not True
    ]
    entries: list[JsonObject] = []
    texts: list[str] = []
    for part in parts:
        inline = part.get("inlineData")
        if isinstance(inline, Mapping) and str(inline.get("mimeType", "")).startswith("image/"):
            entries.append({"b64_json": inline.get("data"), "media_type": inline.get("mimeType")})
        elif isinstance(part.get("text"), str):
            texts.append(part["text"])
    finish = candidate.get("finishReason")
    usage = _normalize_gemini_usage(payload.get("usageMetadata"))
    raw: JsonObject = {
        key: payload[key]
        for key in ("modelVersion", "responseId", "usageMetadata")
        if key in payload
    }
    raw["finishReason"] = finish
    raw["text"] = "".join(texts)

    images, failures = _decode_image_entries(
        entries, field="b64_json", requested_output_format=None
    )
    if images:
        return ImageGenerationResult(
            images=images, model=model, usage=usage, raw=raw, failures=failures
        )
    if entries:
        raise ProviderError("Google image response images could not be decoded", retryable=True)

    feedback = payload.get("promptFeedback")
    block_reason = feedback.get("blockReason") if isinstance(feedback, Mapping) else None
    if block_reason:
        raise ProviderContentRefusedError(f"Google blocked the prompt ({block_reason})")
    filtered = (
        _gemini_finish_reason(finish, has_tool_calls=False) == TERMINAL_OUTCOME_CONTENT_FILTERED
    )
    if filtered or finish in _GOOGLE_DECLINED_FINISH_REASONS or (finish == "STOP" and texts):
        reply = _carrier_reply(texts)
        message = candidate.get("finishMessage")
        if reply is None and isinstance(message, str) and message.strip():
            reply = _carrier_reply([message])
        raise ProviderContentRefusedError(reply or f"Google returned no image ({finish})")
    raise ProviderError(
        f"Google image response contains no image (finish reason {finish or 'missing'})",
        retryable=True,
    )


def _build_openai_image_edit_form(
    model_id: str,
    prompt: str,
    options: JsonObject,
) -> dict[str, str]:
    """Build scalar multipart fields for OpenAI's image-edit endpoint."""

    payload: JsonObject = {"model": model_id, "prompt": prompt}
    for key in _OPENAI_IMAGE_EDIT_KEYS:
        if key in options and not is_omittable_option(options[key]):
            payload[key] = options[key]
    merge_extra_options(payload, options)
    return {key: _multipart_form_value(value) for key, value in payload.items()}


def _multipart_form_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, dict | list):
        return json.dumps(value, separators=(",", ":"))
    return str(value)


def _openai_image_edit_files(input_images: tuple[ImageInput, ...]) -> list[tuple[str, Any]]:
    """Build OpenAI multipart file parts, using bracket notation for arrays."""

    field_name = "image" if len(input_images) == 1 else "image[]"
    return [(field_name, (image.filename, image.data, image.media_type)) for image in input_images]


def _build_openai_image_payload(
    model_id: str,
    prompt: str,
    options: JsonObject,
) -> JsonObject:
    """Build the OpenAI ``/v1/images/generations`` request payload.

    Only fields that are present in *options* with a non-empty value are
    included; no defaults are invented and empty placeholders are dropped
    (see :func:`core.providers.task_client.is_omittable_option`). ``n > 1`` is honored: the response
    ``data`` array is mapped to one image per element downstream, and
    ``ImageService.generate_artifacts`` already loops over the result to
    persist one artifact per image. ``extra_options`` adds only fields not
    already authored by this builder.
    """

    payload: JsonObject = {
        "model": model_id,
        "prompt": prompt,
    }
    for key in _OPENAI_IMAGE_KEYS:
        if key in options and not is_omittable_option(options[key]):
            payload[key] = options[key]
    merge_extra_options(payload, options)
    return payload


def _build_openai_codex_image_payload(
    prompt: str,
    options: JsonObject,
    *,
    input_images: tuple[ImageInput, ...] = (),
) -> JsonObject:
    """Build the OpenAI subscription Codex Responses image request."""

    tool: JsonObject = {"type": "image_generation"}
    for key in _OPENAI_CODEX_IMAGE_TOOL_KEYS:
        if key in options and not is_omittable_option(options[key]):
            tool[key] = options[key]

    merge_extra_options(tool, options)
    _drop_openai_codex_forbidden_tool_keys(tool, options)
    tool["type"] = "image_generation"

    content: list[JsonObject] = [
        {
            "type": "input_text",
            "text": _openai_codex_image_user_text(
                prompt,
                tool,
                has_input_images=bool(input_images),
            ),
        }
    ]
    content.extend(
        {
            "type": "input_image",
            "image_url": (
                f"data:{image.media_type};base64,{base64.b64encode(image.data).decode('ascii')}"
            ),
        }
        for image in input_images
    )

    return {
        "model": _OPENAI_CODEX_IMAGE_CARRIER_MODEL,
        "stream": True,
        "store": False,
        "instructions": _OPENAI_CODEX_IMAGE_INSTRUCTIONS,
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": content,
            }
        ],
        "tools": [tool],
    }


def _drop_openai_codex_forbidden_tool_keys(tool: JsonObject, options: JsonObject) -> None:
    dropped_keys = {
        key
        for key in _OPENAI_CODEX_IMAGE_FORBIDDEN_TOOL_KEYS
        if key in tool or (key in options and not is_omittable_option(options[key]))
    }
    for key in sorted(dropped_keys):
        tool.pop(key, None)
        _LOGGER.debug("Dropped unsupported OpenAI Codex image option: %s", key)


def _openai_codex_image_user_text(
    prompt: str,
    tool: Mapping[str, Any],
    *,
    has_input_images: bool = False,
) -> str:
    hints = [
        f"{key} {tool[key]}"
        for key in _OPENAI_CODEX_IMAGE_PROMPT_HINT_KEYS
        if key in tool and not is_omittable_option(tool[key])
    ]
    action = "edit the provided image(s)" if has_input_images else "render"
    if not hints:
        return f"Use the image_generation tool to {action}: {prompt}"
    return f"Use the image_generation tool to {action} ({', '.join(hints)}): {prompt}"


def _openai_codex_requested_output_format(payload: Mapping[str, Any]) -> Any:
    tools = payload.get("tools")
    if not isinstance(tools, list) or not tools:
        return None
    tool = tools[0]
    if not isinstance(tool, Mapping):
        return None
    return tool.get("output_format")


def _decode_image_entries(
    entries: list[Any], *, field: str, requested_output_format: Any
) -> tuple[tuple[GeneratedImage, ...], tuple[ImageGenerationFailure, ...]]:
    images: list[GeneratedImage] = []
    failures: list[ImageGenerationFailure] = []
    for index, entry in enumerate(entries):
        encoded = entry.get(field) if isinstance(entry, dict) else None
        if not isinstance(encoded, str) or not encoded:
            reason = "No image data was returned."
            if isinstance(entry, dict) and entry.get("url"):
                reason = "Only an image URL was returned; inline image data is required."
            elif isinstance(entry, dict) and entry.get("status") == "failed":
                reason = "Image generation failed; no image data was returned."
            failures.append(ImageGenerationFailure(index, reason))
            continue
        try:
            decoded = base64.b64decode(encoded, validate=True)
        except binascii.Error, ValueError:
            failures.append(
                ImageGenerationFailure(index, "The returned image data could not be decoded.")
            )
            continue
        media_type = entry.get("media_type") or entry.get("mime_type")
        if not isinstance(media_type, str) or not media_type.strip():
            media_type = _media_type_from_output_format(
                entry.get("output_format") or requested_output_format
            )
        revised = entry.get("revised_prompt")
        images.append(
            GeneratedImage(
                data=decoded,
                media_type=media_type,
                index=index,
                revised_prompt=revised.strip()
                if isinstance(revised, str) and revised.strip()
                else None,
            )
        )
    return tuple(images), tuple(failures)


def _parse_unified_image_response(
    payload: JsonObject,
    *,
    model: str,
    requested_output_format: Any = None,
) -> ImageGenerationResult:
    """Extract images from an OpenRouter unified image API response.

    The response shape is ``{"created": <int>, "data": [<entry>, ...],
    "usage": {...}}`` where each entry carries ``b64_json`` and optionally a
    ``media_type`` (vector outputs). The media type falls back to the
    requested ``output_format`` because raster entries do not echo it.
    """

    data = payload.get("data")
    if not isinstance(data, list) or not data:
        raise ProviderError(
            "Image generation response contains no data",
            retryable=True,
        )

    images, failures = _decode_image_entries(
        data, field="b64_json", requested_output_format=requested_output_format
    )
    if not images:
        if any(isinstance(entry, dict) and entry.get("url") for entry in data):
            raise ProviderError(
                "Image generation returned URLs instead of inline bytes; set "
                "response_format='b64_json' in the task-model options.",
                retryable=False,
            )
        raise ProviderError(
            "Image generation response images could not be decoded",
            retryable=True,
        )

    usage = payload.get("usage")
    return ImageGenerationResult(
        images=images,
        model=model,
        usage=usage if isinstance(usage, dict) else None,
        raw=payload,
        failures=failures,
    )


def _parse_openai_codex_image_response(
    sse_body: str,
    *,
    model: str,
    requested_output_format: Any = None,
) -> ImageGenerationResult:
    image_items: list[JsonObject] = []
    carrier_texts: list[str] = []
    completed_response: Mapping[str, Any] | None = None

    for data in _iter_sse_data_from_text(sse_body):
        if data.strip() == "[DONE]":
            continue
        event = parse_sse_json_data(data, context="OpenAI Codex image generation")
        if not isinstance(event, Mapping):
            continue
        event_type = event.get("type")
        if event_type == "response.output_item.done":
            item = event.get("item")
            if not isinstance(item, Mapping):
                continue
            if item.get("type") == "image_generation_call":
                image_items.append(dict(item))
            elif item.get("type") == "message":
                carrier_texts.extend(_codex_message_texts(item))
        elif event_type == "response.completed":
            response = event.get("response")
            if isinstance(response, Mapping):
                completed_response = response

    images, failures = _decode_image_entries(
        image_items, field="result", requested_output_format=requested_output_format
    )
    if not images:
        # The tool reports a refused render only as an item with status
        # "failed" and no reason; the carrier's reply carries the reason. A
        # reply without any image call is the carrier declining by itself.
        refused = (image_items and all(item.get("status") == "failed" for item in image_items)) or (
            not image_items and carrier_texts
        )
        if refused:
            raise ProviderContentRefusedError(_carrier_reply(carrier_texts))
        if not image_items:
            raise ProviderError(
                "OpenAI Codex image response contains no final image",
                retryable=True,
            )
        raise ProviderError(
            "OpenAI Codex image response images could not be decoded",
            retryable=True,
        )

    raw: JsonObject = {"image_generation_calls": image_items}
    if completed_response is not None:
        raw["response"] = dict(completed_response)

    return ImageGenerationResult(
        images=images,
        model=model,
        usage=_openai_codex_usage(completed_response),
        raw=raw,
        failures=failures,
    )


def _codex_message_texts(item: Mapping[str, Any]) -> list[str]:
    content = item.get("content")
    if not isinstance(content, list):
        return []
    return [
        part["text"]
        for part in content
        if isinstance(part, Mapping)
        and part.get("type") == "output_text"
        and isinstance(part.get("text"), str)
    ]


def _carrier_reply(texts: list[str]) -> str | None:
    """Return the carrier's reply as one bounded line, or None when it said nothing."""

    reply = " ".join(" ".join(texts).split())
    if not reply:
        return None
    if len(reply) <= _MAX_CARRIER_REPLY_CHARS:
        return reply
    return reply[: _MAX_CARRIER_REPLY_CHARS - 3].rstrip() + "..."


def _iter_sse_data_from_text(body: str):
    data_parts: list[str] = []
    for line in split_stream_lines(body):
        if line == "":
            if data_parts:
                yield "\n".join(data_parts)
                data_parts = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            data_parts.append(line[len("data:") :].lstrip(" "))
    if data_parts:
        yield "\n".join(data_parts)


def _openai_codex_usage(completed_response: Mapping[str, Any] | None) -> JsonObject | None:
    if completed_response is None:
        return None

    usage: JsonObject = {}
    tool_usage = completed_response.get("tool_usage")
    if isinstance(tool_usage, Mapping):
        image_usage = tool_usage.get("image_gen")
        if isinstance(image_usage, Mapping):
            usage["image_gen"] = dict(image_usage)

    response_usage = completed_response.get("usage")
    if isinstance(response_usage, Mapping):
        usage["response"] = dict(response_usage)

    return usage or None


def _media_type_from_output_format(requested_output_format: Any) -> str:
    if requested_output_format == "svg":
        return "image/svg+xml"
    if isinstance(requested_output_format, str) and requested_output_format:
        return "image/" + requested_output_format
    return "image/png"


def _parse_openai_image_response(
    payload: JsonObject,
    *,
    model: str,
    requested_output_format: Any = None,
) -> ImageGenerationResult:
    """Extract images from an OpenAI ``/v1/images/generations`` response.

    The response shape is ``{"created": <int>, "data": [<entry>, ...]}``
    where each entry has either ``b64_json`` (the default ``b64_json``
    ``response_format``) or ``url`` (``response_format="url"``). The wire
    layer only decodes ``b64_json`` entries because URL responses would
    require an additional HTTP fetch and the Settings schema defaults to
    ``b64_json``. ``n > 1`` is honored: every ``b64_json`` entry becomes
    one element in the returned ``images`` tuple.
    """

    data = payload.get("data")
    if not isinstance(data, list) or not data:
        raise ProviderError(
            "Image generation response contains no data",
            retryable=True,
        )

    images, failures = _decode_image_entries(
        data, field="b64_json", requested_output_format=requested_output_format
    )
    if not images:
        if any(isinstance(entry, dict) and entry.get("url") for entry in data):
            raise ProviderError(
                "Image generation returned URLs instead of inline bytes; set "
                "response_format='b64_json' in the task-model options.",
                retryable=False,
            )
        raise ProviderError(
            "Image generation response images could not be decoded",
            retryable=True,
        )

    return ImageGenerationResult(
        images=images,
        model=model,
        raw=payload,
        failures=failures,
    )
