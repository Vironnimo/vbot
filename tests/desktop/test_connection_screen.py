"""The Desktop's native connection screen (desktop.connection_screen)."""

from __future__ import annotations

import pytest

from desktop.connection import ServerEntry
from desktop.connection_screen import (
    STATUS_DEVICES_UNAVAILABLE,
    STATUS_HTTPS_REQUIRED,
    STATUS_PAIRING_DENIED,
    STATUS_PAIRING_EXPIRED,
    STATUS_RATE_LIMITED,
    STATUS_SIGN_IN_FAILED,
    PairingOffer,
    PairingView,
    build_connection_html,
    connection_error_copy,
)
from desktop.main import (
    PROBE_ACCESS_DENIED,
    PROBE_INVALID_TARGET,
    PROBE_NOT_VBOT_SERVER,
    PROBE_SERVER_UNREACHABLE,
    PROBE_WEBUI_UNAVAILABLE,
    DesktopProbeResult,
    DesktopTarget,
)
from desktop.server_address import ServerAddress, validate_host

CLOUD = ServerEntry("vbot.example.com", 443, "Cloud", "https")
CLOUD_TARGET = DesktopTarget.for_address(ServerAddress("https", "vbot.example.com", 443))


def test_connection_html_lists_saved_servers_with_connect_hooks() -> None:
    servers = [ServerEntry("pi.lan", 9000, "Pi"), ServerEntry("10.0.0.5", 8500), CLOUD]

    page = build_connection_html(servers)

    assert "Pi (pi.lan:9000)" in page
    assert "Cloud (https://vbot.example.com)" in page
    # Saved hosts/ports ride data-* attributes; a static handler reads them as
    # strings via dataset, never as interpolated JS (no inline onclick carries a
    # host in a JS-string context). An HTTPS server's host is its address.
    assert 'data-host="pi.lan" data-port="9000"' in page
    assert 'data-host="10.0.0.5" data-port="8500"' in page
    assert 'data-host="https://vbot.example.com" data-port="443"' in page
    assert "connectSaved('pi.lan'" not in page
    assert "connectSaved(button.dataset.host" in page


def test_connection_html_awaits_bridge_result_before_navigation() -> None:
    page = build_connection_html([])

    bridge_call = "await window.pywebview.api.connect(host, port, SCREEN_KEY)"
    navigation = "window.location.assign(result.url)"
    assert bridge_call in page
    assert navigation in page
    assert page.index(bridge_call) < page.index(navigation)
    # The user's own connect hides a launch wait's waiting state and a pairing first.
    assert page.index("waiting.hidden = true") < page.index(bridge_call)
    assert page.index('getElementById("connection-pairing").hidden = true') < page.index(
        bridge_call
    )
    assert page.index('getElementById("connection-offer").hidden = true') < page.index(bridge_call)
    assert ".textContent = title" in page
    assert ".textContent = body" in page
    assert ".textContent = pairing.code" in page
    assert "prefillConnectionTarget(host, port)" in page
    assert "innerHTML" not in page


def test_pairing_is_offered_with_the_origin_and_an_editable_name_before_any_code() -> None:
    offer = PairingOffer(
        server="https://vbot.example.com",
        origin="https://vbot.example.com",
        name="Desk PC",
        target=CLOUD_TARGET,
    )

    page = build_connection_html([CLOUD], offer=offer, screen_key="k3y")

    # The offer names the origin and the name the server will see; no code exists yet.
    assert (
        '<div id="connection-offer" class="pairing" role="status" '
        'data-host="https://vbot.example.com" data-port="443">'
    ) in page
    assert "Continue only if you meant to connect to https://vbot.example.com." in page
    assert 'id="offer-name" type="text" maxlength="80" autocomplete="off" value="Desk PC"' in page
    assert ">Get a code</button>" in page
    assert "api.startPairing(host, port, name, SCREEN_KEY)" in page
    assert 'var SCREEN_KEY = "k3y";' in page
    # The code panel waits hidden for the script; the form keeps the offered server.
    assert '<div id="connection-pairing" class="pairing" role="status" hidden>' in page
    assert '<p id="pairing-code" class="code"></p>' in page
    assert "window.pywebview.api.cancelPairing()" in page
    assert 'value="https://vbot.example.com"' in page
    assert offer.to_payload() == {
        "host": "https://vbot.example.com",
        "port": 443,
        "server": "https://vbot.example.com",
        "origin": "https://vbot.example.com",
        "name": "Desk PC",
        "title": "Sign in this Desktop",
        "body": offer.body,
    }
    idle = build_connection_html([CLOUD])
    assert '<div id="connection-offer" class="pairing" role="status" hidden>' in idle


