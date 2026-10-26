"""The Desktop's native connection screen: saved servers, add/connect, waiting and pairing.

:func:`build_connection_html` renders the escaped page the connection
controller loads into the window; :func:`connection_error_copy`,
:class:`PairingOffer` and :class:`PairingView` hold the user-facing copy the
controller also hands to bridge callers. Every interpolated value is
HTML-escaped, and the page's script fills in bridge results with
``textContent`` only.

Pairing is the user's step on this screen: a server that needs this Desktop
signed in is offered with the device name it will receive, which the user can
edit; only "Get a code" asks the server for a code. The bridge methods that
connect, pair or forget a sign-in take the screen's key (``screen_key``), which
only this page carries, so a server's page cannot call them.
"""

from __future__ import annotations

import html
import json
from collections.abc import Collection
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from desktop.probe import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    PROBE_ACCESS_DENIED,
    PROBE_INVALID_TARGET,
    PROBE_NOT_VBOT_SERVER,
    PROBE_SERVER_UNREACHABLE,
    PROBE_WEBUI_AVAILABLE,
    PROBE_WEBUI_UNAVAILABLE,
    DesktopProbeResult,
    DesktopTarget,
)

if TYPE_CHECKING:
    from desktop.servers import ServerEntry

_WAITING_BODY = (
    "vBot Desktop connects as soon as the server answers. You can also choose another server below."
)
# Connection outcomes of the sign-in (beside the PROBE_* statuses).
STATUS_PAIRING_REQUIRED = "pairing_required"
STATUS_PAIRING_DENIED = "pairing_denied"
STATUS_PAIRING_EXPIRED = "pairing_expired"
STATUS_RATE_LIMITED = "rate_limited"
STATUS_DEVICES_UNAVAILABLE = "devices_unavailable"
STATUS_SIGN_IN_FAILED = "sign_in_failed"
STATUS_HTTPS_REQUIRED = "https_required"
STATUS_SWITCH_CANCELLED = "server_switch_cancelled"

_ERROR_COPY = {
    STATUS_SWITCH_CANCELLED: (
        "Server switch cancelled",
        "Your current server and local terminals remain active.",
    ),
    PROBE_SERVER_UNREACHABLE: (
        "Server unreachable",
        "vBot Desktop could not reach that server. Check the server is running and the "
        "address is right, then try again.",
    ),
    PROBE_WEBUI_UNAVAILABLE: (
        "WebUI unavailable",
        "The vBot server answered, but it is not serving the WebUI at the root "
        "path. Build or deploy the WebUI for that server, then reconnect.",
    ),
    PROBE_NOT_VBOT_SERVER: (
        "Not a vBot server",
        "Something answered at that address, but it is not a vBot server. "
        "Use the address of a running vBot server.",
    ),
    PROBE_INVALID_TARGET: (
        "Invalid server address",
        "Enter a host name or IP address with a port, or an https:// address such as "
        "https://vbot.example.com, without a path or spaces.",
    ),
    PROBE_ACCESS_DENIED: (
        "Access denied",
        "The vBot server refused this computer. Depending on its access mode it admits "
        "only its own computer, its home network, or connections over https://. Check "
        "the address, or connect with the server's https:// address.",
    ),
    STATUS_PAIRING_DENIED: (
        "Sign-in denied",
        "The request to sign in this Desktop was denied. Connect again to get a new code.",
    ),
    STATUS_PAIRING_EXPIRED: (
        "Code expired",
        "The code was not approved in time. Connect again to get a new code.",
    ),
    STATUS_RATE_LIMITED: (
        "Too many attempts",
        "The server is limiting sign-in attempts from this computer. Wait a few minutes, "
        "then try again.",
    ),
    STATUS_DEVICES_UNAVAILABLE: (
        "Sign-in unavailable",
        "The server cannot read its list of signed-in devices. Its owner has to fix this "
        "on the server's computer; then try again.",
    ),
    STATUS_HTTPS_REQUIRED: (
        "Sign-in needs https://",
        "This server requires sign-in, and vBot Desktop signs in over plain http:// only to "
        "the server of its own installation, at the address that server listens on (such as "
        "127.0.0.1:22332, not localhost). Connect with the server's https:// address instead; "
        "on the server's own computer, open vBot Desktop from its installation.",
    ),
    STATUS_SIGN_IN_FAILED: (
        "Sign-in failed",
        "vBot Desktop could not sign in to this server. Try again; if it keeps failing, "
        "forget this server's sign-in and connect again to pair anew.",
    ),
}


