"""The Agent widget route: one widget's document by capability."""

from __future__ import annotations

from core.tools import builtin_payload_owner
from core.tools.show_widget import (
    SHOW_WIDGET_TOOL_NAME,
    WIDGET_PAYLOAD_CODE,
    WIDGET_PAYLOAD_TITLE,
)
from server._http_dependencies import FastAPIType, Request, Response
from server.file_delivery import WIDGET_URL_PREFIX
from server.widget_document import unavailable_widget_document, widget_document

_WIDGET_OWNER = builtin_payload_owner(SHOW_WIDGET_TOOL_NAME)


def register_widget_routes(app: FastAPIType) -> None:
    @app.get(WIDGET_URL_PREFIX + "{token}")
    async def get_widget(request: Request, token: str) -> Response:
        """Serve a widget while a Session keeps it; it can reach nothing on the server."""
        delivery = request.app.state.file_delivery
        headers = delivery.widget_headers()
        widget_id = delivery.widget_id(token)
        payload = (
            None
            if widget_id is None
            else await request.app.state.runtime.chat_sessions.tool_result_payload_by_id_async(
                widget_id, owner_name=_WIDGET_OWNER
            )
        )
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get(WIDGET_PAYLOAD_TITLE), str)
            or not isinstance(payload.get(WIDGET_PAYLOAD_CODE), str)
        ):
            return Response(
                unavailable_widget_document(),
                status_code=404,
                media_type="text/html",
                headers=headers,
            )
        return Response(
            widget_document(payload[WIDGET_PAYLOAD_TITLE], payload[WIDGET_PAYLOAD_CODE]),
            media_type="text/html",
            headers=headers,
        )
