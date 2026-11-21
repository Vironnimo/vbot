"""Native speech enablement belongs to the exact origin receiving recordings."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from desktop._windows import url_origin


class SpeechDestination:
    def __init__(self, url: str, own_server: Callable[[str], bool]) -> None:
        self.origin = url_origin(url)
        self._own_server = own_server

    def enabled(self, settings: dict[str, Any]) -> bool:
        if self.origin is None:
            return False
        grants = settings.get("enabled_servers", {})
        if not isinstance(grants, dict):
            return False
        if self.origin in grants:
            return grants[self.origin] is True
        # A pre-origin preference only authorized the installation's own server.
        return settings.get("enabled") is True and self._own_server(self.origin)

    def set_enabled(self, settings: dict[str, Any], enabled: bool) -> None:
        if self.origin is None:
            return
        grants = settings.get("enabled_servers", {})
        settings["enabled_servers"] = {
            **(grants if isinstance(grants, dict) else {}),
            self.origin: enabled,
        }
