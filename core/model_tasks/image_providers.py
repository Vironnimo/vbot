"""Provider HTTP clients for image generation task-model targets."""

from __future__ import annotations

import base64
from typing import Any

from core.model_tasks.image_types import ImageGenerationResult, JsonObject
from core.providers.errors import ProviderError
from core.providers.task_client import (
    ProviderTaskClient,
    is_omittable_option,
    merge_extra_options,
)
from core.utils.logging import get_logger

_OPENROUTER_IMAGES_ENDPOINT = "/images"
_OPENAI_IMAGES_GENERATIONS_ENDPOINT = "/images/generations"
_DEFAULT_IMAGE_TIMEOUT = 120.0
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
    ) -> ImageGenerationResult:
        """Call the selected provider's image generation endpoint."""

        if self._provider.id == "openrouter":
            return await self._generate_openrouter(prompt, options=options)
        if self._provider.id == "openai":
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
    ) -> ImageGenerationResult:
        payload = _build_openrouter_image_payload(self._model_id, prompt, options)
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


def _build_openrouter_image_payload(
    model_id: str,
    prompt: str,
    options: JsonObject,
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

    provider_options = options.get(_PROVIDER_OPTIONS_KEY)
    if isinstance(provider_options, dict) and provider_options:
        payload["provider"] = {"options": provider_options}

    merge_extra_options(payload, options)
    return payload


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
