"""In-window server selection for the vBot pywebview Desktop accessor.

The Desktop owns a small native connection screen instead of a dead-end error
page: it lists the remembered servers, lets the user add/select/remove one, and
auto-connects to the last-used target on launch. Every probe failure
(unreachable server, non-vBot server, missing WebUI, invalid target) lands the
user back on the *same* interactive screen with the failed host/port prefilled,
so there is never a page the user cannot recover from.

Three pieces live here:

- Remembered-servers operations over :mod:`desktop.settings`
  (:func:`list_servers` / :func:`add_server` / :func:`select_server` /
  :func:`remove_server` / :func:`resolve_last_used`).
- The escaped connection-screen HTML (:func:`build_connection_html`) — saved
  server list, add/connect form, and an inline probe error.
- :class:`ConnectionController`, which holds the live pywebview ``Window`` and
  drives connect/switch/reconnect: a successful probe navigates the window to
  the server's WebUI via ``Window.load_url``; any failure renders the connection
  screen inline via ``Window.load_html``.

Target classification reuses ``probe_target`` / ``validate_host`` /
``validate_port`` from :mod:`desktop.main` rather than re-deriving them. pywebview
is imported lazily (like the rest of ``desktop/``) so the backend test gate never
requires the optional GUI package; tests inject a fake window.
"""

from __future__ import annotations

import html
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from desktop.main import (
    ACCESSOR_QUERY_PARAM,
    PROBE_INVALID_TARGET,
    PROBE_NOT_VBOT_SERVER,
    PROBE_SERVER_UNREACHABLE,
    PROBE_WEBUI_AVAILABLE,
    PROBE_WEBUI_UNAVAILABLE,
    DesktopProbeResult,
    DesktopTarget,
    build_target_url,
    probe_target,
    validate_host,
    validate_port,
)
from desktop.settings import (
    clear_last_used,
    read_last_used,
    read_servers,
    write_last_used,
    write_servers,
)

logger = logging.getLogger("vbot.desktop.connection")

_DEFAULT_HOST_PLACEHOLDER = "127.0.0.1"
_DEFAULT_PORT_PLACEHOLDER = 8420


class WindowProtocol(Protocol):
    """Subset of a pywebview ``Window`` the controller drives.

    pywebview keeps the same ``Window`` across navigation, so the controller can
    swap between the remote WebUI and the local connection screen on one window.
    """

    def load_url(self, url: str) -> Any:
        """Navigate the live window to a URL."""

    def load_html(self, content: str) -> Any:
        """Replace the live window content with inline HTML."""


@dataclass(frozen=True)
class ServerEntry:
    """A remembered Desktop server target.

    Identity is the ``(host, port)`` pair — the optional ``label`` is display
    only. This mirrors the on-disk ``servers`` entry shape so the settings store
    and the controller speak the same vocabulary.
    """

    host: str
    port: int
    label: str | None = None

    @property
    def key(self) -> tuple[str, int]:
        """Return the identity used to match/deduplicate remembered servers."""

        return (self.host, self.port)

    def to_storage(self) -> dict[str, Any]:
        """Render the entry as a settings ``servers`` dict (label omitted if unset)."""

        entry: dict[str, Any] = {"host": self.host, "port": self.port}
        if self.label:
            entry["label"] = self.label
        return entry

    @classmethod
    def from_storage(cls, entry: dict[str, Any]) -> ServerEntry:
        """Build an entry from a normalized settings ``servers`` dict."""

        label = entry.get("label")
        return cls(
            host=entry["host"],
            port=entry["port"],
            label=label if isinstance(label, str) else None,
        )

    def display_name(self) -> str:
        """Return a human label for the UI — explicit label or host:port."""

        if self.label:
            return f"{self.label} ({self.host}:{self.port})"
        return f"{self.host}:{self.port}"


