"""The pywebview ``js_api`` facade of the Desktop window.

pywebview exposes the public methods of :class:`DesktopBridge` to JavaScript as
``window.pywebview.api.<method>()``; every call runs on its own thread and
returns plain JSON values. A raised exception rejects the JavaScript Promise
with an ``Error`` carrying the exception's message. Error contract: a known,
user-actionable failure (an exception with a stable ``error_code`` attribute)
rejects with a :class:`BridgeError` whose message is exactly that code, so the
page can explain it; any other failure keeps its own message, which never
looks like a code, and is logged with its traceback.

The same instance stays the window's ``js_api`` across navigation, so it
serves both the shell connection screen (server selection) and the remote
WebUI (capabilities, clipboard and browser, the Live voice hotkey, Voice). The
facade holds no Voice state: Voice methods delegate to
:class:`desktop.wakeword.controller.VoiceController`, server selection to the
connection controller.
"""

from __future__ import annotations

import base64
import binascii
import functools
import inspect
import logging
import re
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol, TypeVar, cast

from desktop.system_actions import DesktopSystemActions
from desktop.wakeword.engine import MAX_CUSTOM_WAKEWORD_MODEL_BYTES, WakewordModelError

if TYPE_CHECKING:
    from desktop.connection import PreparedConnection, ServerEntry
    from desktop.hotkey import LiveHotkeyController
    from desktop.wakeword.controller import VoiceController

logger = logging.getLogger("vbot.desktop.bridge")

VOICE_API_VERSION = 2
"""Version of the Voice bridge methods; the WebUI enables Voice only for this version."""

