"""Configured server bind resolution and browser-origin metadata."""

from __future__ import annotations

from typing import Any, TypedDict

from core.utils.config import DEFAULT_HOST, DEFAULT_PORT, Config, resolve_server_bind

JsonObject = dict[str, Any]

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

    return resolve_server_bind(config, host=DEFAULT_HOST)


def _default_server_bind() -> ServerBindState:
    return {
        "listen_host": DEFAULT_HOST,
        "listen_port": DEFAULT_PORT,
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
        return DEFAULT_HOST
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
