"""Bind the pinned pywebview dispatch to a document in vBot's selected server.

6.2.1 resolves caller-supplied dotted names on the native object and sends its
reply to whichever page is in the window then. Intercept its two transport
hooks before the GUI starts. The normal JS stubs, including older WebUIs, keep
their signatures. No private attributes or generic pywebview calls are exposed.
"""

from __future__ import annotations

import importlib
import json
import secrets
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, cast

from desktop._windows import url_origin
from desktop.bridge import DesktopBridge
from desktop.page_permissions import PagePermissions

_SCREEN_METHODS = frozenset({"connect", "startPairing", "forgetSignIn", "cancelPairing"})


class DocumentBridge:
    def __init__(
        self,
        bridge: DesktopBridge,
        permissions: PagePermissions,
        selected: Callable[[], str | None],
    ) -> None:
        self._permissions = permissions
        self._selected = selected
        self._methods = {
            name: getattr(bridge, name)
            for name, method in vars(DesktopBridge).items()
            if not name.startswith("_") and callable(method)
        }
        self._lock = threading.Lock()
        self._key = ""
        self._origin: str | None = None

    def invalidate(self) -> None:
        with self._lock:
            self._key = ""
            self._origin = None

    def prepare(self, page_url: str | None) -> str:
        """Issue one credential for the native renderer's current top-level URL."""
        origin = url_origin(page_url)
        selected = url_origin(self._selected())
        shell = not page_url or page_url.startswith(("about:", "data:", "file:"))
        with self._lock:
            self._origin = origin
            self._key = secrets.token_hex(32) if shell or origin and origin == selected else ""
            key = self._key
        if not key:
            return ""
        condition = (
            f"window.location.origin === {json.dumps(origin)}"
            if origin
            else '(window.location.origin === "null" || window.location.protocol === "file:")'
        )
        return f"""
        if ({condition}) {{
            (() => {{
                "use strict";
                const api = window.pywebview;
                const send = api._jsApiCallback.bind(api);
                const key = "{key}";
                api._vbotDocument = key;
                api._jsApiCallback = (name, args, id) => send(name, [key, ...args], id);
            }})();
        }}
        """

    def dispatch(
        self, window: Any, name: Any, args: Any, value_id: Any, *, source: str | None = None
    ) -> None:
        """Handle only published methods with the current document's credential."""
        if not isinstance(name, str) or name not in self._methods or not isinstance(args, list):
            return
        if not args or not isinstance(args[0], str) or not isinstance(value_id, str):
            return
        key = args[0]
        with self._lock:
            origin = self._origin
            if not key or key != self._key or (name in _SCREEN_METHODS) != (origin is None):
                return
            if source is not None and url_origin(source) != origin:
                return
        method = self._methods[name]

        def current() -> bool:
            with self._lock:
                return key == self._key and (not origin or origin == url_origin(self._selected()))

        def call() -> None:
            try:
                if not current():
                    return
                with self._permissions.document(origin, current):
                    value = method(*args[1:])
                result: dict[str, object] = {"value": json.dumps(value)}
            except Exception as exc:
                result = {"isError": True, "value": json.dumps({"message": str(exc)})}
            with self._lock:
                if key != self._key:
                    return
            # Selecting another server changes the native target before the
            # source document consumes its response. Bind delivery to the actual
            # JS document, independent of which target happens to be active now.
            match_origin = f"window.location.origin === {json.dumps(origin)} && " if origin else ""
            window.run_js(
                f"{match_origin}window.pywebview?._vbotDocument === {json.dumps(key)} && "
                f"window.pywebview._returnValuesCallbacks[{json.dumps(name)}]"
                f"?.[{json.dumps(value_id)}]?.({json.dumps(result)})"
            )

        threading.Thread(target=call, name="vbot-desktop-bridge", daemon=True).start()


@contextmanager
def bind_webview_transport(
    window: Any,
    bridge: DesktopBridge,
    permissions: PagePermissions,
    selected: Callable[[], str | None],
) -> Iterator[None]:
    """Use the same restricted dispatcher for current and lazily loaded backends."""
    util = importlib.import_module("webview.util")
    transport = DocumentBridge(bridge, permissions, selected)
    original_run_js = window.run_js
    original_dispatch = util.js_bridge_call

    def run_js(script: str) -> Any:
        # 6.2.1 runs its final stub setup on the injection worker, where the
        # renderer URL is safe to read directly before the loaded event.
        if script.startswith("window.pywebview._createApi("):
            script = transport.prepare(window.gui.get_current_url(window.uid)) + script
        return original_run_js(script)

    def dispatch(target: Any, name: Any, args: Any, identifier: Any) -> None:
        if target is window:
            transport.dispatch(target, name, args, identifier)

    def replace_dispatch(previous: Any, replacement: Any) -> None:
        for module_name, module in tuple(sys.modules.items()):
            if (module is util or module_name.startswith("webview.platforms.")) and getattr(
                module, "js_bridge_call", None
            ) is previous:
                cast(Any, module).js_bridge_call = replacement

    sdk: Any = importlib.import_module("webview")
    original_initialize = sdk.initialize
    edge: Any = None
    original_notify: Any = None
    original_starting: Any = None

    def initialize(*args: Any, **kwargs: Any) -> Any:
        nonlocal edge, original_notify, original_starting
        gui = original_initialize(*args, **kwargs)
        if gui.renderer == "edgechromium":
            edge = importlib.import_module("webview.platforms.edgechromium")
            original_notify = edge.EdgeChrome.on_script_notify
            original_starting = edge.EdgeChrome.on_navigation_start

            def message_received(browser: Any, _sender: Any, event: Any) -> None:
                # The browser supplies Source independently of any page data.
                # vBot uses no pywebview file-drop, console, or generic host messages.
                if browser.pywebview_window is not window:
                    return
                try:
                    message = json.loads(event.get_WebMessageAsJson())
                    if not isinstance(message, list) or len(message) != 3:
                        return
                    name, arguments, identifier = message
                    transport.dispatch(
                        window, name, json.loads(arguments), identifier, source=str(event.Source)
                    )
                except TypeError, ValueError:
                    return

            def navigation_start(browser: Any, sender: Any, event: Any) -> None:
                transport.invalidate()
                original_starting(browser, sender, event)

            edge.EdgeChrome.on_script_notify = message_received
            edge.EdgeChrome.on_navigation_start = navigation_start
        elif sys.platform == "win32":
            raise RuntimeError("vBot Desktop requires WebView2 on Windows")
        return gui

    replace_dispatch(original_dispatch, dispatch)
    sdk.initialize = initialize
    window.events.before_load += transport.invalidate
    window.run_js = run_js
    try:
        yield
    finally:
        sdk.initialize = original_initialize
        window.run_js = original_run_js
        window.events.before_load -= transport.invalidate
        replace_dispatch(dispatch, original_dispatch)
        if edge is not None:
            edge.EdgeChrome.on_script_notify = original_notify
            edge.EdgeChrome.on_navigation_start = original_starting