@dataclass(frozen=True)
class PairingOffer:
    """A server that needs this Desktop signed in, before any code is requested.

    ``name`` is the device name the server will show the approver; the user
    may change it before asking for a code.
    """

    server: str
    origin: str
    name: str
    target: DesktopTarget

    @property
    def title(self) -> str:
        return "Sign in this Desktop"

    @property
    def body(self) -> str:
        return (
            f"{self.origin} admits only signed-in devices. To sign in, vBot Desktop sends it "
            "the name below and gets a code that you approve there. Continue only if you "
            f"meant to connect to {self.origin}."
        )

    def to_payload(self) -> dict[str, Any]:
        """The bridge payload the connection screen's script renders."""
        address = self.target.address
        return {
            "host": address.bridge_host if address is not None else self.target.host,
            "port": self.target.port,
            "server": self.server,
            "origin": self.origin,
            "name": self.name,
            "title": self.title,
            "body": self.body,
        }


@dataclass(frozen=True)
class PairingView:
    """A pairing waiting for approval, as the connection screen shows it."""

    code: str
    server: str
    origin: str
    target: DesktopTarget

    @property
    def title(self) -> str:
        return "Approve this Desktop"

    @property
    def body(self) -> str:
        return (
            f"Enter the code {self.code} only in vBot at {self.origin}: on a device already "
            "signed in there, under Settings → Access & devices → Add device, or with "
            f"“vbot devices approve {self.code}” on the server's computer. Never enter it for "
            "another address: whoever gets it approved signs in as this Desktop. vBot Desktop "
            "connects as soon as it is approved."
        )

    def to_payload(self) -> dict[str, str]:
        """The bridge payload the connection screen's script renders."""
        return {
            "code": self.code,
            "server": self.server,
            "origin": self.origin,
            "title": self.title,
            "body": self.body,
        }


def connection_error_copy(status: str) -> tuple[str, str]:
    """Return title/body copy for a failed connection outcome shown inline.

    Covers the probe failures and the sign-in failures. Any other status is a
    programming error and raises rather than rendering an empty banner.
    """
    try:
        return _ERROR_COPY[status]
    except KeyError:
        raise ValueError(f"Unsupported Desktop connection status: {status}") from None


