"""Shared fixtures and fakes for openai behavior tests.

Covers both the default ``/chat/completions`` mode (``api-key`` connection)
and the Codex Responses mode (``subscription`` connection with
``connection_mode="codex_responses"``).
"""

from __future__ import annotations

import base64
import json

import httpx

from core.providers.openai import (
    CODEX_RESPONSES_ENDPOINT,
    CODEX_RESPONSES_MODE,
)
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig

OPENAI_SUBSCRIPTION_URL = f"https://chatgpt.com/backend-api{CODEX_RESPONSES_ENDPOINT}"

SAMPLE_MESSAGES = [
    {"role": "system", "content": "Use concise answers."},
    {"role": "user", "content": "Hello"},
]


def _subscription_config(*, include_mode: bool = True) -> ProviderConfig:
    """Provider config matching the ChatGPT ``subscription`` connection."""

    return ProviderConfig(
        id="openai",
        name="OpenAI",
        adapter="openai",
        base_url="https://chatgpt.com/backend-api",
        connections=[
            ConnectionConfig(
                id="subscription",
                type="oauth",
                label="ChatGPT Plus/Pro",
                auth=AuthConfig(header="Authorization", prefix="Bearer "),
                mode=CODEX_RESPONSES_MODE if include_mode else None,
            )
        ],
        defaults={"max_tokens": 8192},
    )


class _RotatingTokenGetter:
    """Async token getter that yields a fresh token on each call."""

    def __init__(self, tokens: list[str]) -> None:
        self._tokens = tokens
        self.calls = 0

    async def __call__(self) -> str:
        token = self._tokens[min(self.calls, len(self._tokens) - 1)]
        self.calls += 1
        return token


def _jwt_with_account(account_id: str = "acct_vbot") -> str:
    payload = {
        "https://api.openai.com/auth": {
            "chatgpt_account_id": account_id,
        }
    }
    encoded_payload = (
        base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("ascii").rstrip("=")
    )
    return f"header.{encoded_payload}.signature"


def _codex_sse_response(response: dict[str, object]) -> httpx.Response:
    body = (
        "event: response.completed\n"
        f"data: {json.dumps({'type': 'response.completed', 'response': response})}\n\n"
    )
    return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})


_CODEX_TOOLS = [
    {
        "name": "lookup",
        "description": "Look up data",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        "strict": True,
    }
]
