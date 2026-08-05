"""Server startup entrypoint."""

from __future__ import annotations

import argparse
from pathlib import Path

from core.tools.process_manager import activate_process_containment
from core.utils.config import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    PORT_SETTING_KEYS,
    Config,
    ServerBind,
    resolve_port,
    resolve_server_bind,
)
from core.utils.logging import build_uvicorn_log_config
from core.utils.server_control import create_server_control, remove_server_control
from server.app import create_app

# Re-exported from core.utils.config so existing `from server.main import ...`
# consumers (and the server tests) keep working after the resolver moved to core.
__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "PORT_SETTING_KEYS",
    "ServerBind",
    "main",
    "parse_args",
    "resolve_port",
    "resolve_server_bind",
]

_UVICORN_IMPORT_ERROR: ModuleNotFoundError | None

try:
    import uvicorn  # type: ignore[import-not-found]
except ModuleNotFoundError as exc:  # pragma: no cover - exercised when server extra is absent.
    uvicorn = None  # type: ignore[assignment]
    _UVICORN_IMPORT_ERROR = exc
else:
    _UVICORN_IMPORT_ERROR = None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse server CLI arguments."""
    parser = argparse.ArgumentParser(description="Start the vBot server")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int)
    parser.add_argument("--data-dir")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    """Start uvicorn for the vBot FastAPI app."""
    if uvicorn is None:
        raise RuntimeError("uvicorn is required to start the server") from _UVICORN_IMPORT_ERROR
    args = parse_args(argv)
    data_dir = Path(args.data_dir) if args.data_dir else None
    config = Config(data_dir=data_dir)
    server_bind = resolve_server_bind(config, host=args.host, explicit_port=args.port)
    activate_process_containment()
    control = create_server_control(config.data_dir, server_bind["listen_port"])
    try:
        server_holder: dict[str, object] = {}

        def request_shutdown() -> None:
            server = server_holder.get("server")
            if server is not None:
                server.should_exit = True  # type: ignore[attr-defined]

        app = create_app(
            config=config,
            server_bind=server_bind,
            shutdown_token=control.token,
            request_shutdown=request_shutdown,
        )
        uvicorn_config = uvicorn.Config(
            app,
            host=server_bind["listen_host"],
            port=server_bind["listen_port"],
            log_level="info",
            access_log=False,
            log_config=build_uvicorn_log_config(),
        )
        server = uvicorn.Server(uvicorn_config)
        server_holder["server"] = server
        server.run()
    finally:
        remove_server_control(control)


if __name__ == "__main__":
    main()