_MAX_MODEL_BASE64_CHARS = 4 * ((MAX_CUSTOM_WAKEWORD_MODEL_BYTES + 2) // 3)
_ERROR_CODE = re.compile(r"[a-z][a-z0-9_]*")

_BridgeClass = TypeVar("_BridgeClass", bound=type)


class BridgeError(Exception):
    """A known, user-actionable bridge failure whose message is exactly its ``error_code``."""

    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code


def _reject_with_error_codes(cls: _BridgeClass) -> _BridgeClass:
    """Apply the module's error contract to every public method of ``cls``."""
    for name, member in list(vars(cls).items()):
        if not name.startswith("_") and inspect.isfunction(member):
            setattr(cls, name, _bridge_method(member))
    return cls


def _bridge_method(method: Callable[..., Any]) -> Callable[..., Any]:
    name = method.__name__

    @functools.wraps(method)
    def call(*args: Any, **kwargs: Any) -> Any:
        try:
            return method(*args, **kwargs)
        except Exception as exc:
            error_code = getattr(exc, "error_code", None)
            if isinstance(error_code, str) and _ERROR_CODE.fullmatch(error_code):
                field = getattr(exc, "field", None)
                logger.warning(
                    "Desktop bridge %s failed (error_code=%s%s): %s",
                    name,
                    error_code,
                    f", field={field}" if field else "",
                    exc,
                )
                raise BridgeError(error_code) from exc
            logger.exception("Desktop bridge %s failed unexpectedly", name)
            if _ERROR_CODE.fullmatch(str(exc)):
                raise RuntimeError(f"The Desktop could not complete {name}") from exc
            raise

    # pywebview reads the JavaScript parameter names from the signature.
    call.__signature__ = inspect.signature(method)  # type: ignore[attr-defined]
    return call


class ConnectionDelegate(Protocol):
    """The server-selection surface the bridge delegates to (``ConnectionController``)."""

    def prepare_connect(self, host: str, port: Any, label: str | None = ...) -> PreparedConnection:
        """Probe and persist a target without replacing the calling document."""

    def add_server(self, host: str, port: Any, label: str | None = ...) -> ServerEntry:
        """Remember a server without connecting."""

    def remove_server(self, host: str, port: Any) -> bool:
        """Forget a remembered server, reporting whether one was removed."""

    def list_servers(self) -> list[ServerEntry]:
        """Return the remembered servers in stored order."""

    def active_server_url(self) -> str | None:
        """Return the base URL of the window's current server, if any."""


@_reject_with_error_codes
class DesktopBridge:
    """Bridge API exposed to the WebUI and the connection screen via pywebview ``js_api``."""

    def __init__(
        self,
        *,
        voice: VoiceController,
        connection: ConnectionDelegate | None = None,
        system_actions: DesktopSystemActions | None = None,
        live_hotkey: LiveHotkeyController | None = None,
        secure_origins: tuple[str, ...] = (),
    ) -> None:
        self._voice = voice
        self._connection = connection
        self._system_actions = system_actions or DesktopSystemActions()
        self._live_hotkey = live_hotkey
        # Remote HTTP origins WebView2 treats as secure for this process. A
        # server added later needs a Desktop restart before its microphone works.
        self._secure_origins = tuple(secure_origins)
        # Server-selection calls mutate stored servers and navigate the single
        # window; serialize them across pywebview's call threads.
        self._connection_lock = threading.Lock()

    # -- Capabilities and system actions ---------------------------------------

    def getDesktopCapabilities(self) -> dict[str, Any]:  # noqa: N802
        """Return the Desktop feature flags for the WebUI feature gates."""
        return {
            "wakeword": True,
            "voiceApi": VOICE_API_VERSION,
            "serverSelection": True,
            "contextMenu": True,
            "liveHotkey": self._live_hotkey is not None and self._live_hotkey.supported,
            "secureOrigins": list(self._secure_origins),
        }

    def setClipboardText(self, text: Any) -> dict[str, bool]:  # noqa: N802
        """Replace the host clipboard with validated plain text."""
        self._system_actions.set_clipboard_text(text)
        return {"copied": True}

    def getClipboardText(self) -> str:  # noqa: N802
        """Return plain text from the host clipboard for an explicit paste."""
        return self._system_actions.get_clipboard_text()

    def openExternalUrl(self, url: Any) -> dict[str, bool]:  # noqa: N802
        """Open one validated HTTP(S) URL in the host's default browser."""
        self._system_actions.open_external_url(url)
        return {"opened": True}

    # -- Voice -----------------------------------------------------------------

    def getVoiceStatus(self) -> dict[str, Any]:  # noqa: N802
        """Return the Voice status snapshot."""
        return self._voice.status()

    def setVoiceEnabled(self, enabled: Any) -> dict[str, Any]:  # noqa: N802
        """Enable or disable Voice listening; returns ``{enabled, error_code}``."""
        return self._voice.set_enabled(enabled)

    def updateVoiceConfig(self, changes: Any) -> dict[str, Any]:  # noqa: N802
        """Apply one partial Voice config change; returns the status snapshot."""
        return self._voice.update_config(changes)

    def listMicrophones(self) -> list[dict[str, Any]]:  # noqa: N802
        """Return the input devices and whether Voice can use them."""
        return self._voice.list_microphones()

    def listWakewordModels(self) -> list[dict[str, Any]]:  # noqa: N802
        """Return the curated built-ins and the imported wakeword models."""
        return self._voice.list_models()

    def importWakewordModel(self, filename: Any, content_base64: Any) -> dict[str, Any]:  # noqa: N802
        """Install one base64-encoded TFLite model; returns its descriptor and ``activated``."""
        if not isinstance(filename, str):
            raise WakewordModelError("Wakeword model file name must be text")
        if not isinstance(content_base64, str):
            raise WakewordModelError("Wakeword model content must be base64 text")
        if len(content_base64) > _MAX_MODEL_BASE64_CHARS:
            raise WakewordModelError("Wakeword model exceeds the import size limit")
        try:
            content = base64.b64decode(content_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise WakewordModelError("Wakeword model content is not valid base64") from exc
        return self._voice.import_model(filename, content)

    def deleteWakewordModel(self, model_id: Any) -> dict[str, bool]:  # noqa: N802
        """Permanently remove one inactive imported model."""
        return self._voice.delete_model(model_id)

    def retryVoice(self) -> dict[str, Any]:  # noqa: N802
        """Restart Voice listening; returns the status snapshot."""
        return self._voice.retry()

    def stopVoiceRecording(self) -> dict[str, Any]:  # noqa: N802
        """End the current command recording and send it; returns the status snapshot."""
        return self._voice.stop_recording()

    def startVoiceCalibration(self, model_id: Any) -> dict[str, Any]:  # noqa: N802
        """Calibrate one active phrase; returns the status snapshot."""
        return self._voice.start_calibration(model_id)

    def restartVoiceCalibration(self) -> dict[str, Any]:  # noqa: N802
        """Restart the running calibration from room noise; returns the status snapshot."""
        return self._voice.restart_calibration()

    def stopVoiceCalibration(self) -> dict[str, Any]:  # noqa: N802
        """Leave calibration; returns the status snapshot."""
        return self._voice.stop_calibration()

    # -- Live voice hotkey -----------------------------------------------------

    def getLiveHotkey(self) -> dict[str, Any]:  # noqa: N802
        """Return ``{supported, enabled, hotkey, error_code}`` for the Live voice hotkey."""
        return self._require_live_hotkey().status()

    def setLiveHotkey(self, changes: Any) -> dict[str, Any]:  # noqa: N802
        """Merge, persist, and re-register the Live voice hotkey; returns its status."""
        return self._require_live_hotkey().update(changes)

    # -- Server selection ------------------------------------------------------

    def connect(self, host: str, port: Any) -> dict[str, str]:
        """Probe and persist a server for the connection screen, which then navigates.

        JavaScript navigates only after this call resolved: replacing the page
        inside the call would destroy pywebview's result callback.
        """
        controller = self._require_connection()
        with self._connection_lock:
            prepared = controller.prepare_connect(host, _coerce_port(port))
        return prepared.to_bridge_payload()

    def listServers(self) -> list[dict[str, Any]]:  # noqa: N802
        """Return the remembered servers and mark the window's active one."""
        controller = self._require_connection()
        with self._connection_lock:
            servers = controller.list_servers()
            active_url = (controller.active_server_url() or "").rstrip("/")
        return [
            {**entry.to_storage(), "active": f"http://{entry.host}:{entry.port}" == active_url}
            for entry in servers
        ]

    def addServer(  # noqa: N802
        self, host: str, port: Any, label: str | None = None
    ) -> dict[str, Any]:
        """Remember a server without connecting, returning the stored entry."""
        controller = self._require_connection()
        with self._connection_lock:
            entry = controller.add_server(host, _coerce_port(port), label or None)
        return entry.to_storage()

    def removeServer(self, host: str, port: Any) -> dict[str, bool]:  # noqa: N802
        """Forget a remembered server, reporting whether one was removed."""
        controller = self._require_connection()
        with self._connection_lock:
            removed = controller.remove_server(host, _coerce_port(port))
        return {"removed": removed}

    def selectServer(self, host: str, port: Any) -> dict[str, str]:  # noqa: N802
        """Prepare a remembered server connection for navigation from JavaScript."""
        controller = self._require_connection()
        with self._connection_lock:
            prepared = controller.prepare_connect(host, _coerce_port(port))
        return prepared.to_bridge_payload()

    # -- Internals -------------------------------------------------------------

    def _require_live_hotkey(self) -> LiveHotkeyController:
        if self._live_hotkey is None:
            raise RuntimeError("The Live voice hotkey is not available in this Desktop")
        return self._live_hotkey

    def _require_connection(self) -> ConnectionDelegate:
        if self._connection is None:
            raise RuntimeError("Server selection is not available in this Desktop")
        return self._connection


def _coerce_port(value: Any) -> int | str:
    """Coerce a JavaScript port to an int where possible for the controller.

    A numeric string becomes an int; anything else passes through unchanged
    so the controller's port validation rejects it with a clear message.
    """
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return value
    return cast("int | str", value)