@dataclass(frozen=True)
class PreparedConnection:
    """A probed connection whose visible navigation has not happened yet.

    pywebview API calls return through a Promise callback stored in the current
    document. Replacing that document before the Python method returns destroys
    the callback and makes pywebview raise while delivering the result. This
    value lets bridge callers receive the outcome first and navigate from
    JavaScript afterwards, while native launch callers can still apply the same
    prepared outcome through :meth:`ConnectionController.connect`.
    """

    result: DesktopProbeResult
    navigation_url: str | None = None
    error_title: str | None = None
    error_body: str | None = None

    def to_bridge_payload(self) -> dict[str, str]:
        """Return the plain payload consumed by Desktop JavaScript callers."""

        payload = {"status": self.result.status}
        if self.navigation_url is not None:
            payload["url"] = self.navigation_url
        if self.error_title is not None:
            payload["error_title"] = self.error_title
        if self.error_body is not None:
            payload["error_body"] = self.error_body
        return payload


# -- Remembered-servers operations -------------------------------------------


def list_servers(settings_file: Path | None = None) -> list[ServerEntry]:
    """Return the remembered servers in stored order."""

    return [ServerEntry.from_storage(entry) for entry in read_servers(settings_file)]


def add_server(
    host: str,
    port: int,
    label: str | None = None,
    *,
    settings_file: Path | None = None,
) -> ServerEntry:
    """Add (or update) a remembered server, keyed by ``(host, port)``.

    An existing entry with the same host/port is replaced in place (so a re-add
    can refresh its label) rather than duplicated; the list order is otherwise
    preserved. Host and port are validated through the shared Desktop rules
    before anything is persisted, so a malformed entry never reaches disk.
    """

    validated_host = validate_host(host)
    validated_port = validate_port(port)
    entry = ServerEntry(
        host=validated_host,
        port=validated_port,
        label=label if label else None,
    )

    servers = list_servers(settings_file)
    replaced = False
    updated: list[ServerEntry] = []
    for existing in servers:
        if existing.key == entry.key:
            updated.append(entry)
            replaced = True
        else:
            updated.append(existing)
    if not replaced:
        updated.append(entry)

    write_servers([item.to_storage() for item in updated], settings_file)
    return entry


def remove_server(
    host: str,
    port: int,
    *,
    settings_file: Path | None = None,
) -> bool:
    """Remove a remembered server by ``(host, port)``.

    Returns whether an entry was removed. Removing the last-used target also
    clears the last-used reference so launch does not point at a server the user
    just forgot.
    """

    target_key = (host, port)
    servers = list_servers(settings_file)
    remaining = [entry for entry in servers if entry.key != target_key]
    if len(remaining) == len(servers):
        return False

    write_servers([entry.to_storage() for entry in remaining], settings_file)

    last_used = read_last_used(settings_file)
    if last_used is not None and (last_used["host"], last_used["port"]) == target_key:
        _clear_last_used(settings_file)
    return True


def select_server(
    host: str,
    port: int,
    *,
    settings_file: Path | None = None,
) -> None:
    """Mark a server as last-used so the next launch auto-connects to it."""

    write_last_used(host, port, settings_file)


def resolve_last_used(settings_file: Path | None = None) -> ServerEntry | None:
    """Resolve the target to auto-connect on launch.

    The last-used reference wins; when it names a remembered server the stored
    label is carried through. With no last-used reference, the first remembered
    server is used. With nothing remembered at all, ``None`` signals first-run —
    the caller opens the connection screen instead of grabbing a default.
    """

    servers = list_servers(settings_file)
    last_used = read_last_used(settings_file)
    if last_used is not None:
        wanted = (last_used["host"], last_used["port"])
        for entry in servers:
            if entry.key == wanted:
                return entry
        return ServerEntry(host=last_used["host"], port=last_used["port"])
    if servers:
        return servers[0]
    return None


# -- Controller --------------------------------------------------------------


