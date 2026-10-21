"""The ``portal`` Tool: show the user a web app that runs on this computer.

The Agent starts the app itself, such as a dev server as a background command,
and opens a Portal for its port. The call's display carries the Portal's id;
the WebUI shows a card after the call with the Portal's live state and opens
it with a credential only the WebUI receives (``.vorch/domain-maps/tools/portal.md``).
"""

from __future__ import annotations

from functools import cache
from typing import Any
from urllib.parse import urlsplit

from core.sessions import SessionAddress
from core.tools.call_syntax import SpellingAliases, is_placeholder, normalize_call_arguments
from core.tools.contracts import ToolContract, ToolContractError, compile_tool_contract
from core.tools.portal_registry import (
    MAX_PORTALS_PER_SESSION,
    Portal,
    PortalError,
    PortalRegistry,
    describe_portals,
)
from core.tools.tools import (
    JsonObject,
    ToolContext,
    ToolDisplay,
    ToolRegistry,
    tool_failure,
    tool_success,
)

PORTAL_TOOL_NAME = "portal"
PORTAL_ACTIONS = ("open", "list", "close")
MAX_PORTAL_TITLE_LENGTH = 120
MAX_PORTAL_PATH_LENGTH = 2048
REFUSAL_PREFIX = "portal was not run: "

PORTAL_DESCRIPTION = (
    "Show the user a web app that runs on this computer, such as a dev server you started, "
    "through a private link in their chat. The portal only forwards to the app's port; it "
    "never starts or stops the app."
)
PORTAL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": list(PORTAL_ACTIONS)},
        "port": {
            "type": "integer",
            "description": (
                "The localhost port the app listens on, such as 5173. Required for open."
            ),
        },
        "title": {
            "type": "string",
            "description": 'Short label on the card. Omit to use "localhost:<port>".',
        },
        "path": {
            "type": "string",
            "description": "Page the card opens first, such as /docs. Omit for /.",
        },
        "id": {
            "type": "string",
            "description": "The portal id from open or list. Required for close.",
        },
    },
    "required": ["action"],
}

_FIELD_ALIASES = SpellingAliases(
    {
        "port": ("target_port", "local_port", "app_port", "server_port"),
        "id": ("portal_id", "portal"),
        "title": ("name", "label"),
        "path": ("url_path", "route", "page"),
    }
)
_ACTION_SYNONYMS = SpellingAliases(
    {
        "open": ("create", "start", "add", "new", "show", "expose", "share"),
        "list": ("ls", "status", "all", "get"),
        "close": ("stop", "remove", "delete", "end", "destroy"),
    }
)
# A whole address instead of port and path, such as "http://localhost:5173/docs".
_URL_FIELDS = ("url", "address", "target", "target_url", "local_url")
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0", "[::1]"})


@cache
def _repair_contract() -> ToolContract:
    return compile_tool_contract(
        name=PORTAL_TOOL_NAME, input_schema=PORTAL_PARAMETERS, require_closed_input=False
    )


def _action_word(value: Any) -> Any:
    return _ACTION_SYNONYMS.get(value, value) if isinstance(value, str) else value


def _normalize_arguments(arguments: Any) -> Any:
    if isinstance(arguments, dict):
        arguments = dict(arguments)
        for key in [key for key in arguments if key.casefold() in _URL_FIELDS]:
            _take_url(arguments, arguments.pop(key))
    normalized = normalize_call_arguments(
        _repair_contract(),
        arguments,
        enum_fields=("action",),
        field_aliases=_FIELD_ALIASES,
        field_normalizers={"action": _action_word},
        empty_as_omitted=("port", "title", "path", "id"),
    )
    if not isinstance(normalized, dict):
        return normalized
    title = normalized.get("title")
    if isinstance(title, str):
        title = " ".join(title.split())[:MAX_PORTAL_TITLE_LENGTH]
        if title:
            normalized["title"] = title
        else:
            normalized.pop("title")
    path = normalized.get("path")
    if isinstance(path, str):
        normalized["path"] = _page_path(path)
    return normalized


def _take_url(arguments: dict[str, Any], value: Any) -> None:
    """Read port and path from a local address the call sent in place of them."""
    if not isinstance(value, str) or is_placeholder(value):
        return
    text = value.strip()
    if "://" not in text:
        text = f"http://{text}"
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        port = None
    if port is None or (parts.hostname or "") not in _LOCAL_HOSTS:
        raise ToolContractError(
            f"{REFUSAL_PREFIX}{value!r} is not a localhost address with a port. Pass port, "
            "the localhost port the app listens on."
        )
    arguments.setdefault("port", port)
    if parts.path not in ("", "/") or parts.query:
        arguments.setdefault("path", parts.path + (f"?{parts.query}" if parts.query else ""))


