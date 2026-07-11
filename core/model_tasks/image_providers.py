"""Provider HTTP clients for image generation task-model targets."""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping
from typing import Any

from core.model_tasks.image_types import ImageGenerationResult, ImageInput, JsonObject
from core.providers._http_shared import parse_sse_json_data
from core.providers.errors import ProviderAuthError, ProviderError
from core.providers.openai import (
    CODEX_EXTRA_HEADERS,
    CODEX_RESPONSES_ENDPOINT,
    CODEX_RESPONSES_MODE,
)
from core.providers.openai_subscription_auth import extract_chatgpt_account_id
from core.providers.task_client import (
    ProviderTaskClient,
    is_omittable_option,
    merge_extra_options,
)
from core.utils.logging import get_logger

_OPENROUTER_IMAGES_ENDPOINT = "/images"
_OPENAI_IMAGES_GENERATIONS_ENDPOINT = "/images/generations"
_OPENAI_IMAGES_EDITS_ENDPOINT = "/images/edits"
_DEFAULT_IMAGE_TIMEOUT = 120.0
_OPENAI_CODEX_IMAGE_TIMEOUT = 300.0
_OPENAI_CODEX_IMAGE_CARRIER_MODEL = "gpt-5.5"
_OPENAI_CODEX_IMAGE_INSTRUCTIONS = "You are an image generation assistant."
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
    ) -> ImageGenerationResult:
        """Call the selected provider's image generation or edit endpoint."""

        if self._provider.id == "openrouter":
            return await self._generate_openrouter(
                prompt,
                options=options,
                input_images=input_images,
            )
        if self._provider.id == "openai":
            if self._connection.mode == CODEX_RESPONSES_MODE:
                return await self._generate_openai_codex_responses(
                    prompt,
                    options=options,
                    input_images=input_images,
                )
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
        requested_output_format = options.get("output_format")

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
        )

    async def _edit_openai(
        self,
        prompt: str,
        *,
        options: JsonObject,
        input_images: tuple[ImageInput, ...],
    ) -> ImageGenerationResult:
        form = _build_openai_image_edit_form(self._model_id, prompt, options)
        files = _openai_image_edit_files(input_images)
        requested_output_format = options.get("output_format")

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
        )

    async def _generate_openai(
        self,
        prompt: str,
        *,
        options: JsonObject,
    ) -> ImageGenerationResult:
        payload = _build_openai_image_payload(self._model_id, prompt, options)
        requested_output_format = options.get("output_format")

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
        )

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
    ``extra_options`` merges last.
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
    persist one artifact per image. ``extra_options`` merges last.
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

    image_bytes_list: list[bytes] = []
    detected_media_type = ""
    for entry in data:
        if not isinstance(entry, dict):
            continue
        b64_json = entry.get("b64_json")
        if not isinstance(b64_json, str) or not b64_json:
            continue
        image_bytes_list.append(base64.b64decode(b64_json))
        entry_media_type = entry.get("media_type")
        if not detected_media_type and isinstance(entry_media_type, str):
            detected_media_type = entry_media_type

    if not image_bytes_list:
        raise ProviderError(
            "Image generation response images could not be decoded",
            retryable=True,
        )

    if not detected_media_type:
        detected_media_type = _media_type_from_output_format(requested_output_format)

    usage = payload.get("usage")
    return ImageGenerationResult(
        images=tuple(image_bytes_list),
        media_type=detected_media_type,
        model=model,
        usage=usage if isinstance(usage, dict) else None,
        raw=payload,
    )


def _parse_openai_codex_image_response(
    sse_body: str,
    *,
    model: str,
    requested_output_format: Any = None,
) -> ImageGenerationResult:
    image_items: list[JsonObject] = []
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
            if isinstance(item, Mapping) and item.get("type") == "image_generation_call":
                image_items.append(dict(item))
        elif event_type == "response.completed":
            response = event.get("response")
            if isinstance(response, Mapping):
                completed_response = response

    if not image_items:
        raise ProviderError(
            "OpenAI Codex image response contains no final image",
            retryable=True,
        )

    image_bytes_list: list[bytes] = []
    for item in image_items:
        result = item.get("result")
        if not isinstance(result, str) or not result:
            continue
        try:
            image_bytes_list.append(base64.b64decode(result, validate=True))
        except (binascii.Error, ValueError):
            continue

    if not image_bytes_list:
        raise ProviderError(
            "OpenAI Codex image response images could not be decoded",
            retryable=True,
        )

    actual_output_format = requested_output_format
    if is_omittable_option(actual_output_format):
        actual_output_format = image_items[0].get("output_format")

    raw: JsonObject = {"image_generation_calls": image_items}
    if completed_response is not None:
        raw["response"] = dict(completed_response)

    return ImageGenerationResult(
        images=tuple(image_bytes_list),
        media_type=_media_type_from_output_format(actual_output_format),
        model=model,
        usage=_openai_codex_usage(completed_response),
        raw=raw,
    )


def _iter_sse_data_from_text(body: str):
    data_parts: list[str] = []
    for raw_line in body.splitlines():
        line = raw_line.rstrip("\r")
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

    image_bytes_list: list[bytes] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        b64_json = entry.get("b64_json")
        if isinstance(b64_json, str) and b64_json:
            image_bytes_list.append(base64.b64decode(b64_json))
        elif isinstance(entry.get("url"), str):
            # URL responses require an extra fetch; we surface a clear
            # error rather than silently dropping the image so the caller
            # can switch to ``response_format="b64_json"``.
            raise ProviderError(
                "OpenAI image returned a URL but the wire layer only "
                "decodes b64_json; set response_format='b64_json' in the "
                "task-model options to receive inline bytes.",
                retryable=False,
            )

    if not image_bytes_list:
        raise ProviderError(
            "Image generation response images could not be decoded",
            retryable=True,
        )

    # gpt-image-1 returns the bytes verbatim; the format is determined by
    # the request's ``output_format`` (default ``png``). The response
    # body does not echo the format, so we record the value the caller
    # asked for. The artifact layer falls back to ``image/png`` when no
    # format was requested.
    return ImageGenerationResult(
        images=tuple(image_bytes_list),
        media_type=_media_type_from_output_format(requested_output_format),
        model=model,
        raw=payload,
    )