def test_a_pairing_code_tells_where_alone_to_enter_it() -> None:
    view = PairingView(
        code="K7M2-Q9XA",
        server="https://vbot.example.com",
        origin="https://vbot.example.com",
        target=CLOUD_TARGET,
    )

    assert view.title == "Approve this Desktop"
    assert view.body.startswith("Enter the code K7M2-Q9XA only in vBot at https://vbot.example.com")
    assert "Never enter it for another address" in view.body
    assert "vbot devices approve K7M2-Q9XA" in view.body
    assert view.to_payload() == {
        "code": "K7M2-Q9XA",
        "server": "https://vbot.example.com",
        "origin": "https://vbot.example.com",
        "title": view.title,
        "body": view.body,
    }


def test_forget_sign_in_is_offered_for_signed_in_servers_only() -> None:
    servers = [CLOUD, ServerEntry("pi.lan", 9000)]

    page = build_connection_html(servers, signed_in={"https://vbot.example.com"})

    assert page.count(">Forget sign-in</button>") == 1
    assert 'data-forget-host="https://vbot.example.com" data-forget-port="443"' in page
    assert "window.pywebview.api.forgetSignIn(" in page
    assert "button.dataset.forgetPort, 10), SCREEN_KEY" in page
    assert ">Forget sign-in</button>" not in build_connection_html(servers)


@pytest.mark.parametrize(
    ("status", "title"),
    [
        (PROBE_SERVER_UNREACHABLE, "Server unreachable"),
        (PROBE_WEBUI_UNAVAILABLE, "WebUI unavailable"),
        (PROBE_NOT_VBOT_SERVER, "Not a vBot server"),
        (PROBE_INVALID_TARGET, "Invalid server address"),
        (PROBE_ACCESS_DENIED, "Access denied"),
        (STATUS_PAIRING_DENIED, "Sign-in denied"),
        (STATUS_PAIRING_EXPIRED, "Code expired"),
        (STATUS_RATE_LIMITED, "Too many attempts"),
        (STATUS_DEVICES_UNAVAILABLE, "Sign-in unavailable"),
        (STATUS_SIGN_IN_FAILED, "Sign-in failed"),
        (STATUS_HTTPS_REQUIRED, "Sign-in needs https://"),
    ],
)
def test_each_failed_connect_shows_its_own_alert(status: str, title: str) -> None:
    page = build_connection_html([], DesktopProbeResult(status=status, target=CLOUD_TARGET))

    assert connection_error_copy(status)[0] == title
    assert f'<h2 id="connection-error-title">{title}</h2>' in page
    assert 'role="alert"' in page
    assert 'value="https://vbot.example.com"' in page


def test_an_unknown_status_has_no_copy() -> None:
    with pytest.raises(ValueError):
        connection_error_copy("webui_available")


@pytest.mark.parametrize("state", ["error", "waiting", "offer"])
def test_connection_html_escapes_the_target_host_in_its_banner_and_prefill(state: str) -> None:
    malicious = '<script>alert("x")</script>'
    target = DesktopTarget(malicious, 9000, "")
    if state == "error":
        page = build_connection_html(
            [], DesktopProbeResult(status=PROBE_INVALID_TARGET, target=target)
        )
    elif state == "waiting":
        page = build_connection_html([], waiting_for=target)
    else:
        offer = PairingOffer(server=malicious, origin=malicious, name=malicious, target=target)
        page = build_connection_html([], offer=offer)

    assert malicious not in page
    assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;" in page


def test_connection_html_escapes_saved_server_label_and_host() -> None:
    servers = [ServerEntry('<b>"evil"</b>', 9000, "<i>label</i>")]

    page = build_connection_html(servers)

    assert "<b>" not in page
    assert "<i>label</i>" not in page
    assert "&lt;b&gt;" in page
    assert "&lt;i&gt;label&lt;/i&gt;" in page


def test_quote_bearing_host_never_breaks_out_of_the_data_attribute() -> None:
    # A host that would break out of the old inline onclick JS-string context.
    # Validation rejects it before storage; a directly built entry must still
    # render only html-escaped inside data-host, with no inline onclick.
    malicious = "a');document.title='x';('"
    with pytest.raises(ValueError):
        validate_host(malicious)

    page = build_connection_html([ServerEntry(malicious, 9000)], signed_in={"http://x"})

    assert 'data-host="a&#x27;);document.title=&#x27;x&#x27;;(&#x27;"' in page
    assert malicious not in page
    assert "');" not in page
    assert 'onclick="connectSaved(' not in page
