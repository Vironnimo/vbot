"""The ``show_widget`` Tool: an Agent-authored interactive widget inline in the chat.

The widget's HTML is kept as this call's result payload, so it lives exactly as
long as the Tool Result in the Session's history. The server serves it by
capability in a sandboxed document and the WebUI renders it after the call's
row; values the user sends back arrive as a note before their message
(``.vorch/domain-maps/tools/show_widget.md``).
"""

from __future__ import annotations

from functools import cache
from typing import Any

from core.tools._tool_display import display_text
from core.tools.call_syntax import SpellingAliases, normalize_call_arguments
from core.tools.contracts import ToolContract, ToolContractError, compile_tool_contract
from core.tools.tools import (
    JsonObject,
    ToolContext,
    ToolDisplay,
    ToolRegistry,
    tool_failure,
    tool_success,
)

SHOW_WIDGET_TOOL_NAME = "show_widget"
# The stored payload of one widget: ``{"title": str, "widget_code": str}``.
WIDGET_PAYLOAD_TITLE = "title"
WIDGET_PAYLOAD_CODE = "widget_code"
MAX_WIDGET_CODE_BYTES = 1024 * 1024
MAX_WIDGET_TITLE_LENGTH = 120
REFUSAL_PREFIX = "show_widget was not run: "

SHOW_WIDGET_DESCRIPTION = (
    "Show an interactive HTML widget inline in the user's chat, such as controls to tune a "
    "design, variants to choose from, or a chart. It runs in a sandboxed frame without "
    "network access; scripts, styles, fonts and images load only from https URLs."
)
SHOW_WIDGET_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "title": {
            "type": "string",
            "description": "Short label shown above the widget.",
        },
        "widget_code": {
            "type": "string",
            "description": (
                "HTML for the widget's body. Plain form controls already match the app's dark "
                "theme; the CSS variables --bg, --surface, --text, --muted, --border and "
                "--accent are available. When the user changes the widget and sends a "
                "message, you receive with it the values of all form fields that have a name "
                "attribute, plus what the widget passed to vbot.setState({key: value}), such "
                "as a chosen variant."
            ),
        },
    },
    "required": ["title", "widget_code"],
}

_FIELD_ALIASES = SpellingAliases(
    {
        WIDGET_PAYLOAD_CODE: ("html", "code", "content", "widget_html", "markup", "body"),
        WIDGET_PAYLOAD_TITLE: ("name", "label", "heading"),
    }
)


@cache
def _repair_contract() -> ToolContract:
    return compile_tool_contract(
        name=SHOW_WIDGET_TOOL_NAME, input_schema=SHOW_WIDGET_PARAMETERS, require_closed_input=False
    )


def _normalize_arguments(arguments: Any) -> Any:
    normalized = normalize_call_arguments(
        _repair_contract(), arguments, field_aliases=_FIELD_ALIASES
    )
    if not isinstance(normalized, dict):
        return normalized
    title = normalized.get(WIDGET_PAYLOAD_TITLE)
    if isinstance(title, str):
        normalized[WIDGET_PAYLOAD_TITLE] = " ".join(title.split())[:MAX_WIDGET_TITLE_LENGTH]
        if not normalized[WIDGET_PAYLOAD_TITLE]:
            raise ToolContractError(f"{REFUSAL_PREFIX}title is empty; pass a short label.")
    code = normalized.get(WIDGET_PAYLOAD_CODE)
    if isinstance(code, str):
        if not code.strip():
            raise ToolContractError(f"{REFUSAL_PREFIX}widget_code is empty; pass the HTML.")
        size = len(code.encode("utf-8"))
        if size > MAX_WIDGET_CODE_BYTES:
            raise ToolContractError(
                f"{REFUSAL_PREFIX}widget_code has {-(-size // 1024)} KiB; the limit is "
                f"{MAX_WIDGET_CODE_BYTES // 1024} KiB. Shorten the HTML, such as by building "
                "repeated elements with a script."
            )
    return normalized


def register_show_widget_tool(registry: ToolRegistry) -> None:
    """Register ``show_widget``; it needs nothing beyond the calling Session."""

    async def handler(context: ToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
        title = arguments[WIDGET_PAYLOAD_TITLE]
        code = arguments[WIDGET_PAYLOAD_CODE]
        if not context.result_payloads_available:
            return tool_failure(
                "session_required",
                "Nothing was shown. Widgets show in a chat Session, and this call runs "
                "outside one.",
            )
        widget_id = context.attach_result_payload(
            {WIDGET_PAYLOAD_TITLE: title, WIDGET_PAYLOAD_CODE: code}
        )
        context.show_display_widget(widget_id, title)
        return tool_success({"content": _shown_text(title, context)})

    registry.register(
        SHOW_WIDGET_TOOL_NAME,
        SHOW_WIDGET_DESCRIPTION,
        SHOW_WIDGET_PARAMETERS,
        handler,
        summary="Show an interactive widget, such as sliders or a chart, in the chat.",
        family="media",
        open_input_schema=True,
        argument_normalizer=_normalize_arguments,
        result_schema={"type": "object", "required": ["content"]},
        display=ToolDisplay(
            summary_fields=(WIDGET_PAYLOAD_TITLE,),
            hidden_argument_keys=(WIDGET_PAYLOAD_CODE,),
            detail_builder=_details,
        ),
    )


def _shown_text(title: str, context: ToolContext) -> str:
    text = (
        f'Showed the widget "{title}" in the chat. When the user changes it and sends a '
        "message, its values arrive with that message."
    )
    surface = context.reply_surface
    if surface is None or surface.kind == "webui":
        return text
    where = surface.platform_display_name if surface.kind == "channel" else "the terminal"
    return (
        f"{text} The user is using {where}, where widgets do not show; they see it when they "
        "open this Session in the vBot app."
    )


def _details(arguments: JsonObject, _result: JsonObject | None) -> list[JsonObject]:
    return [display_text("content", source="arguments", path=(WIDGET_PAYLOAD_CODE,))]
