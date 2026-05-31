"""Desktop target configuration and probing for the vBot pywebview accessor."""

from __future__ import annotations

import argparse
import html
import importlib
import json
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8420
SETTINGS_FILE_NAME = "settings.json"
WINDOW_TITLE = "vBot"
ICON_FILE_NAME = "icon.png"
PROBE_TIMEOUT_SECONDS = 2.0
PROBE_WEBUI_AVAILABLE = "webui_available"
PROBE_WEBUI_UNAVAILABLE = "webui_unavailable"
PROBE_SERVER_UNREACHABLE = "server_unreachable"
PROBE_NOT_VBOT_SERVER = "not_vbot_server"
PROBE_INVALID_TARGET = "invalid_target"
INVALID_HOST_CHARACTERS = frozenset("/\\:?#@[]")
ACCESSOR_QUERY_PARAM = "accessor=desktop"

DEFAULT_WAKEWORD_SETTINGS: dict[str, Any] = {
    "enabled": False,
    "engine": "openwakeword",
    "microphone": None,
    "sensitivity": 0.5,
    "target_agent_id": None,
    "session_behavior": "active",
    "wake_phrase": "hey_jarvis",
}


class HttpResponse(Protocol):
    """Subset of an HTTP response used by Desktop probing."""

    status_code: int

    def json(self) -> Any:
        """Return the decoded JSON body."""


class HttpGet(Protocol):
    """Synchronous HTTP GET callable used by Desktop probing."""

    def __call__(self, url: str, *, timeout: float) -> HttpResponse:
        """Fetch a URL with a bounded timeout."""


class WebviewModule(Protocol):
    """Subset of pywebview used by the Desktop shell."""

    def create_window(self, title: str, **kwargs: Any) -> Any:
        """Create a window before the GUI loop starts."""

    def start(self, **kwargs: Any) -> Any:
        """Start the native GUI loop."""


@dataclass(frozen=True)
class DesktopTarget:
    """Resolved Desktop server target."""

    host: str
    port: int
    url: str
    configuration_error: str | None = None


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
    parser.add_argument(
        "--mock-wakeword",
        action="store_true",
        help="Use a mock wakeword engine for UI validation without a real microphone.",
    )
    return parser.parse_args(argv)


def desktop_dir() -> Path:
    """Return the source-run Desktop directory used for local settings."""

    return Path(__file__).resolve().parent


def settings_path(base_dir: Path | None = None) -> Path:
    """Return the Desktop-local settings path beside this entrypoint."""

    return (base_dir if base_dir is not None else desktop_dir()) / SETTINGS_FILE_NAME


def icon_path(base_dir: Path | None = None) -> Path:
    """Return the optional source-run Desktop icon path."""

    return (base_dir if base_dir is not None else desktop_dir()) / ICON_FILE_NAME


def read_settings(path: Path | None = None) -> dict[str, Any]:
    """Read Desktop-local JSON settings, defaulting to empty settings."""

    resolved_path = path if path is not None else settings_path()
    if not resolved_path.exists():
        return {}

    for attempt in range(3):
        try:
            data = json.loads(resolved_path.read_text(encoding="utf-8"))
        except PermissionError:
            if attempt < 2:
                time.sleep(0.05 * (attempt + 1))
                continue
            return {}
        except (OSError, json.JSONDecodeError):
            return {}
        else:
            break
    if not isinstance(data, dict):
        return {}
    return data


def write_settings(settings: dict[str, Any], path: Path | None = None) -> None:
    """Persist Desktop-local settings with a same-directory atomic replace."""

    resolved_path = path if path is not None else settings_path()
    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(settings, indent=2, sort_keys=True) + "\n"

    temporary_path: Path | None = None
    for attempt in range(3):
        try:
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
            return
        except PermissionError:
            if temporary_path is not None and temporary_path.exists():
                try:
                    temporary_path.unlink()
                except OSError:
                    pass
            if attempt < 2:
                time.sleep(0.05 * (attempt + 1))
                continue
        except OSError:
            if temporary_path is not None and temporary_path.exists():
                try:
                    temporary_path.unlink()
                except OSError:
                    pass
            if attempt < 2:
                time.sleep(0.05 * (attempt + 1))
                continue
            return
        else:
            return


