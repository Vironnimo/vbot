"""Agent-facing wording for failures of the configured media models."""

from __future__ import annotations

import json
import re

from core.providers.errors import (
    NetworkError,
    ProviderAuthError,
    ProviderRateLimitError,
    ProviderTimeoutError,
)

_MAX_DETAIL_CHARS = 200
_PREFIXES = ("Authentication error:", "Rate limited:", "Provider error:")
_HTML = re.compile(r"<(?:!doctype|html|head|body)\b", re.IGNORECASE)
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_JSON_MESSAGE = re.compile(r'"(?:message|detail|error)"\s*:\s*"((?:[^"\\]|\\.)*)"')
_LABEL = re.compile(r"^\([^)]*\)\s*:?\s*")


def _cause(error: BaseException, kinds: tuple[type[BaseException], ...]) -> BaseException | None:
    current: BaseException | None = error.__cause__
    while current is not None:
        if isinstance(current, kinds):
            return current
        current = current.__cause__
    return None


def _status(error: BaseException) -> int | None:
    current: BaseException | None = error
    while current is not None:
        status = getattr(current, "status_code", None)
        if isinstance(status, int) and not isinstance(status, bool):
            return status
        current = current.__cause__
    return None


def _shortened(text: str) -> str:
    if len(text) <= _MAX_DETAIL_CHARS:
        return text
    return text[: _MAX_DETAIL_CHARS - 3].rstrip() + "..."


def provider_detail(error: BaseException) -> str:
    """Return the status and the provider's own reason, without raw bodies."""
    status = _status(error)
    text = " ".join(str(error).split())
    for prefix in _PREFIXES:
        if text.startswith(prefix):
            text = text[len(prefix) :].strip()
            break
    if status is not None and text.startswith(str(status)):
        text = text[len(str(status)) :].strip(" :-")
    text = _LABEL.sub("", text)
    if _HTML.search(text):
        title = _TITLE.search(text)
        text = " ".join(title[1].split()) if title else "an HTML error page"
    else:
        message = _JSON_MESSAGE.search(text)
        if message is not None:
            try:
                text = json.loads(f'"{message[1]}"')
            except ValueError:
                text = message[1]
    text = _shortened(text.strip())
    if status is None:
        return text or "no details"
    return f"HTTP {status}: {text}" if text else f"HTTP {status}"


def provider_failure_message(error: BaseException, *, task: str, setting: str) -> str:
    """Say what a failed media-model request means and what the Agent can do next.

    ``task`` names the provider's job in running text ("image-understanding");
    ``setting`` is the Settings entry under Specialized Models ("Image
    understanding").
    """
    detail = provider_detail(error)
    if _cause(error, (ProviderAuthError,)) is not None:
        return (
            f"The {task} provider rejected its credentials ({detail}). Tell the user to "
            "check that provider's API key or sign-in in Settings."
        )
    if _cause(error, (ProviderRateLimitError,)) is not None:
        return (
            f"The {task} provider is limiting requests or its usage limit is reached "
            f"({detail}). Wait before trying again, and tell the user if it keeps happening."
        )
    if _cause(error, (ProviderTimeoutError, NetworkError)) is not None:
        return f"The {task} provider did not answer ({detail}). Try again later."
    if getattr(error, "retryable", False):
        return f"The {task} provider failed ({detail}). Try again later."
    return (
        f"The {task} provider rejected the request ({detail}). If the reason concerns the "
        "request, change it; otherwise tell the user, who may need to choose another "
        f"{setting} model in Settings under Specialized Models."
    )


def unavailable_message(error: BaseException, *, setting: str) -> str:
    """Say that the Settings entry for a media model needs the user's attention."""
    return (
        f"{setting} is not available ({provider_detail(error)}). Tell the user to choose a "
        f"working {setting} model in Settings under Specialized Models."
    )
