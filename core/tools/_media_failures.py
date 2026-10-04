"""Agent-facing wording for failures of the configured media models."""

from __future__ import annotations

import json
import re

from core.model_tasks.artifacts import OutputDirectoryError, OutputWriteError
from core.providers.errors import (
    NetworkError,
    ProviderAuthError,
    ProviderContentRefusedError,
    ProviderRateLimitError,
    ProviderTimeoutError,
)
from core.utils.paths import model_path

_MAX_DETAIL_CHARS = 200
_PREFIXES = ("Authentication error:", "Rate limited:", "Provider error:")
_HTML = re.compile(r"<(?:!doctype|html|head|body)\b", re.IGNORECASE)
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_JSON_MESSAGE = re.compile(r'"(?:message|detail|error)"\s*:\s*"((?:[^"\\]|\\.)*)"')
_LABEL = re.compile(r"^\([^)]*\)\s*:?\s*")
_LEADING_STATUS = re.compile(r"([1-5]\d\d)\b[\s:-]*")


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
    leading = _LEADING_STATUS.match(text)
    if leading is not None and (status is None or int(leading[1]) == status):
        status = int(leading[1])
        text = text[leading.end() :]
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


#: The Settings page and section that show a media model's entry, as the WebUI
#: names them; every entry not listed here sits with the media models on Tools.
_SETTINGS_PLACES = {"Text to speech": "Voice → Speech models"}
_MEDIA_MODELS_PLACE = "Tools → Images, video & music"


def settings_place(setting: str) -> str:
    """Where the user chooses the model of the Settings entry ``setting``."""
    return f"Settings → {_SETTINGS_PLACES.get(setting, _MEDIA_MODELS_PLACE)}"


def provider_failure_message(error: BaseException, *, task: str, setting: str) -> str:
    """Say what a failed media-model request means and what the Agent can do next.

    ``task`` names the provider's job in running text ("image-understanding");
    ``setting`` is the model's Settings entry ("Image understanding").
    """
    refusal = _cause(error, (ProviderContentRefusedError,))
    if isinstance(refusal, ProviderContentRefusedError):
        return refusal_message(refusal.reason, task=task)
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
        f"{setting} model in {settings_place(setting)}."
    )


def unavailable_message(error: BaseException, *, setting: str) -> str:
    """Say that the Settings entry for a media model needs the user's attention."""
    return (
        f"{setting} is not available ({provider_detail(error)}). Tell the user to choose a "
        f"working {setting} model in {settings_place(setting)}."
    )


def refusal_message(reason: str | None, *, task: str) -> str:
    """Say that the provider declined the content and how the Agent can go on."""
    if reason:
        cause = f"Its reason: {_shortened(' '.join(reason.split()))}"
        if not cause.endswith((".", "!", "?")):
            cause += "."
    else:
        cause = "It gave no reason, which most often means its content policy blocked the request."
    return (
        f"The {task} provider refused the request and created nothing. {cause} Repeating "
        "the unchanged request gets the same refusal. Change what the prompt asks for, for "
        "example an original design instead of a named character, brand or real person, "
        "or tell the user."
    )


def outcome_unknown_message(*, task: str, product: str) -> str:
    """Say that a billed request ended without a readable answer and what that means."""
    return (
        f"The request to the {task} provider ended without a usable answer, so it is unknown "
        f"whether it created the {product}. Nothing was saved. Repeating the request can "
        f"create and charge a second {product}. If you still need it, repeat the call once, "
        "and tell the user if that fails too."
    )


def output_failure_message(error: OutputDirectoryError | OutputWriteError, *, product: str) -> str:
    """Say what was saved, why the folder failed, and the next call."""
    if isinstance(error, OutputDirectoryError):
        folder = model_path(error.directory)
        return (
            f"Nothing was generated. The output folder {folder} cannot be used: "
            f"{error.reason}. Pass another output_dir, or omit output_dir to use the default "
            "folder."
        )
    saved = (
        "Files saved before the failure: "
        + ", ".join(model_path(path) for path in error.saved)
        + "."
        if error.saved
        else "No file was saved."
    )
    folder = model_path(error.directory)
    return (
        f"The provider generated the {product}, but saving to {folder} failed: "
        f"{error.reason}. {saved} Repeating the call generates the {product} again at new "
        "cost. Tell the user before you repeat it."
    )


def unfinished_job_message(*, task: str, product: str, job_id: str, reason: str) -> str:
    """Say that an accepted job's result could not be collected and must not be resubmitted."""
    return (
        f"Nothing was saved. The {task} provider accepted the request as job {job_id}, but "
        f"{_shortened(' '.join(reason.split()))}. If the job finishes, the provider bills it. "
        f"Repeating the call starts and bills a second {product} job. Tell the user, including "
        "the job id, instead of repeating the call."
    )