def build_connection_html(
    servers: list[ServerEntry],
    probe_result: DesktopProbeResult | None = None,
    *,
    waiting_for: DesktopTarget | None = None,
    offer: PairingOffer | None = None,
    signed_in: Collection[str] = (),
    screen_key: str = "",
) -> str:
    """Build the escaped interactive connection screen.

    A failed ``probe_result`` (a probe or sign-in status) drives an inline error
    banner and its target prefills the connect form, so the user fixes the
    target in place. ``waiting_for`` shows the launch connect's waiting state
    for that target, ``offer`` a server that needs this Desktop paired (with the
    editable device name and "Get a code"); the script then shows the code
    waiting for approval. They hide once the user connects to a server
    themselves. With
    none of them (first run) no banner shows and the form prefills with the
    default suggestion. The saved-server list offers one-click reconnect to any
    remembered target, and "Forget sign-in" for those whose origin is in
    ``signed_in``. ``screen_key`` goes with every bridge call that connects,
    pairs or forgets a sign-in.
    """

    error_section = _render_error_section(probe_result)
    waiting_section = _render_waiting_section(waiting_for)
    offer_section = _render_offer_section(offer)
    pairing_section = _render_pairing_section()
    servers_section = _render_servers_section(servers, signed_in)
    prefill_host, prefill_port = _resolve_prefill(
        probe_result, waiting_for, offer.target if offer is not None else None
    )
    # A JSON string literal is safe inside the script element: the key is base64url.
    key_literal = json.dumps(screen_key)

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
      .waiting, .pairing {{
        margin: 0 0 1.5rem;
        padding: 1rem 1.15rem;
        border: 1px solid rgba(240, 164, 58, 0.5);
        border-radius: 1rem;
        background: rgba(240, 164, 58, 0.1);
      }}
      .waiting h2, .pairing h2 {{
        margin: 0 0 0.4rem;
        font-size: 1.05rem;
        color: #f0a43a;
      }}
      .waiting p, .pairing p {{ margin: 0; color: #e7d9c4; }}
      .pairing .code {{
        margin: 0.85rem 0;
        font-family: Consolas, "Courier New", monospace;
        font-size: 2rem;
        font-weight: 700;
        letter-spacing: 0.18em;
        color: #f1eadf;
        user-select: all;
      }}
      .pairing button {{ margin-top: 0.85rem; }}
      .pairing label {{ margin-top: 0.85rem; }}
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
      ul.servers li {{
        display: flex;
        gap: 0.5rem;
      }}
      ul.servers button {{
        text-align: left;
        padding: 0.85rem 1rem;
        border: 1px solid rgba(240, 164, 58, 0.22);
        border-radius: 0.85rem;
        background: #2d271f;
        color: #f1eadf;
        font: inherit;
        cursor: pointer;
      }}
      ul.servers button[data-host] {{ flex: 1; }}
      ul.servers button:hover {{ border-color: rgba(240, 164, 58, 0.55); }}
      button.secondary {{
        padding: 0.75rem 1rem;
        border: 1px solid rgba(240, 164, 58, 0.35);
        border-radius: 0.85rem;
        background: transparent;
        color: #e7d9c4;
        font: inherit;
        cursor: pointer;
      }}
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
      .hint {{ grid-column: 1 / -1; margin: 0; font-size: 0.85rem; }}
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
      <p>Pick a saved server, or enter the address of a running vBot server.</p>
{waiting_section}{offer_section}{pairing_section}
{error_section}
{servers_section}
      <h2 class="section">Add a server</h2>
      <form id="connect-form" onsubmit="return false;">
        <label>Host or https:// address
          <input id="host" name="host" type="text" autocomplete="off"
                 value="{html.escape(prefill_host)}" placeholder="{DEFAULT_HOST}">
        </label>
        <label class="field-port">Port
          <input id="port" name="port" type="number" min="1" max="65535"
                 value="{html.escape(prefill_port)}" placeholder="{DEFAULT_PORT}">
        </label>
        <p class="hint">A server on this computer or your network: its host and port.
          A server on the internet: its https:// address, such as https://vbot.example.com
          (the port field is then ignored).</p>
        <div class="actions">
          <button class="primary" type="button" onclick="connectFromForm()">Connect</button>
        </div>
      </form>
    </main>
    <script>
      var SCREEN_KEY = {key_literal};
      function bridgeReady(method) {{
        return Boolean(window.pywebview && window.pywebview.api && window.pywebview.api[method]);
      }}
      function showConnectionError(title, body) {{
        var container = document.getElementById("connection-error");
        container.hidden = false;
        container.setAttribute("role", "alert");
        document.getElementById("connection-error-title").textContent = title;
        document.getElementById("connection-error-body").textContent = body;
      }}
      function hideNotices() {{
        var waiting = document.getElementById("connection-waiting");
        if (waiting) {{
          // The user's own connect ends the launch wait and a pending pairing.
          waiting.hidden = true;
        }}
        document.getElementById("connection-offer").hidden = true;
        document.getElementById("connection-pairing").hidden = true;
        document.getElementById("connection-error").hidden = true;
      }}
      function showOffer(offer) {{
        var panel = document.getElementById("connection-offer");
        document.getElementById("offer-title").textContent = offer.title;
        document.getElementById("offer-body").textContent = offer.body;
        document.getElementById("offer-name").value = offer.name;
        panel.dataset.host = offer.host;
        panel.dataset.port = String(offer.port);
        panel.hidden = false;
      }}
      function showPairing(pairing) {{
        document.getElementById("pairing-title").textContent = pairing.title;
        document.getElementById("pairing-body").textContent = pairing.body;
        document.getElementById("pairing-code").textContent = pairing.code;
        document.getElementById("connection-pairing").hidden = false;
      }}
      function prefillConnectionTarget(host, port) {{
        document.getElementById("host").value = host;
        document.getElementById("port").value = Number.isInteger(port) ? port : "";
      }}
      async function requestConnection(host, port) {{
        if (!bridgeReady("connect")) {{
          showConnectionError(
            "Desktop bridge unavailable",
            "The native Desktop bridge is not ready. Close the window and start vBot Desktop again."
          );
          return;
        }}
        hideNotices();
        try {{
          var result = await window.pywebview.api.connect(host, port, SCREEN_KEY);
          if (result.url) {{
            window.location.assign(result.url);
            return;
          }}
          prefillConnectionTarget(host, port);
          if (result.offer) {{
            showOffer(result.offer);
            return;
          }}
          if (result.pairing) {{
            showPairing(result.pairing);
            return;
          }}
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
      async function requestCode() {{
        var panel = document.getElementById("connection-offer");
        var host = panel.dataset.host;
        var port = parseInt(panel.dataset.port, 10);
        var name = document.getElementById("offer-name").value;
        if (!bridgeReady("startPairing")) {{
          return;
        }}
        hideNotices();
        try {{
          var result = await window.pywebview.api.startPairing(host, port, name, SCREEN_KEY);
          if (result.pairing) {{
            showPairing(result.pairing);
            return;
          }}
          showConnectionError(result.error_title, result.error_body);
        }} catch (error) {{
          showConnectionError(
            "Pairing not started",
            error && error.message ? error.message : "The Desktop could not ask for a code."
          );
        }}
      }}
      function cancelOffer() {{
        document.getElementById("connection-offer").hidden = true;
      }}
      async function cancelPairing() {{
        document.getElementById("connection-pairing").hidden = true;
        if (bridgeReady("cancelPairing")) {{
          try {{
            await window.pywebview.api.cancelPairing();
          }} catch (error) {{
            // The pairing ends with its code anyway.
          }}
        }}
      }}
      async function forgetSignIn(button) {{
        if (!bridgeReady("forgetSignIn")) {{
          return;
        }}
        try {{
          await window.pywebview.api.forgetSignIn(
            button.dataset.forgetHost, parseInt(button.dataset.forgetPort, 10), SCREEN_KEY
          );
          button.remove();
        }} catch (error) {{
          showConnectionError(
            "Sign-in not forgotten",
            error && error.message ? error.message : "The Desktop could not forget the sign-in."
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
      document.querySelectorAll("ul.servers button[data-forget-host]").forEach(function (button) {{
        button.addEventListener("click", function () {{
          forgetSignIn(button);
        }});
      }});
      document.getElementById("pairing-cancel").addEventListener("click", cancelPairing);
      document.getElementById("offer-start").addEventListener("click", requestCode);
      document.getElementById("offer-cancel").addEventListener("click", cancelOffer);
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
    title, body = connection_error_copy(probe_result.status)
    return (
        '      <div id="connection-error" class="error" role="alert">\n'
        f'        <h2 id="connection-error-title">{html.escape(title)}</h2>\n'
        f'        <p id="connection-error-body">{html.escape(body)}</p>\n'
        "      </div>"
    )


def _render_waiting_section(waiting_for: DesktopTarget | None) -> str:
    """Return the escaped waiting state of the launch connect, or nothing."""

    if waiting_for is None:
        return ""
    title = f"Waiting for {waiting_for.display} …"
    return (
        '      <div id="connection-waiting" class="waiting" role="status">\n'
        f"        <h2>{html.escape(title)}</h2>\n"
        f"        <p>{html.escape(_WAITING_BODY)}</p>\n"
        "      </div>\n"
    )


def _render_offer_section(offer: PairingOffer | None) -> str:
    """Return the pairing offer: filled for a server that needs pairing, else hidden."""

    payload = offer.to_payload() if offer is not None else {}
    title, body, name = (str(payload.get(key, "")) for key in ("title", "body", "name"))
    # The offered server rides data-* attributes for the script, like a saved server.
    attributes = (
        f' data-host="{html.escape(str(payload["host"]))}" '
        f'data-port="{html.escape(str(payload["port"]))}"'
        if offer is not None
        else " hidden"
    )
    return (
        f'      <div id="connection-offer" class="pairing" role="status"{attributes}>\n'
        f'        <h2 id="offer-title">{html.escape(title)}</h2>\n'
        f'        <p id="offer-body">{html.escape(body)}</p>\n'
        "        <label>Name for this Desktop\n"
        f'          <input id="offer-name" type="text" maxlength="80" autocomplete="off" '
        f'value="{html.escape(name)}">\n'
        "        </label>\n"
        '        <button id="offer-start" class="primary" type="button">Get a code</button>\n'
        '        <button id="offer-cancel" class="secondary" type="button">Cancel</button>\n'
        "      </div>\n"
    )


def _render_pairing_section() -> str:
    """Return the hidden pairing panel the script fills once the user asked for a code."""

    return (
        '      <div id="connection-pairing" class="pairing" role="status" hidden>\n'
        '        <h2 id="pairing-title"></h2>\n'
        '        <p id="pairing-body"></p>\n'
        '        <p id="pairing-code" class="code"></p>\n'
        "        <p>Waiting for approval …</p>\n"
        '        <button id="pairing-cancel" class="secondary" type="button">Cancel</button>\n'
        "      </div>"
    )


def _render_servers_section(servers: list[ServerEntry], signed_in: Collection[str]) -> str:
    """Return the escaped saved-server list, or an empty-state line when none."""

    if not servers:
        return '      <h2 class="section">Saved servers</h2>\n      <p>No servers saved yet.</p>'

    items: list[str] = []
    for entry in servers:
        address = entry.address
        escaped_host = html.escape(address.bridge_host)
        escaped_port = html.escape(str(entry.port))
        escaped_name = html.escape(entry.display_name())
        forget = (
            f'<button type="button" class="secondary" data-forget-host="{escaped_host}" '
            f'data-forget-port="{escaped_port}">Forget sign-in</button>'
            if address.origin in signed_in
            else ""
        )
        items.append(
            "        <li>"
            f'<button type="button" data-host="{escaped_host}" data-port="{escaped_port}">'
            f"{escaped_name}</button>{forget}</li>"
        )
    joined = "\n".join(items)
    return (
        '      <h2 class="section">Saved servers</h2>\n'
        '      <ul class="servers">\n'
        f"{joined}\n"
        "      </ul>"
    )


def _resolve_prefill(
    probe_result: DesktopProbeResult | None,
    waiting_for: DesktopTarget | None = None,
    offered: DesktopTarget | None = None,
) -> tuple[str, str]:
    """Return the (host, port) strings to prefill the connect form.

    A failed connection prefills the offending target so the user can correct
    it in place, a waiting launch connect or a pairing offer its target; otherwise
    the default suggestion is offered (never an auto-connect target — just a hint).
    """

    if probe_result is not None and probe_result.status != PROBE_WEBUI_AVAILABLE:
        return _form_values(probe_result.target)
    if waiting_for is not None:
        return _form_values(waiting_for)
    if offered is not None:
        return _form_values(offered)
    return (DEFAULT_HOST, str(DEFAULT_PORT))


def _form_values(target: DesktopTarget) -> tuple[str, str]:
    address = target.address
    host = address.bridge_host if address is not None else target.host
    return (host, str(target.port))
