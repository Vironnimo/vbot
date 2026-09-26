"""Server startup entrypoint."""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
from typing import Any, Literal

from core.storage.layout import initialize_data_directory
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
from core.utils.processes import activate_process_containment
from core.utils.server_control import (
    create_server_control,
    remove_server_control,
    server_control_claim,
)
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
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--verification-only", action="store_true")
    modes.add_argument("--test-instance", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    """Start uvicorn for the vBot FastAPI app."""
    if uvicorn is None:
        raise RuntimeError("uvicorn is required to start the server") from _UVICORN_IMPORT_ERROR
    args = parse_args(argv)
    data_dir = Path(args.data_dir) if args.data_dir else None
    config = Config(data_dir=data_dir)
    safe_startup_mode: Literal["verification", "test"] | None = (
        "verification" if args.verification_only else "test" if args.test_instance else None
    )
    server_bind = resolve_server_bind(config, host=args.host, explicit_port=args.port)
    activate_process_containment()
    if not config.data_dir.expanduser().exists():
        # The control claim writes into the data directory. A fresh root must come
        # from the canonical layout so it carries the Session store's bootstrap
        # marker; an existing root without that marker is refused at startup.
        initialize_data_directory(config.data_dir, resources_dir=config.get("RESOURCES_PATH"))
    with server_control_claim(config.data_dir, server_bind["listen_port"]):
        control = create_server_control(config.data_dir, server_bind["listen_port"])
        try:
            server_holder: dict[str, object] = {}

            def request_shutdown() -> None:
                server = server_holder.get("server")
                if server is not None:
                    server.should_exit = True  # type: ignore[attr-defined]

            restart_scheduled = False

            def request_restart() -> None:
                nonlocal restart_scheduled
                if restart_scheduled:
                    return
                from cli.server_management import resolve_instance, schedule_server_restart

                instance = resolve_instance(
                    host=server_bind["listen_host"],
                    port=server_bind["listen_port"],
                    data_dir=config.data_dir,
                )
                result = schedule_server_restart(instance, wait_pid=os.getpid())
                if not result.ok:
                    raise RuntimeError("restart_unavailable")
                restart_scheduled = True
                # Let the RPC response reach the client before cooperative teardown.
                asyncio.get_running_loop().call_later(0.5, request_shutdown)

            app_kwargs: dict[str, Any] = {
                "config": config,
                "server_bind": server_bind,
                "shutdown_token": control.token,
                "request_shutdown": request_shutdown,
                "request_restart": request_restart,
            }
            if safe_startup_mode is not None:
                app_kwargs["safe_startup_mode"] = safe_startup_mode
            app = create_app(**app_kwargs)
            uvicorn_config = uvicorn.Config(
                app,
                host=server_bind["listen_host"],
                port=server_bind["listen_port"],
                # Keep synchronous WebSocket compression off the shared Event Loop.
                ws_per_message_deflate=False,
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
