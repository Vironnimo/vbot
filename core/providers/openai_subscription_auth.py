"""OpenAI Subscription OAuth token helpers."""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from typing import Any

OPENAI_AUTH_CLAIM = "https://api.openai.com/auth"
CHATGPT_ACCOUNT_ID_CLAIM = "chatgpt_account_id"
CHATGPT_ACCOUNT_ID_EXTRA_KEY = "chatgpt_account_id"


def extract_chatgpt_account_id(access_token: str) -> str | None:
    """Return the ChatGPT account id embedded in an OpenAI OAuth JWT."""

    payload = _decode_jwt_payload(access_token)
    auth_claim = payload.get(OPENAI_AUTH_CLAIM)
    if not isinstance(auth_claim, Mapping):
        return None
    account_id = auth_claim.get(CHATGPT_ACCOUNT_ID_CLAIM)
    if not isinstance(account_id, str):
        return None
    normalized_account_id = account_id.strip()
    return normalized_account_id or None


def openai_subscription_token_extra(access_token: str) -> dict[str, str]:
    """Build sanitized token-store metadata derived from an OpenAI OAuth token."""

    account_id = extract_chatgpt_account_id(access_token)
    if account_id is None:
        return {}
    return {CHATGPT_ACCOUNT_ID_EXTRA_KEY: account_id}


def _decode_jwt_payload(access_token: str) -> Mapping[str, Any]:
    parts = access_token.split(".")
    if len(parts) < 2:
        return {}
    encoded_payload = parts[1]
    padding = "=" * (-len(encoded_payload) % 4)
    try:
        payload_bytes = base64.urlsafe_b64decode(f"{encoded_payload}{padding}")
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}
    return payload if isinstance(payload, Mapping) else {}
