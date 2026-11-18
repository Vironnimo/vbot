"""Run the normal CLI for the server that owns an App-Agent's Tool call."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from cli._server_target import ServerInstance, parse_server_url, resolve_instance
from core.access.local_token import read_local_access


def bind_target(args: argparse.Namespace, instance: ServerInstance) -> None:
    """Validate parsed selectors, leaving command operands and Extension payloads intact."""
    selected = getattr(args, "_target_arguments", {})
    url = parse_server_url(selected["url"]).origin if "url" in selected else instance.url
    if (
        selected.get("host", instance.host) != instance.host
        or selected.get("port", instance.port) != instance.port
        or url != instance.url
        or Path(selected.get("data_dir", instance.data_dir)).expanduser().resolve()
        != instance.data_dir
    ):
        raise ValueError(
            "This Tool runs commands on the current vBot instance. Omit the target options."
        )
    # Pin even commands whose normal default is the last-used Desktop or the installation.
    for name in ("host", "port", "data_dir"):
        if hasattr(args, name):
            setattr(
                args,
                name,
                str(instance.data_dir) if name == "data_dir" else getattr(instance, name),
            )


def main() -> int:
    from cli._commands import run
    from cli.main import _configure_console_output

    _configure_console_output()
    data_dir = Path(os.environ["VBOT_DATA_DIR"])
    port = int(os.environ["VBOT_SERVER_PORT"])
    listen_host = os.environ.pop("VBOT_APP_CLI_LISTEN_HOST")
    local = read_local_access(data_dir, port)
    # A wildcard or localhost bind can resolve to several interfaces. The server's
    # access record names the loopback literal on which it actually accepts the CLI.
    host = local.loopback[0] if local and local.loopback else listen_host
    target = resolve_instance(host=host, port=port, data_dir=data_dir)
    return run(app_instance=target)


if __name__ == "__main__":
    raise SystemExit(main())
