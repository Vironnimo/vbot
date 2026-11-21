"""Keep a Channel's socket at the origin that supplied its protocol endpoint."""

from typing import override

from websockets.asyncio.client import connect
from websockets.uri import parse_uri


class ChannelConnection(connect):
    @override
    def process_redirect(self, exc: Exception) -> Exception | str:
        redirect = super().process_redirect(exc)
        if isinstance(redirect, str):
            current = self.ws_uri
            destination = parse_uri(redirect)
            if (current.secure, current.host, current.port) != (
                destination.secure,
                destination.host,
                destination.port,
            ):
                return exc
        return redirect
