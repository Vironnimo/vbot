"""Desktop target configuration and probing for the vBot pywebview accessor."""

from __future__ import annotations

import argparse
import html
import json
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8420
SETTINGS_FILE_NAME = "settings.json"
PROBE_TIMEOUT_SECONDS = 2.0
PROBE_WEBUI_AVAILABLE = "webui_available"
PROBE_WEBUI_UNAVAILABLE = "webui_unavailable"
PROBE_SERVER_UNREACHABLE = "server_unreachable"
PROBE_NOT_VBOT_SERVER = "not_vbot_server"


class HttpResponse(Protocol):
    """Subset of an HTTP response used by Desktop probing."""

    status_code: int

    def json(self) -> Any:
        """Return the decoded JSON body."""


class HttpGet(Protocol):
    """Synchronous HTTP GET callable used by Desktop probing."""

    def __call__(self, url: str, *, timeout: float) -> HttpResponse:
        """Fetch a URL with a bounded timeout."""


@dataclass(frozen=True)
class DesktopTarget:
    """Resolved Desktop server target."""

    host: str
    port: int
    url: str


@dataclass(frozen=True)
class DesktopProbeResult:
    """Result of probing a target vBot server and its WebUI root."""

    status: str
    target: DesktopTarget


@dataclass(frozen=True)
class DesktopWindowContent:
    """Window content selected before pywebview window creation."""

    status: str
    url: str | None = None
    html: str | None = None


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


def probe_target(
    target: DesktopTarget,
    *,
    get: HttpGet = httpx.get,
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> DesktopProbeResult:
    """Classify the configured server target before Desktop window creation."""

    health_url = f"{target.url.rstrip('/')}/health"
    try:
        health_response = get(health_url, timeout=timeout)
    except httpx.RequestError:
        return DesktopProbeResult(status=PROBE_SERVER_UNREACHABLE, target=target)

    if health_response.status_code != 200 or not _is_vbot_health_response(health_response):
        return DesktopProbeResult(status=PROBE_NOT_VBOT_SERVER, target=target)

    try:
        webui_response = get(target.url, timeout=timeout)
    except httpx.RequestError:
        return DesktopProbeResult(status=PROBE_WEBUI_UNAVAILABLE, target=target)

    if 200 <= webui_response.status_code <= 399:
        return DesktopProbeResult(status=PROBE_WEBUI_AVAILABLE, target=target)
    return DesktopProbeResult(status=PROBE_WEBUI_UNAVAILABLE, target=target)


def choose_window_content(
    target: DesktopTarget,
    *,
    probe: Callable[[DesktopTarget], DesktopProbeResult] = probe_target,
) -> DesktopWindowContent:
    """Return URL content for a valid WebUI target, otherwise safe inline HTML."""

    probe_result = probe(target)
    if probe_result.status == PROBE_WEBUI_AVAILABLE:
        return DesktopWindowContent(status=probe_result.status, url=target.url)

    return DesktopWindowContent(
        status=probe_result.status,
        html=build_fallback_html(probe_result),
    )


def build_fallback_html(probe_result: DesktopProbeResult) -> str:
    """Build escaped inline HTML for expected Desktop connection failures."""

    target = probe_result.target
    escaped_host = html.escape(target.host)
    escaped_port = html.escape(str(target.port))
    escaped_url = html.escape(target.url)
    title, body, hint = _fallback_copy(probe_result.status)

    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{html.escape(title)} · vBot</title>
    <style>
      :root {{
        color-scheme: dark;
        background: #15130f;
        color: #f1eadf;
        font-family: "Trebuchet MS", Verdana, sans-serif;
      }}
      body {{
        min-height: 100vh;
        margin: 0;
        display: grid;
        place-items: center;
        background:
          radial-gradient(circle at 20% 10%, rgba(240, 164, 58, 0.18), transparent 32rem),
          #15130f;
      }}
      main {{
        width: min(42rem, calc(100vw - 3rem));
        padding: 2rem;
        border: 1px solid rgba(240, 164, 58, 0.28);
        border-radius: 1.5rem;
        background: #211d17;
        box-shadow: 0 1.5rem 5rem rgba(0, 0, 0, 0.36);
      }}
      .eyebrow {{
        margin: 0 0 0.65rem;
        color: #f0a43a;
        font-size: 0.75rem;
        font-weight: 700;
        letter-spacing: 0.16em;
        text-transform: uppercase;
      }}
      h1 {{
        margin: 0 0 1rem;
        font-family: Georgia, "Times New Roman", serif;
        font-size: clamp(2.25rem, 7vw, 4rem);
        line-height: 0.98;
      }}
      p {{
        margin: 0 0 1rem;
        color: #b8aa95;
        font-size: 1rem;
        line-height: 1.6;
      }}
      dl {{
        display: grid;
        grid-template-columns: max-content 1fr;
        gap: 0.65rem 1rem;
        margin: 1.5rem 0 0;
        padding: 1rem;
        border-radius: 1rem;
        background: #2d271f;
      }}
      dt {{ color: #847762; }}
      dd {{ margin: 0; overflow-wrap: anywhere; }}
    </style>
  </head>
  <body>
    <main>
      <p class="eyebrow">vBot Desktop</p>
      <h1>{html.escape(title)}</h1>
      <p>{html.escape(body)}</p>
      <p>{html.escape(hint)}</p>
      <dl aria-label="Desktop target">
        <dt>Host</dt><dd>{escaped_host}</dd>
        <dt>Port</dt><dd>{escaped_port}</dd>
        <dt>URL</dt><dd>{escaped_url}</dd>
      </dl>
    </main>
  </body>
</html>
"""


def validate_port(value: Any, *, source: str = "port") -> int:
    """Validate a TCP port value."""

    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{source} must be an integer port") from exc
    if port < 1 or port > 65535:
        raise ValueError(f"{source} must be between 1 and 65535")
    return port


def _is_vbot_health_response(response: HttpResponse) -> bool:
    """Return whether /health matches the vBot server identity contract."""

    try:
        payload = response.json()
    except ValueError:
        return False
    return isinstance(payload, dict) and payload.get("status") == "ok"


def _fallback_copy(status: str) -> tuple[str, str, str]:
    """Return title/body/help copy for a Desktop probe status."""

    if status == PROBE_SERVER_UNREACHABLE:
        return (
            "Server unreachable",
            "vBot Desktop could not connect to the configured server target.",
            "Start a vBot server or choose a different host and port, then open Desktop again.",
        )
    if status == PROBE_WEBUI_UNAVAILABLE:
        return (
            "WebUI unavailable",
            "The vBot server is reachable, but it is not serving the WebUI at the root path.",
            "Build or deploy the WebUI for this server, then reopen Desktop.",
        )
    if status == PROBE_NOT_VBOT_SERVER:
        return (
            "Not a vBot server",
            "Something responded at the configured target, but /health did not match vBot.",
            "Use a host and port for a running vBot server.",
        )
    raise ValueError(f"Unsupported Desktop probe status: {status}")


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
