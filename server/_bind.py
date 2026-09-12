"""Configured server bind resolution and browser-origin metadata."""

from __future__ import annotations

import logging
import os
from typing import Any, TypedDict

from core.settings import SettingsValidationError, load_runtime_settings_json
from core.utils.config import Config

JsonObject = dict[str, Any]

DEFAULT_SERVER_HOST = "127.0.0.1"

DEFAULT_SERVER_PORT = 8420

DEFAULT_SERVER_PORT_SOURCE = "default"


class ServerBindState(TypedDict):
    """Resolved bind metadata persisted in FastAPI app state."""

    listen_host: str
    listen_port: int
    port_source: str


def _resolve_server_bind(
    *, config: Config | None, server_bind: ServerBindState | None
) -> ServerBindState:
    if server_bind is not None:
        return {
            "listen_host": _coerce_bind_host(server_bind.get("listen_host")),
            "listen_port": _coerce_bind_port(
                server_bind.get("listen_port"),
                source="server_bind.listen_port",
            ),
            "port_source": _coerce_bind_port_source(server_bind.get("port_source")),
        }

    if config is None:
        return _default_server_bind()

    if environment_port := os.environ.get("VBOT_SERVER_PORT"):
        return {
            "listen_host": DEFAULT_SERVER_HOST,
            "listen_port": _coerce_bind_port(environment_port, source="VBOT_SERVER_PORT"),
            "port_source": "VBOT_SERVER_PORT",
        }

    settings_path = config.data_dir / "settings.json"
    try:
        data, ignored = load_runtime_settings_json(settings_path)
    except SettingsValidationError as exc:
        logging.getLogger("vbot.server.app").warning(
            "Ignoring invalid settings file %s for server bind and using the default port: %s",
            settings_path,
            exc,
        )
        return _default_server_bind()
    if ignored:
        details = "; ".join(f"{diagnostic.path}: {diagnostic.message}" for diagnostic in ignored)
        logging.getLogger("vbot.server.app").warning(
            "Ignoring invalid Settings keys in %s for server bind while keeping valid siblings: %s",
            settings_path,
            details,
        )
    if data:
        for key in ("server_port", "SERVER_PORT", "port", "PORT"):
            value = data.get(key)
            if value is not None:
                return {
                    "listen_host": DEFAULT_SERVER_HOST,
                    "listen_port": _coerce_bind_port(value, source=f"settings.{key}"),
                    "port_source": f"settings.{key}",
                }

    return _default_server_bind()


def _default_server_bind() -> ServerBindState:
    return {
        "listen_host": DEFAULT_SERVER_HOST,
        "listen_port": DEFAULT_SERVER_PORT,
        "port_source": DEFAULT_SERVER_PORT_SOURCE,
    }


def _runtime_config(runtime: Any) -> Config | None:
    """Read the runtime's public config when present — bind resolution runs pre-start."""
    config = getattr(runtime, "config", None)
    if isinstance(config, Config):
        return config
    return None


def _coerce_bind_host(value: Any) -> str:
    if not isinstance(value, str) or not value:
        return DEFAULT_SERVER_HOST
    return value


def _coerce_bind_port(value: Any, *, source: str) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{source} must be an integer port") from exc
    if port < 1 or port > 65535:
        raise ValueError(f"{source} must be between 1 and 65535")
    return port


def _coerce_bind_port_source(value: Any) -> str:
    if not isinstance(value, str) or not value:
        return DEFAULT_SERVER_PORT_SOURCE
    return value
