"""The portal built-in Tool and its registry: what it opens, refuses, lists and closes."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from core.chat import ReplySurface
from core.sessions import SessionAddress
from core.tools.portal import PORTAL_TOOL_NAME, register_portal_tool
from core.tools.portal_registry import MAX_PORTALS_PER_SESSION, PortalRegistry
from core.tools.tools import ToolRegistry
from tests.core.tools.image_test_support import make_context
from tests.core.tools.tools_test_support import dispatch_as_executor

_VBOT_PORT = 22335


class _Listeners:
    """Stands in for the server's listeners: hands out ports and records closes."""

    own_ports = frozenset({_VBOT_PORT})

    def __init__(self) -> None:
        self.next_port = 41000
        self.closed: list[str] = []

    async def open(self, _portal_id: str, _target_port: int) -> int:
        self.next_port += 1
        return self.next_port

    def close(self, portal_id: str) -> None:
        self.closed.append(portal_id)

    def address(self, listen_port: int, path: str) -> str:
        return f"http://localhost:{listen_port}{path}"


def _setup() -> tuple[ToolRegistry, PortalRegistry, _Listeners]:
    # Only port 5173 has an app listening.
    portals = PortalRegistry(probe=lambda port: asyncio.sleep(0, result=port == 5173))
    listeners = _Listeners()
    portals.attach(listeners)
    tools = ToolRegistry()
    register_portal_tool(tools, portals)
    return tools, portals, listeners


async def _call(
    tools: ToolRegistry, tmp_path: Path, arguments: dict[str, Any], **context: Any
) -> tuple[dict[str, Any], Any]:
    context.setdefault("result_payload_hook", lambda *_args: "res")
    tool_context = make_context(tmp_path, PORTAL_TOOL_NAME, **context)
    result = await dispatch_as_executor(tools, tool_context, arguments)
    return result, tool_context


@pytest.mark.asyncio
async def test_open_shows_a_card_and_tells_the_address_without_the_credential(
    tmp_path: Path,
) -> None:
    tools, portals, _listeners = _setup()

    opened, context = await _call(
        tools, tmp_path, {"action": "open", "url": "http://localhost:5173/docs", "title": "Docs"}
    )
    again, _context = await _call(tools, tmp_path, {"action": "open", "port": 5173, "path": "docs"})
    waiting, _context = await _call(tools, tmp_path, {"action": "open", "port": 3000})
    listed, _context = await _call(tools, tmp_path, {"action": "list"})

    [first, second] = portals.list(SessionAddress(None, context.agent_id, context.session_id))
    assert context.presentation_portal == {"id": first.id, "title": "Docs", "port": 5173}
    assert opened["data"]["content"] == (
        f"Opened portal {first.id} for localhost:5173 at http://localhost:41001/docs. The user "
        "opens it from the card in the chat, which carries their access; the address alone "
        'answers "private".'
    )
    assert again["data"]["content"] == (
        f"Portal {first.id} already shows localhost:5173 at http://localhost:41001/docs."
    )
    assert waiting["data"]["content"].endswith(
        'Nothing answers on port 3000 yet; the card shows "waiting" until the app does.'
    )
    assert second.title == "localhost:3000" and second.listening is False
    assert listed["data"]["content"] == (
        "Portals of this Session:\n"
        f'- {first.id} "Docs": localhost:5173, http://localhost:41001/docs (app answers)\n'
        f'- {second.id} "localhost:3000": localhost:3000, http://localhost:41002/ '
        "(nothing answers yet)"
    )
    await portals.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "context", "code", "message"),
    [
        ({"action": "open", "port": _VBOT_PORT}, {}, "own_port", "Port 22335 is vBot itself"),
        ({"action": "open"}, {}, "invalid_arguments", "portal open needs port"),
        ({"action": "close", "id": "p00000000"}, {}, "portal_not_found", "No portal p00000000"),
        (
            {"action": "open", "port": 5173},
            {"result_payload_hook": None},
            "session_required",
            "Portals belong to a chat Session",
        ),
    ],
    ids=["vbot-port", "no-port", "unknown-id", "no-session"],
)
async def test_refused_calls_change_nothing_and_say_why(
    tmp_path: Path, arguments: dict[str, Any], context: dict[str, Any], code: str, message: str
) -> None:
    tools, _portals, listeners = _setup()

    result, _context = await _call(tools, tmp_path, arguments, **context)

    assert result["error"]["code"] == code
    assert message in result["error"]["message"]
    assert result["error"]["message"].startswith("Nothing was")
    assert listeners.next_port == 41000


@pytest.mark.asyncio
async def test_a_session_holds_a_bounded_number_of_portals_until_one_closes(
    tmp_path: Path,
) -> None:
    tools, portals, listeners = _setup()
    for port in range(3000, 3000 + MAX_PORTALS_PER_SESSION):
        await _call(tools, tmp_path, {"action": "open", "port": port})

    refused, _context = await _call(tools, tmp_path, {"action": "open", "port": 4000})
    first_id = refused["error"]["message"].split("(")[1].split(",")[0]
    closed, _context = await _call(tools, tmp_path, {"action": "close", "id": first_id})
    reopened, _context = await _call(tools, tmp_path, {"action": "open", "port": 4000})

    assert refused["error"]["code"] == "limit"
    assert closed["data"]["content"] == (
        f"Closed portal {first_id}. The app on localhost:3000 keeps running."
    )
    assert listeners.closed == [first_id]
    assert reopened["data"]["content"].startswith("Opened portal ")
    await portals.aclose()


@pytest.mark.asyncio
async def test_archiving_sessions_closes_only_their_portals() -> None:
    portals = PortalRegistry(probe=lambda _port: asyncio.sleep(0, result=True))
    listeners = _Listeners()
    portals.attach(listeners)
    changed: list[SessionAddress] = []
    portals.subscribe(changed.append)
    archived = SessionAddress(None, "coder", "old")
    kept = SessionAddress("shop", "coder", "current")
    gone, _opened = await portals.open(archived, target_port=5173, title="t", path="/")
    stays, _opened = await portals.open(kept, target_port=5173, title="t", path="/")

    assert portals.close_sessions([archived]) == 1

    assert listeners.closed == [gone.id]
    assert portals.list(kept) == [stays]
    assert changed == [archived, kept, archived]
    await portals.aclose()
    assert listeners.closed == [gone.id, stays.id]


@pytest.mark.asyncio
async def test_a_user_outside_the_webui_learns_where_the_card_shows(tmp_path: Path) -> None:
    tools, portals, _listeners = _setup()
    surface = ReplySurface.channel(
        platform="telegram", platform_display_name="Telegram", channel_id="c"
    )

    result, _context = await _call(
        tools, tmp_path, {"action": "open", "port": 5173}, reply_surface=surface
    )

    assert result["data"]["content"].endswith(
        "The user is using Telegram, where the card does not show; they open the portal from "
        "this Session in the vBot app."
    )
    await portals.aclose()
