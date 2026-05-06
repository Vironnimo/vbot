"""Server startup entrypoint."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, TypedDict

from core.utils.config import Config
from server.app import create_app

_UVICORN_IMPORT_ERROR: ModuleNotFoundError | None

try:
    import uvicorn  # type: ignore[import-not-found]
except ModuleNotFoundError as exc:  # pragma: no cover - exercised when server extra is absent.
    uvicorn = None  # type: ignore[assignment]
    _UVICORN_IMPORT_ERROR = exc
else:
    _UVICORN_IMPORT_ERROR = None

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8420
PORT_SETTING_KEYS = ("server_port", "SERVER_PORT", "port", "PORT")


class ServerBind(TypedDict):
    """Resolved host/port metadata for server startup."""

    listen_host: str
    listen_port: int
    port_source: str


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse server CLI arguments."""
    parser = argparse.ArgumentParser(description="Start the vBot server")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int)
    parser.add_argument("--data-dir")
    return parser.parse_args(argv)


def resolve_port(config: Config, explicit_port: int | None = None) -> int:
    """Resolve port using --port > VBOT_SERVER_PORT > settings.json > default."""
    server_bind = resolve_server_bind(config, host=DEFAULT_HOST, explicit_port=explicit_port)
    return server_bind["listen_port"]


def resolve_server_bind(
    config: Config,
    *,
    host: str,
    explicit_port: int | None = None,
) -> ServerBind:
    """Resolve server bind metadata for startup and Settings reads."""

    if explicit_port is not None:
        return {
            "listen_host": host,
            "listen_port": explicit_port,
            "port_source": "cli",
        }

    environment_port = os.environ.get("VBOT_SERVER_PORT")
    if environment_port:
        return {
            "listen_host": host,
            "listen_port": _coerce_port(environment_port, source="VBOT_SERVER_PORT"),
            "port_source": "VBOT_SERVER_PORT",
        }

    settings = _load_settings_for_port(config)
    for key in PORT_SETTING_KEYS:
        value = settings.get(key)
        if value is not None:
            return {
                "listen_host": host,
                "listen_port": _coerce_port(value, source=f"settings.{key}"),
                "port_source": f"settings.{key}",
            }

    return {
        "listen_host": host,
        "listen_port": DEFAULT_PORT,
        "port_source": "default",
    }


def _load_settings_for_port(config: Config) -> dict[str, Any]:
    """Load settings.json directly so ambient environment cannot affect ports."""

    settings_path = config.data_dir / "settings.json"
    if not settings_path.exists():
        return {}

    data = json.loads(settings_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Expected a JSON object at {settings_path}")
    return data


def main(argv: list[str] | None = None) -> None:
    """Start uvicorn for the vBot FastAPI app."""
    if uvicorn is None:
        raise RuntimeError("uvicorn is required to start the server") from _UVICORN_IMPORT_ERROR
    args = parse_args(argv)
    data_dir = Path(args.data_dir) if args.data_dir else None
    config = Config(data_dir=data_dir)
    server_bind = resolve_server_bind(config, host=args.host, explicit_port=args.port)
    app = create_app(config=config, server_bind=server_bind)
    uvicorn.run(
        app,
        host=server_bind["listen_host"],
        port=server_bind["listen_port"],
        log_level="info",
    )


def _coerce_port(value: Any, *, source: str) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{source} must be an integer port") from exc
    if port < 1 or port > 65535:
        raise ValueError(f"{source} must be between 1 and 65535")
    return port


if __name__ == "__main__":
    main()