def read_wakeword_settings(path: Path | None = None) -> dict[str, Any]:
    """Read wakeword config from Desktop settings, merged with defaults.

    Malformed wakeword key (missing or non-dict) falls back to defaults.
    """

    full = read_settings(path)
    wakeword_data = full.get("wakeword")
    if not isinstance(wakeword_data, dict):
        wakeword_data = {}
    merged = dict(DEFAULT_WAKEWORD_SETTINGS)
    merged.update(wakeword_data)
    return merged


def write_wakeword_settings(wakeword_config: dict[str, Any], path: Path | None = None) -> None:
    """Merge wakeword config into full Desktop settings and persist atomically."""

    full = read_settings(path)
    full["wakeword"] = wakeword_config
    write_settings(full, path)


def resolve_target(
    argv: list[str] | None = None,
    *,
    settings_file: Path | None = None,
) -> DesktopTarget:
    """Resolve target from CLI args, Desktop settings, then defaults."""

    args = parse_args(argv)
    saved_settings = read_settings(settings_file)
    host_source = "--host" if args.host is not None else "settings.host"
    host_value = args.host if args.host is not None else saved_settings.get("host", DEFAULT_HOST)
    port_value = args.port if args.port is not None else saved_settings.get("port", DEFAULT_PORT)
    port = validate_port(port_value, source="settings.port" if args.port is None else "--port")

    try:
        host = validate_host(host_value, source=host_source)
    except ValueError as exc:
        target = DesktopTarget(
            host=str(host_value or ""), port=port, url="", configuration_error=str(exc)
        )
        if args.host is None:
            write_settings({"host": DEFAULT_HOST, "port": port}, settings_file)
        return target

    target = DesktopTarget(host=host, port=port, url=build_target_url(host, port))
    write_settings({"host": target.host, "port": target.port}, settings_file)
    return target


def build_target_url(host: str, port: int) -> str:
    """Build the HTTP WebUI root URL for a resolved Desktop target."""

    return f"http://{validate_host(host)}:{port}/"