class ConnectionController:
    """Drive connect/switch/reconnect against one live pywebview window.

    The controller owns no window at construction time — the entrypoint creates
    the ``Window`` and hands it over via :meth:`attach_window` (the menu builder
    and bridge are wired before the GUI loop exists, so the window arrives
    later). Every public action funnels through :meth:`connect`, which probes the
    target with the shared :func:`probe_target` and either navigates the window
    to the WebUI on success or renders the connection screen inline on failure.
    """

    def __init__(
        self,
        *,
        settings_file: Path | None = None,
        window: WindowProtocol | None = None,
        probe: Callable[[DesktopTarget], DesktopProbeResult] = probe_target,
    ) -> None:
        self._settings_file = settings_file
        self._window = window
        self._probe = probe
        # Notified with the base WebUI URL on every successful connect, so the
        # voice worker follows the window's active server (wired to the bridge in
        # the launcher — the controller stays decoupled from the voice stack).
        self._active_server_listener: Callable[[str], None] | None = None

    def attach_window(self, window: WindowProtocol) -> None:
        """Bind the live pywebview window the controller navigates."""

        self._window = window

    def set_active_server_listener(self, listener: Callable[[str], None] | None) -> None:
        """Register a callback invoked with the base URL on each successful connect."""

        self._active_server_listener = listener

    # -- Remembered-servers surface (delegates to the module operations) -----

    def list_servers(self) -> list[ServerEntry]:
        """Return the remembered servers in stored order."""

        return list_servers(self._settings_file)

    def add_server(self, host: str, port: int, label: str | None = None) -> ServerEntry:
        """Remember a server, keyed by ``(host, port)``."""

        return add_server(host, port, label, settings_file=self._settings_file)

    def remove_server(self, host: str, port: int) -> bool:
        """Forget a remembered server by ``(host, port)``."""

        return remove_server(host, port, settings_file=self._settings_file)

    def resolve_last_used(self) -> ServerEntry | None:
        """Resolve the launch target (last-used, else first remembered, else None)."""

        return resolve_last_used(self._settings_file)

    # -- Navigation ----------------------------------------------------------

    def connect(self, host: str, port: int, label: str | None = None) -> DesktopProbeResult:
        """Probe a target and navigate the window to it, or to the error screen.

        On a successful probe the host/port are remembered (carrying ``label``)
        and marked last-used, and the window loads the WebUI with the
        ``accessor=desktop`` marker. On any failure the window shows the
        connection screen with the failed host/port prefilled and an inline
        error, so the user corrects the target in place. Returns the probe result
        so callers/tests can assert the outcome.
        """

        prepared = self.prepare_connect(host, port, label)
        if prepared.navigation_url is not None:
            self._navigate_url(prepared.navigation_url)
        else:
            self._show_connection_screen(prepared.result)
        return prepared.result

    def prepare_connect(
        self,
        host: str,
        port: int,
        label: str | None = None,
    ) -> PreparedConnection:
        """Probe and persist a target without replacing the current document.

        Successful attempts are remembered, marked last-used, and announced to
        the active-server listener exactly like :meth:`connect`, but navigation
        is returned as data. Failed attempts carry the existing inline error
        copy. This is the bridge-safe seam: pywebview can deliver the payload to
        JavaScript before that document applies the outcome.
        """

        target = self._build_target(host, port)
        result = self._probe(target)

        if result.status == PROBE_WEBUI_AVAILABLE:
            self.add_server(target.host, target.port, label)
            select_server(target.host, target.port, settings_file=self._settings_file)
            logger.info("Desktop connecting to %s:%s", target.host, target.port)
            self._notify_active_server(target.url)
            return PreparedConnection(
                result=result,
                navigation_url=_with_accessor_param(target.url),
            )

        logger.warning(
            "Desktop connection to %s:%s failed (%s)",
            host,
            port,
            result.status,
        )
        error_title, error_body = _connection_error_copy(result.status)
        return PreparedConnection(
            result=result,
            error_title=error_title,
            error_body=error_body,
        )

    def switch_to(self, host: str, port: int, label: str | None = None) -> DesktopProbeResult:
        """Connect to a chosen remembered or typed server."""

        return self.connect(host, port, label)

    def reconnect(self) -> DesktopProbeResult | None:
        """Re-probe and reload the last-used target.

        With nothing remembered, there is no target to retry — the connection
        screen is shown with no inline error and ``None`` is returned.
        """

        entry = self.resolve_last_used()
        if entry is None:
            self.show_connection_screen()
            return None
        return self.connect(entry.host, entry.port, entry.label)

    def auto_connect(self) -> DesktopProbeResult | None:
        """Launch entry point: auto-connect to the last-used target.

        First run (nothing remembered) opens the connection screen and returns
        ``None``; otherwise this is :meth:`reconnect` against the resolved
        launch target.
        """

        return self.reconnect()

    def show_connection_screen(self) -> None:
        """Render the connection screen with no inline error."""

        self._show_connection_screen(probe_result=None)

    # -- Internal ------------------------------------------------------------

    def _build_target(self, host: str, port: int) -> DesktopTarget:
        """Validate host/port into a :class:`DesktopTarget`, carrying any error.

        Invalid values are folded into ``configuration_error`` so the probe
        classifies them as an invalid target and the UI can keep the attempted
        values visible without a bridge exception.
        """

        try:
            validated_port = validate_port(port)
            validated_host = validate_host(host)
        except ValueError as exc:
            return DesktopTarget(
                host=str(host or ""),
                port=_display_port(port),
                url="",
                configuration_error=str(exc),
            )
        return DesktopTarget(
            host=validated_host,
            port=validated_port,
            url=build_target_url(validated_host, validated_port),
        )

    def _navigate_url(self, url: str) -> None:
        window = self._require_window()
        window.load_url(url)

    def _notify_active_server(self, url: str) -> None:
        """Tell the active-server listener (the voice worker) about a new server.

        The listener rebuilds a background worker, so a failure there must never
        break the window navigation that already succeeded — it is logged and
        swallowed. Passed the plain base URL, not the ``accessor=desktop`` window
        URL, since the worker composes its own ``/api/...`` paths from it.
        """

        if self._active_server_listener is None:
            return
        try:
            self._active_server_listener(url)
        except Exception:
            logger.warning("Active-server listener failed for %s", url, exc_info=True)

    def _show_connection_screen(self, probe_result: DesktopProbeResult | None) -> None:
        window = self._require_window()
        window.load_html(
            build_connection_html(
                servers=self.list_servers(),
                probe_result=probe_result,
            )
        )

    def _require_window(self) -> WindowProtocol:
        if self._window is None:
            raise RuntimeError("ConnectionController has no window attached")
        return self._window


