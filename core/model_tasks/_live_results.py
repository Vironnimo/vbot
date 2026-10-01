"""Tool results and update text of Live calls, shared by the call and its host.

Every Tool result is a standard Tool result envelope whose success data is one
short plain-text ``content``; a failure message says what was wrong and names
the next valid call. Run announcements start with :data:`LIVE_UPDATE_PREFIX`,
so instructions can tell the voice model they are app data.
"""

from __future__ import annotations

import json
from typing import Any

from core.providers.adapter import tool_result_text
from core.tools import tool_failure, tool_success

JsonObject = dict[str, Any]

LIVE_UPDATE_PREFIX = "vBot update"


def live_success(content: str) -> JsonObject:
    """A successful Tool result whose Model-facing text is *content*."""

    return tool_success({"content": content})


def live_failure(code: str, message: str) -> JsonObject:
    """A failed Tool result; *message* says what was wrong and what to call next."""

    return tool_failure(code, message)


def live_result_text(result: JsonObject) -> str:
    """Render a Tool result as the plain text a Model reads."""

    text = tool_result_text(json.dumps(result, ensure_ascii=False))
    return text if isinstance(text, str) else json.dumps(result, ensure_ascii=False)
