"""Provider HTTP clients for image generation task-model targets."""

from __future__ import annotations

import base64
import re
from typing import Any

from core.model_tasks.image_types import ImageGenerationResult, JsonObject
from core.providers.errors import ProviderError
from core.providers.task_client import ProviderTaskClient
from core.utils.logging import get_logger

_CHAT_COMPLETIONS_ENDPOINT = "/chat/completions"
_OPENAI_IMAGES_GENERATIONS_ENDPOINT = "/images/generations"
_DEFAULT_IMAGE_TIMEOUT = 120.0
_BASE64_DATA_URL_PATTERN = re.compile(r"^data:([^;]+);base64,(.+)$", re.ASCII)
_LOGGER = get_logger("image.providers")

# All known OpenRouter ``image_config`` keys. The wire layer only sends a key
# when it is present in the merged task-model options; the values are passed
# through unchanged so the provider receives the shape it expects (arrays for
# ``rgb_colors`` / ``text_layout`` / ``font_inputs`` / etc., strings for
# ``style`` / ``scoring_prompt``, numbers for ``strength``).
_IMAGE_CONFIG_KEYS: tuple[str, ...] = (
    "aspect_ratio",
    "image_size",
    "strength",
    "style",
    "rgb_colors",
    "background_rgb_color",
    "text_layout",
    "font_inputs",
    "super_resolution_references",
    "scoring_prompt",
    "scoring_rubric",
    "background_mode",
    "background_hex_color",
)

# OpenAI ``/v1/images/generations`` option keys. The wire layer sends each
# key only when the model advertises support and the user has supplied a
# value. ``model`` and ``prompt`` are always sent.
_OPENAI_IMAGE_KEYS: tuple[str, ...] = (
    "n",
    "size",
    "quality",
    "background",
    "output_format",
    "style",
    "response_format",
)


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

        _LOGGER.debug(
            "Image generation request: url=%s%s model=%s",
            self._base_url,
            _CHAT_COMPLETIONS_ENDPOINT,
            self._model_id,
        )

        return await self.post_and_parse(
            _CHAT_COMPLETIONS_ENDPOINT,
            timeout=_DEFAULT_IMAGE_TIMEOUT,
            parse=lambda response: _parse_image_response(response.json(), model=self._model_id),
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


def _is_omittable_option(value: Any) -> bool:
    """True when an option value carries nothing to forward to the provider.

    Empty placeholders (``None``, ``""``, ``[]``, ``{}``) are option-form
    defaults that mean "unset" — they are injected by the schema's
    ``default_options`` for optional text/json fields and must not reach the
    wire (e.g. Sourceful rejects ``background_hex_color: ""``). Real values
    such as numeric ``0``/``0.0`` and ``False`` carry information and are kept.
    """

    if value is None:
        return True
    return isinstance(value, (str, list, dict)) and len(value) == 0


def _build_openrouter_image_payload(
    model_id: str,
    prompt: str,
    options: JsonObject,
) -> JsonObject:
    """Build the OpenRouter image-generation request payload.

    ``image_config`` is assembled from the known image_config keys that are
    actually present in *options* — no defaults are invented for absent keys,
    so providers that default to ``1:1``/``1K`` (or any other value) keep
    their own defaults when the user has not pinned a value. Keys whose value
    is an empty placeholder (see :func:`_is_omittable_option`) are dropped so
    unset optional fields are not forwarded. The top-level ``seed`` is sent
    when it is present in *options*; the field is provider-level, not nested
    under ``image_config``.
    """

    image_config: JsonObject = {}
    for key in _IMAGE_CONFIG_KEYS:
        if key in options and not _is_omittable_option(options[key]):
            image_config[key] = options[key]

    payload: JsonObject = {
        "model": model_id,
        "messages": [{"role": "user", "content": prompt}],
        "modalities": ["image"],
    }
    if image_config:
        payload["image_config"] = image_config
    if options.get("seed") is not None:
        payload["seed"] = options["seed"]
    return payload


def _build_openai_image_payload(
    model_id: str,
    prompt: str,
    options: JsonObject,
) -> JsonObject:
    """Build the OpenAI ``/v1/images/generations`` request payload.

    Only fields that are present in *options* with a non-empty value are
    included; no defaults are invented and empty placeholders are dropped
    (see :func:`_is_omittable_option`). ``n > 1`` is honored: the response
    ``data`` array is mapped to one image per element downstream, and
    ``ImageService.generate_artifacts`` already loops over the result to
    persist one artifact per image.
    """

    payload: JsonObject = {
        "model": model_id,
        "prompt": prompt,
    }
    for key in _OPENAI_IMAGE_KEYS:
        if key in options and not _is_omittable_option(options[key]):
            payload[key] = options[key]
    return payload


def _parse_image_response(payload: JsonObject, *, model: str) -> ImageGenerationResult:
    """Extract images from an OpenRouter chat/completions response."""

    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ProviderError(
            "Image generation response contains no choices",
            retryable=True,
        )

    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        raise ProviderError(
            "Image generation response message is missing",
            retryable=True,
        )

    images_raw = message.get("images")
    if not isinstance(images_raw, list) or not images_raw:
        raise ProviderError(
            "Image generation response contains no images",
            retryable=True,
        )

    image_bytes_list: list[bytes] = []
    detected_media_type = "image/png"

    for entry in images_raw:
        if isinstance(entry, dict):
            image_url = entry.get("image_url", {})
            url = image_url.get("url", "") if isinstance(image_url, dict) else entry.get("url", "")
        elif isinstance(entry, str):
            url = entry
        else:
            continue

        if not url:
            continue

        match = _BASE64_DATA_URL_PATTERN.match(url)
        if match:
            detected_media_type = match.group(1)
            raw_bytes = base64.b64decode(match.group(2))
            image_bytes_list.append(raw_bytes)

    if not image_bytes_list:
        raise ProviderError(
            "Image generation response images could not be decoded",
            retryable=True,
        )

    usage = payload.get("usage")
    return ImageGenerationResult(
        images=tuple(image_bytes_list),
        media_type=detected_media_type,
        model=model,
        usage=usage if isinstance(usage, dict) else None,
        raw=payload,
    )


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
    if isinstance(requested_output_format, str) and requested_output_format:
        media_type = "image/" + requested_output_format
    else:
        media_type = "image/png"

    return ImageGenerationResult(
        images=tuple(image_bytes_list),
        media_type=media_type,
        model=model,
        raw=payload,
    )