# -- Connection screen HTML --------------------------------------------------


def build_connection_html(
    servers: list[ServerEntry],
    probe_result: DesktopProbeResult | None = None,
) -> str:
    """Build the escaped interactive connection screen.

    Subsumes the four probe outcomes the old static fallback rendered: when
    ``probe_result`` is a failure its status drives an inline error banner and
    its host/port prefill the connect form, so the user fixes the target in
    place. With ``probe_result`` ``None`` (first run / Switch…) no banner shows
    and the form prefills with the default suggestion. The saved-server list
    offers one-click reconnect to any remembered target. Every interpolated
    value — labels, hosts, ports, error copy — is HTML-escaped.
    """

    error_section = _render_error_section(probe_result)
    servers_section = _render_servers_section(servers)
    prefill_host, prefill_port = _resolve_prefill(probe_result)

    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Connect · vBot</title>
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
        font-size: clamp(2rem, 6vw, 3.25rem);
        line-height: 1;
      }}
      p {{
        margin: 0 0 1rem;
        color: #b8aa95;
        font-size: 1rem;
        line-height: 1.6;
      }}
      .error {{
        margin: 0 0 1.5rem;
        padding: 1rem 1.15rem;
        border: 1px solid rgba(232, 96, 72, 0.5);
        border-radius: 1rem;
        background: rgba(232, 96, 72, 0.12);
      }}
      .error h2 {{
        margin: 0 0 0.4rem;
        font-size: 1.05rem;
        color: #f3b1a4;
      }}
      .error p {{ margin: 0; color: #e7c8c0; }}
      h2.section {{
        margin: 1.75rem 0 0.75rem;
        font-size: 0.8rem;
        font-weight: 700;
        letter-spacing: 0.12em;
        text-transform: uppercase;
        color: #847762;
      }}
      ul.servers {{
        margin: 0;
        padding: 0;
        list-style: none;
        display: grid;
        gap: 0.5rem;
      }}
      ul.servers button {{
        width: 100%;
        text-align: left;
        padding: 0.85rem 1rem;
        border: 1px solid rgba(240, 164, 58, 0.22);
        border-radius: 0.85rem;
        background: #2d271f;
        color: #f1eadf;
        font: inherit;
        cursor: pointer;
      }}
      ul.servers button:hover {{ border-color: rgba(240, 164, 58, 0.55); }}
      form {{
        margin: 0.75rem 0 0;
        display: grid;
        grid-template-columns: 1fr max-content;
        gap: 0.75rem;
        align-items: end;
      }}
      label {{
        display: grid;
        gap: 0.3rem;
        font-size: 0.8rem;
        color: #847762;
      }}
      input {{
        padding: 0.7rem 0.85rem;
        border: 1px solid rgba(240, 164, 58, 0.28);
        border-radius: 0.75rem;
        background: #15130f;
        color: #f1eadf;
        font: inherit;
      }}
      .field-port {{ max-width: 8rem; }}
      .actions {{ grid-column: 1 / -1; }}
      button.primary {{
        padding: 0.75rem 1.4rem;
        border: none;
        border-radius: 0.85rem;
        background: #f0a43a;
        color: #15130f;
        font: inherit;
        font-weight: 700;
        cursor: pointer;
      }}
    </style>
  </head>
  <body>
    <main>
      <p class="eyebrow">vBot Desktop</p>
      <h1>Connect to a server</h1>
      <p>Pick a saved server or enter the host and port of a running vBot server.</p>
{error_section}
{servers_section}
      <h2 class="section">Add a server</h2>
      <form id="connect-form" onsubmit="return false;">
        <label>Host
          <input id="host" name="host" type="text" autocomplete="off"
                 value="{html.escape(prefill_host)}" placeholder="{_DEFAULT_HOST_PLACEHOLDER}">
        </label>
        <label class="field-port">Port
          <input id="port" name="port" type="number" min="1" max="65535"
                 value="{html.escape(prefill_port)}" placeholder="{_DEFAULT_PORT_PLACEHOLDER}">
        </label>
        <div class="actions">
          <button class="primary" type="button" onclick="connectFromForm()">Connect</button>
        </div>
      </form>
    </main>
    <script>
      function showConnectionError(title, body) {{
        var container = document.getElementById("connection-error");
        container.hidden = false;
        container.setAttribute("role", "alert");
        document.getElementById("connection-error-title").textContent = title;
        document.getElementById("connection-error-body").textContent = body;
      }}
      function prefillConnectionTarget(host, port) {{
        document.getElementById("host").value = host;
        document.getElementById("port").value = Number.isInteger(port) ? port : "";
      }}
      async function requestConnection(host, port) {{
        if (!(window.pywebview && window.pywebview.api && window.pywebview.api.connect)) {{
          showConnectionError(
            "Desktop bridge unavailable",
            "The native Desktop bridge is not ready. Close the window and start vBot Desktop again."
          );
          return;
        }}
        try {{
          var result = await window.pywebview.api.connect(host, port);
          if (result.url) {{
            window.location.assign(result.url);
            return;
          }}
          prefillConnectionTarget(host, port);
          showConnectionError(result.error_title, result.error_body);
        }} catch (error) {{
          prefillConnectionTarget(host, port);
          showConnectionError(
            "Connection failed",
            error && error.message
              ? error.message
              : "The Desktop bridge could not connect to that server."
          );
        }}
      }}
      function connectFromForm() {{
        var host = document.getElementById("host").value;
        var port = parseInt(document.getElementById("port").value, 10);
        requestConnection(host, port);
      }}
      function connectSaved(host, port) {{
        requestConnection(host, port);
      }}
      document.querySelectorAll("ul.servers button[data-host]").forEach(function (button) {{
        button.addEventListener("click", function () {{
          connectSaved(button.dataset.host, parseInt(button.dataset.port, 10));
        }});
      }});
    </script>
  </body>
</html>
"""


def _render_error_section(probe_result: DesktopProbeResult | None) -> str:
    """Return the escaped inline error banner and bridge-update target."""

    if probe_result is None or probe_result.status == PROBE_WEBUI_AVAILABLE:
        return (
            '      <div id="connection-error" class="error" hidden>\n'
            '        <h2 id="connection-error-title"></h2>\n'
            '        <p id="connection-error-body"></p>\n'
            "      </div>"
        )
    title, body = _connection_error_copy(probe_result.status)
    return (
        '      <div id="connection-error" class="error" role="alert">\n'
        f'        <h2 id="connection-error-title">{html.escape(title)}</h2>\n'
        f'        <p id="connection-error-body">{html.escape(body)}</p>\n'
        "      </div>"
    )


def _render_servers_section(servers: list[ServerEntry]) -> str:
    """Return the escaped saved-server list, or an empty-state line when none."""

    if not servers:
        return '      <h2 class="section">Saved servers</h2>\n      <p>No servers saved yet.</p>'

    items: list[str] = []
    for entry in servers:
        escaped_host = html.escape(entry.host)
        escaped_port = html.escape(str(entry.port))
        escaped_name = html.escape(entry.display_name())
        items.append(
            "        <li>"
            f'<button type="button" data-host="{escaped_host}" data-port="{escaped_port}">'
            f"{escaped_name}</button></li>"
        )
    joined = "\n".join(items)
    return (
        '      <h2 class="section">Saved servers</h2>\n'
        '      <ul class="servers">\n'
        f"{joined}\n"
        "      </ul>"
    )


def _resolve_prefill(probe_result: DesktopProbeResult | None) -> tuple[str, str]:
    """Return the (host, port) strings to prefill the connect form.

    A failed probe prefills the offending host/port so the user can correct it
    in place; otherwise the default suggestion is offered (never an
    auto-connect target — just a hint).
    """

    if probe_result is not None and probe_result.status != PROBE_WEBUI_AVAILABLE:
        target = probe_result.target
        return (target.host, str(target.port))
    return (_DEFAULT_HOST_PLACEHOLDER, str(_DEFAULT_PORT_PLACEHOLDER))


def _connection_error_copy(status: str) -> tuple[str, str]:
    """Return title/body copy for a failed probe status shown inline.

    Covers the four failure outcomes the old static fallback page rendered.
    Precondition: ``status`` is one of those failure statuses — the sole caller
    (:func:`_render_error_section`) filters out ``None`` and the success status
    first. Any other status is a programming error and raises rather than
    rendering an empty banner.
    """

    if status == PROBE_SERVER_UNREACHABLE:
        return (
            "Server unreachable",
            "vBot Desktop could not reach that host and port. Check the server is "
            "running and the address is right, then try again.",
        )
    if status == PROBE_WEBUI_UNAVAILABLE:
        return (
            "WebUI unavailable",
            "The vBot server answered, but it is not serving the WebUI at the root "
            "path. Build or deploy the WebUI for that server, then reconnect.",
        )
    if status == PROBE_NOT_VBOT_SERVER:
        return (
            "Not a vBot server",
            "Something answered at that address, but it is not a vBot server. "
            "Use the host and port of a running vBot server.",
        )
    if status == PROBE_INVALID_TARGET:
        return (
            "Invalid host or port",
            "That host could not be turned into a server address. Enter a plain host "
            "name or IP address, without a scheme, path, or spaces.",
        )
    raise ValueError(f"Unsupported Desktop probe status: {status}")


def _with_accessor_param(url: str) -> str:
    """Append ``?accessor=desktop`` to a URL, preserving existing query params."""

    separator = "&" if "?" in url else "?"
    return f"{url}{separator}{ACCESSOR_QUERY_PARAM}"


def _display_port(value: Any) -> int:
    """Return a safe form-prefill port for an invalid target."""

    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _clear_last_used(settings_file: Path | None) -> None:
    """Drop the last-used reference so it cannot point at a forgotten server.

    Reuses the settings store rather than reaching into the JSON file directly:
    re-read, drop the key, and write back, preserving the other settings.
    """

    clear_last_used(settings_file)