def _page_path(value: str) -> str:
    path = value.strip()
    if "://" in path:
        parts = urlsplit(path)
        path = parts.path + (f"?{parts.query}" if parts.query else "")
    if not path.startswith("/"):
        path = f"/{path}"
    if len(path) > MAX_PORTAL_PATH_LENGTH or any(ord(char) < 0x21 for char in path):
        raise ToolContractError(
            f"{REFUSAL_PREFIX}path must be a page of the app such as /docs, without spaces."
        )
    return path


def register_portal_tool(registry: ToolRegistry, portals: PortalRegistry) -> None:
    """Register ``portal`` over the Runtime's Portal registry."""

    async def handler(context: ToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
        action = arguments["action"]
        if not context.result_payloads_available:
            return tool_failure(
                "session_required",
                "Nothing was changed. Portals belong to a chat Session, and this call runs "
                "outside one.",
            )
        owner = SessionAddress(context.project_id, context.agent_id, context.session_id)
        if action == "list":
            return tool_success({"content": _list_text(portals, owner)})
        if action == "close":
            return _close(portals, owner, arguments.get("id"))
        return await _open(portals, owner, arguments, context)

    registry.register(
        PORTAL_TOOL_NAME,
        PORTAL_DESCRIPTION,
        PORTAL_PARAMETERS,
        handler,
        summary="Show a web app running on this computer, such as a dev server, to the user.",
        family="media",
        open_input_schema=True,
        argument_normalizer=_normalize_arguments,
        result_schema={"type": "object", "required": ["content"]},
        display=ToolDisplay(summary_fields=("action", "port", "title")),
    )


async def _open(
    portals: PortalRegistry, owner: SessionAddress, arguments: dict[str, Any], context: ToolContext
) -> dict[str, Any]:
    port = arguments.get("port")
    if port is None:
        return tool_failure(
            "invalid_arguments",
            "Nothing was opened. portal open needs port, the localhost port the app listens "
            "on, such as 5173.",
        )
    if not 1 <= port <= 65535:
        return tool_failure(
            "invalid_arguments",
            f"Nothing was opened. Port {port} does not exist; pass a port from 1 to 65535.",
        )
    title = arguments.get("title") or f"localhost:{port}"
    path = arguments.get("path") or "/"
    try:
        portal, opened = await portals.open(owner, target_port=port, title=title, path=path)
    except PortalError as exc:
        return tool_failure(exc.code, _open_refusal(exc, port, portals.list(owner)))
    context.show_display_portal(portal.id, portal.title, portal.target_port)
    if opened:
        text = (
            f"Opened portal {portal.id} for localhost:{port}. The user opens it from the card "
            "in the chat."
        )
    else:
        text = f"Portal {portal.id} already shows localhost:{port}."
    if not portal.listening:
        text += f' Nothing answers on port {port} yet; the card shows "waiting" until the app does.'
    return tool_success({"content": text + _surface_note(context)})


def _open_refusal(error: PortalError, port: int, existing: list[Portal]) -> str:
    if error.code == "own_port":
        return f"Nothing was opened. Port {port} is vBot itself; pass the port of the app."
    if error.code == "limit" and len(existing) >= MAX_PORTALS_PER_SESSION:
        return (
            f"Nothing was opened. This Session already has {MAX_PORTALS_PER_SESSION} portals "
            f"({describe_portals(existing)}); close one with action close first."
        )
    if error.code == "limit":
        return f"Nothing was opened. {error}; the user can close portals from their cards."
    return "Nothing was opened. Portals need the vBot server, and this Session runs without it."


def _close(portals: PortalRegistry, owner: SessionAddress, portal_id: Any) -> dict[str, Any]:
    mine = portals.list(owner)
    if not isinstance(portal_id, str):
        return tool_failure(
            "invalid_arguments",
            f"Nothing was closed. portal close needs id; this Session's portals: "
            f"{describe_portals(mine)}.",
        )
    portal = portals.get(portal_id)
    if portal is None or portal.owner != owner:
        return tool_failure(
            "portal_not_found",
            f"Nothing was closed. No portal {portal_id} is open in this Session; its portals: "
            f"{describe_portals(mine)}.",
        )
    portals.close(portal_id)
    return tool_success(
        {
            "content": (
                f"Closed portal {portal_id}. The app on localhost:{portal.target_port} keeps "
                "running."
            )
        }
    )


def _list_text(portals: PortalRegistry, owner: SessionAddress) -> str:
    mine = portals.list(owner)
    if not mine:
        return "No portals are open in this Session."
    lines = ["Portals of this Session:"]
    for portal in mine:
        state = "app answers" if portal.listening else "nothing answers yet"
        lines.append(f'- {portal.id} "{portal.title}": localhost:{portal.target_port} ({state})')
    return "\n".join(lines)


def _surface_note(context: ToolContext) -> str:
    surface = context.reply_surface
    if surface is None or surface.kind == "webui":
        return ""
    where = surface.platform_display_name if surface.kind == "channel" else "the terminal"
    return (
        f" The user is using {where}, where the card does not show; they open the portal "
        "from this Session in the vBot app."
    )