def probe_target(
    target: DesktopTarget,
    *,
    get: HttpGet = httpx.get,
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> DesktopProbeResult:
    """Classify the configured server target before Desktop window creation."""

    if target.configuration_error is not None:
        return DesktopProbeResult(status=PROBE_INVALID_TARGET, target=target)

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


def load_webview() -> WebviewModule:
    """Import pywebview lazily so non-desktop test gates do not require it."""

    try:
        return importlib.import_module("webview")
    except ImportError as exc:
        raise RuntimeError(
            "pywebview is required to run vBot Desktop. "
            "Install the desktop optional dependency group, for example: "
            'pip install -e ".[desktop]"'
        ) from exc


def launch_window(
    content: DesktopWindowContent,
    *,
    webview_module: WebviewModule | None = None,
    app_icon_path: Path | None = None,
    js_api: Any = None,
) -> None:
    """Create the pywebview window and run the GUI loop."""

    webview = webview_module if webview_module is not None else load_webview()
    if content.url is not None:
        kwargs: dict[str, Any] = {"url": content.url}
        if js_api is not None:
            kwargs["js_api"] = js_api
        webview.create_window(WINDOW_TITLE, **kwargs)
    elif content.html is not None:
        webview.create_window(WINDOW_TITLE, html=content.html)
    else:
        raise ValueError("Desktop window content requires either url or html")

    resolved_icon_path = app_icon_path if app_icon_path is not None else icon_path()
    if resolved_icon_path.exists():
        # pywebview icon support varies by backend/platform, so custom icons are optional.
        webview.start(icon=str(resolved_icon_path))
        return
    webview.start()


def launch_desktop(
    argv: list[str] | None = None,
    *,
    settings_file: Path | None = None,
    probe: Callable[[DesktopTarget], DesktopProbeResult] = probe_target,
    webview_module: WebviewModule | None = None,
    app_icon_path: Path | None = None,
) -> DesktopTarget:
    """Resolve, probe, and launch the thin pywebview Desktop shell."""

    args = parse_args(argv)
    target = resolve_target(argv, settings_file=settings_file)
    content = choose_window_content(target, probe=probe)

    # Set up wakeword bridge and worker
    bridge = _create_wakeword_bridge(args, settings_file, target.url)

    # Append desktop accessor query param so the WebUI can detect Desktop mode
    if content.url is not None:
        content = DesktopWindowContent(
            status=content.status,
            url=_append_accessor_param(content.url),
        )

    try:
        launch_window(
            content,
            webview_module=webview_module,
            app_icon_path=app_icon_path,
            js_api=bridge,
        )
    finally:
        if bridge is not None:
            bridge._stop_worker()

    return target


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


def validate_host(value: Any, *, source: str = "host") -> str:
    """Validate a localhost or LAN host value before building an HTTP URL."""

    if not isinstance(value, str):
        raise ValueError(f"{source} must be a host name or IP address")
    host = value.strip()
    if not host:
        raise ValueError(f"{source} must not be empty")
    if any(character.isspace() for character in host):
        raise ValueError(f"{source} must not contain whitespace")
    if any(character in INVALID_HOST_CHARACTERS for character in host):
        raise ValueError(f"{source} must be a host name or IP address, not a URL")
    return host


def _is_vbot_health_response(response: HttpResponse) -> bool:
    """Return whether /health matches the vBot server identity contract."""

    try:
        payload = response.json()
    except ValueError:
        return False
    return bool(payload == {"status": "ok"})


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
    if status == PROBE_INVALID_TARGET:
        return (
            "Invalid Desktop target",
            "vBot Desktop could not build a valid server URL from the configured host.",
            "Choose a localhost or LAN host name without a scheme, path, or whitespace.",
        )
    raise ValueError(f"Unsupported Desktop probe status: {status}")


def _create_wakeword_bridge(
    args: argparse.Namespace,
    settings_file: Path | None = None,
    server_url: str = "",
) -> Any:
    """Create the DesktopBridge with engine and worker for the wakeword pipeline.

    Uses MockWakewordEngine when --mock-wakeword is set or when openWakeWord
    cannot be imported. Returns the bridge instance (never None — the bridge
    always exists so the WebUI can query capabilities).
    """

    from desktop.wakeword.bridge import DesktopBridge
    from desktop.wakeword.engine import MockWakewordEngine

    use_mock = bool(args.mock_wakeword)
    engine = None
    worker = None

    if not use_mock:
        try:
            from desktop.wakeword.engine import OpenWakeWordEngine
            from desktop.wakeword.worker import WakewordWorker

            wakeword_config = read_wakeword_settings(settings_file)
            engine = OpenWakeWordEngine(
                wake_phrase=wakeword_config.get("wake_phrase", "hey_jarvis"),
                sensitivity=wakeword_config.get("sensitivity", 0.5),
            )
            worker = WakewordWorker(
                engine=engine,
                bridge=None,  # Set after bridge creation
                settings_path=settings_file,
                server_url=server_url,
            )
        except ImportError:
            engine = MockWakewordEngine()
    else:
        engine = MockWakewordEngine()
        engine.set_score_sequence([0.0])

    bridge = DesktopBridge(settings_path=settings_file, worker=worker)

    # Back-reference: worker needs bridge to publish state
    if worker is not None:
        worker._bridge = bridge

    wakeword_config = read_wakeword_settings(settings_file)
    if wakeword_config.get("enabled", False) and worker is not None:
        bridge.publish_state("listening")
        worker.start()

    return bridge


def _append_accessor_param(url: str) -> str:
    """Append ?accessor=desktop to a URL, preserving existing query params."""

    separator = "&" if "?" in url else "?"
    return f"{url}{separator}{ACCESSOR_QUERY_PARAM}"


def main(argv: list[str] | None = None) -> DesktopTarget:
    """Open the vBot Desktop shell for the resolved server target."""

    return launch_desktop(argv)


def _parse_port(value: str) -> int:
    """Argparse adapter for Desktop port validation."""

    try:
        return validate_port(value, source="--port")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


if __name__ == "__main__":
    main()
