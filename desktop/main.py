"""Desktop target configuration for the vBot pywebview accessor."""

from __future__ import annotations

import argparse
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8420
SETTINGS_FILE_NAME = "settings.json"


@dataclass(frozen=True)
class DesktopTarget:
    """Resolved Desktop server target."""

    host: str
    port: int
    url: str


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse Desktop CLI target arguments."""

    parser = argparse.ArgumentParser(description="Open the vBot desktop shell")
    parser.add_argument("--host")
    parser.add_argument("--port", type=_parse_port)
    return parser.parse_args(argv)


def desktop_dir() -> Path:
    """Return the source-run Desktop directory used for local settings."""

    return Path(__file__).resolve().parent


def settings_path(base_dir: Path | None = None) -> Path:
    """Return the Desktop-local settings path beside this entrypoint."""

    return (base_dir if base_dir is not None else desktop_dir()) / SETTINGS_FILE_NAME


def read_settings(path: Path | None = None) -> dict[str, Any]:
    """Read Desktop-local JSON settings, defaulting to empty settings."""

    resolved_path = path if path is not None else settings_path()
    if not resolved_path.exists():
        return {}

    data = json.loads(resolved_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Expected a JSON object at {resolved_path}")
    return data


def write_settings(settings: dict[str, Any], path: Path | None = None) -> None:
    """Persist Desktop-local settings with a same-directory atomic replace."""

    resolved_path = path if path is not None else settings_path()
    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(settings, indent=2, sort_keys=True) + "\n"

    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=resolved_path.parent,
        delete=False,
        prefix=f".{resolved_path.name}.",
        suffix=".tmp",
    ) as temporary_file:
        temporary_file.write(payload)
        temporary_path = Path(temporary_file.name)

    temporary_path.replace(resolved_path)


def resolve_target(
    argv: list[str] | None = None,
    *,
    settings_file: Path | None = None,
) -> DesktopTarget:
    """Resolve target from CLI args, Desktop settings, then defaults."""

    args = parse_args(argv)
    saved_settings = read_settings(settings_file)
    host = args.host if args.host is not None else str(saved_settings.get("host", DEFAULT_HOST))
    port_value = args.port if args.port is not None else saved_settings.get("port", DEFAULT_PORT)
    port = validate_port(port_value, source="settings.port" if args.port is None else "--port")

    target = DesktopTarget(host=host, port=port, url=build_target_url(host, port))
    write_settings({"host": target.host, "port": target.port}, settings_file)
    return target


def build_target_url(host: str, port: int) -> str:
    """Build the HTTP WebUI root URL for a resolved Desktop target."""

    return f"http://{host}:{port}/"


def validate_port(value: Any, *, source: str = "port") -> int:
    """Validate a TCP port value."""

    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{source} must be an integer port") from exc
    if port < 1 or port > 65535:
        raise ValueError(f"{source} must be between 1 and 65535")
    return port


def main(argv: list[str] | None = None) -> DesktopTarget:
    """Resolve and persist the Desktop target.

    Later Phase 6 work wires this target into probing and pywebview window creation.
    """

    return resolve_target(argv)


def _parse_port(value: str) -> int:
    """Argparse adapter for Desktop port validation."""

    try:
        return validate_port(value, source="--port")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


if __name__ == "__main__":
    main()
