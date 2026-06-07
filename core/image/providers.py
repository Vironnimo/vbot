"""Provider HTTP clients for image generation task-model targets."""

from __future__ import annotations

import base64
import re
from typing import Any

import httpx

from core.image.types import ImageGenerationResult, JsonObject
from core.providers._http_shared import classify_http_status, wrap_network_error
from core.providers.errors import ProviderError
from core.utils.logging import get_logger
from core.utils.retry import retry_async

_CHAT_COMPLETIONS_ENDPOINT = "/chat/completions"
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


class ProviderImageClient:
    """Small provider HTTP client bound to one image-generation target."""

    def __init__(
        self,
        *,
        provider: Any,
        connection: Any,
        credential: str,
        model_id: str,
    ) -> None:
        self._provider = provider
        self._connection = connection
        self._credential = credential
        self._model_id = model_id
        self._base_url = connection.base_url or provider.base_url

    @classmethod
    def from_runtime(cls, runtime: Any, target_ref: Any) -> ProviderImageClient:
        """Create a client from runtime provider configuration and credentials."""

        provider = runtime.providers.get(target_ref.provider_id)
        connection = provider.get_connection(target_ref.local_connection_id)
        credential = runtime.provider_credentials.get_credentials(
            target_ref.provider_id,
            target_ref.connection_id,
        )
        return cls(
            provider=provider,
            connection=connection,
            credential=credential,
            model_id=target_ref.model_id,
        )

    async def generate(
        self,
        prompt: str,
        *,
        options: JsonObject,
    ) -> ImageGenerationResult:
        """Call the selected provider's image generation endpoint."""

        if self._provider.id == "openrouter":
            return await self._generate_openrouter(prompt, options=options)
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

        async def _do_request() -> ImageGenerationResult:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=_DEFAULT_IMAGE_TIMEOUT,
            ) as client:
                try:
                    response = await client.post(
                        _CHAT_COMPLETIONS_ENDPOINT,
                        json=payload,
                        headers=self._headers(),
                    )
                except (httpx.TimeoutException, httpx.ConnectError) as exc:
                    raise wrap_network_error(exc) from exc
                _classify_image_response(response)
                return _parse_image_response(response.json(), model=self._model_id)

        return await retry_async(_do_request)

    def _headers(self) -> dict[str, str]:
        auth = self._connection.auth
        headers = {auth.header: f"{auth.prefix}{self._credential}"}
        if self._provider.extra_headers:
            headers.update(self._provider.extra_headers)
        return headers


def _build_openrouter_image_payload(
    model_id: str,
    prompt: str,
    options: JsonObject,
) -> JsonObject:
    """Build the OpenRouter image-generation request payload.

    ``image_config`` is assembled from the known image_config keys that are
    actually present in *options* — no defaults are invented for absent keys,
    so providers that default to ``1:1``/``1K`` (or any other value) keep
    their own defaults when the user has not pinned a value. The top-level
    ``seed`` is sent when it is present in *options*; the field is
    provider-level, not nested under ``image_config``.
    """

    image_config: JsonObject = {}
    for key in _IMAGE_CONFIG_KEYS:
        if key in options:
            image_config[key] = options[key]

    payload: JsonObject = {
        "model": model_id,
        "messages": [{"role": "user", "content": prompt}],
        "modalities": ["image"],
    }
    if image_config:
        payload["image_config"] = image_config
    if "seed" in options:
        payload["seed"] = options["seed"]
    return payload


def _classify_image_response(response: httpx.Response) -> None:
    """Classify an image generation HTTP response, including body detail on error."""

    detail = response.text if response.status_code >= 400 else ""
    classify_http_status(
        response.status_code,
        detail=f"{response.status_code} {detail}".strip() if detail else str(response.status_code),
    )


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
