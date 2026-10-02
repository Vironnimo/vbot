"""Windows-native glue for the Desktop shell.

DPI bootstrap and screen conversion, the per-user single-instance guard with
its activation request handoff, the WebView2 browser arguments (secure remote
HTTP origins, autoplay), the WebView2 microphone permission hook, and the Win32
DLL loading that the Desktop's ctypes bindings share. Every function is a no-op
on other platforms and loads Win32/.NET code only on Windows; the DLL helpers
raise ``OSError`` there.
"""

from __future__ import annotations

import contextlib
import ctypes
import hashlib
import importlib
import ipaddress
import json
import logging
import math
import os
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from copy import copy
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Protocol
from urllib.parse import urlsplit

logger = logging.getLogger("vbot.desktop")

WEBVIEW2_BROWSER_ARGUMENTS_ENV = "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"
_SECURE_ORIGIN_SWITCH = "--unsafely-treat-insecure-origin-as-secure"
_DISABLE_FEATURES_SWITCH = "--disable-features"
# Hands-free Live voice starts (wakeword, hotkey) have no user gesture.
_AUTOPLAY_SWITCH = "--autoplay-policy=no-user-gesture-required"
# pywebview passes this through its own WebView2 options; repeat it here so a
# browser-argument override from the environment cannot drop it.
_PYWEBVIEW_DISABLED_FEATURES = ("ElasticOverscroll",)
_LIST_SWITCHES = (_SECURE_ORIGIN_SWITCH, _DISABLE_FEATURES_SWITCH)
_ORIGIN_HOST_CHARACTERS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789.-_:")
_ACTIVATION_POLL_MS = 500
ACTIVATION_REQUEST_FILE_NAME = "activation-request.json"
ACTIVATION_REQUEST_MAX_AGE_SECONDS = 60.0
#: How long a restart successor waits for its predecessor to release the guard.
HANDOFF_CLAIM_TIMEOUT_SECONDS = 30.0
_HANDOFF_CLAIM_POLL_SECONDS = 0.1


def win32_library(name: str) -> ctypes.CDLL:
    """Load a Win32 system DLL whose calls record their error for :func:`win32_last_error`."""
    if sys.platform != "win32":
        raise OSError(f"{name}.dll exists only on Windows")
    return ctypes.WinDLL(name, use_last_error=True)


def win32_last_error() -> int:
    """Return the error of this thread's latest call into a :func:`win32_library` DLL."""
    if sys.platform != "win32":
        raise OSError("Win32 errors exist only on Windows")
    return ctypes.get_last_error()


def configure_dpi() -> None:
    """Select per-monitor rendering before pywebview queries screens or creates HWNDs.

    pywebview's later SetProcessDPIAware call only selects system DPI awareness.
    Establishing PerMonitorV2 first prevents Windows bitmap-scaling the WebView
    when the window crosses monitors. This applies to every Desktop entrypoint,
    including an ordinary Python host without a Desktop-specific manifest.
    """
    if sys.platform != "win32":
        return

    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    set_awareness = user32.SetProcessDpiAwarenessContext
    set_awareness.argtypes = [ctypes.c_void_p]
    set_awareness.restype = wintypes.BOOL
    get_context = user32.GetThreadDpiAwarenessContext
    get_context.argtypes = []
    get_context.restype = ctypes.c_void_p
    equal_contexts = user32.AreDpiAwarenessContextsEqual
    equal_contexts.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    equal_contexts.restype = wintypes.BOOL
    per_monitor_v2 = ctypes.c_void_p(-4)
    if set_awareness(per_monitor_v2):
        return
    error = ctypes.get_last_error()
    # A manifest or a previous Desktop launch may already have selected V2.
    if not equal_contexts(get_context(), per_monitor_v2):
        logger.warning("Desktop per-monitor DPI awareness could not be enabled (error=%s)", error)


def configure_winforms() -> None:
    """Opt the Python-hosted .NET Framework into dynamic WinForms DPI handling.

    Python hosts have no TargetFrameworkAttribute. Without this opt-in WinForms
    ignores its PerMonitorV2 configuration even when Windows reports that mode.
    Set both values before pywebview loads WinForms through screen discovery.
    Modern .NET already uses the process DPI context and needs no Framework file.
    """
    if sys.platform != "win32":
        return
    importlib.import_module("clr")
    system = importlib.import_module("System")
    if system.Environment.Version.Major == 4:
        domain = system.AppDomain.CurrentDomain
        domain.SetData("APP_CONFIG_FILE", str(Path(__file__).with_name("windows.config")))
        domain.SetData("TargetFrameworkName", ".NETFramework,Version=v4.8")


def primary_scale() -> float:
    """Return the primary display scale after per-monitor awareness is enabled."""
    if sys.platform != "win32":
        return 1.0
    from ctypes import wintypes

    get_dpi = ctypes.WinDLL("user32", use_last_error=True).GetDpiForSystem
    get_dpi.argtypes = []
    get_dpi.restype = wintypes.UINT
    return int(get_dpi()) / 96.0


def logical_primary_screen(screen: Any) -> Any:
    """Convert WinForms' physical screen/work-area pixels for pywebview's API.

    With PerMonitorV2 enabled, pywebview's Windows screen enumeration returns
    physical bounds and a misleading scale of 1. Its window constructor still
    expects logical dimensions. Only the origin-containing primary is used for
    startup; preserve the original snapshot and normalize a private copy.
    """
    scale = primary_scale()
    if scale == 1.0:
        return screen
    result = copy(screen)
    for name in ("x", "y", "width", "height"):
        setattr(result, name, int(getattr(screen, name) / scale))
    result.scale = scale
    frame = getattr(screen, "frame", None)
    if frame is not None:
        result.frame = SimpleNamespace(
            **{name: int(getattr(frame, name) / scale) for name in ("X", "Y", "Width", "Height")}
        )
    return result


def bind_window_dpi(window: Any, minimum_size: tuple[int, int]) -> None:
    """Keep the resize floor in logical pixels across WinForms DPI changes."""
    if sys.platform != "win32":
        return

    def dpi_changed(sender: Any, event: Any) -> None:
        scale = event.DeviceDpiNew / 96.0
        # .NET Framework otherwise retains the old physical minimum, which can
        # enlarge a small window during the move or prevent shrinking it again.
        minimum = type(sender.MinimumSize)(
            round(minimum_size[0] * scale), round(minimum_size[1] * scale)
        )
        sender.MinimumSize = minimum

        def finish_layout() -> None:
            # Framework autoscaling can rewrite MinimumSize after DpiChanged.
            if not sender.IsDisposed:
                sender.MinimumSize = minimum

        system = importlib.import_module("System")
        sender.BeginInvoke(system.Action(finish_layout))

    def before_show() -> None:
        # pywebview runs before_show synchronously on the native GUI thread.
        window.native.DpiChanged += dpi_changed

    window.events.before_show += before_show


# -- Single instance ----------------------------------------------------------


class InstanceApi(Protocol):
    """Win32 kernel objects used by the single-instance guard."""

    def create_mutex(self, name: str) -> tuple[int, bool]:
        """Return ``(handle, already_existed)``; a zero handle means failure."""

    def create_event(self, name: str) -> int:
        """Open or create a named auto-reset event; zero means failure."""

    def signal(self, handle: int) -> bool:
        """Set the event."""

    def wait(self, handle: int, timeout_ms: int) -> bool | None:
        """Return ``True`` when signaled, ``False`` on timeout, ``None`` on failure."""

    def allow_foreground(self) -> None:
        """Let the running instance take the foreground from this process."""

    def close(self, handle: int) -> None:
        """Close one kernel handle."""


class _Win32InstanceApi:
    """ctypes binding of :class:`InstanceApi` with explicit signatures."""

    def __init__(self) -> None:
        from ctypes import wintypes

        kernel32 = win32_library("kernel32")
        user32 = win32_library("user32")
        self._create_mutex = kernel32.CreateMutexW
        self._create_mutex.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        self._create_mutex.restype = wintypes.HANDLE
        self._create_event = kernel32.CreateEventW
        self._create_event.argtypes = [
            ctypes.c_void_p,
            wintypes.BOOL,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        ]
        self._create_event.restype = wintypes.HANDLE
        self._set_event = kernel32.SetEvent
        self._set_event.argtypes = [wintypes.HANDLE]
        self._set_event.restype = wintypes.BOOL
        self._wait = kernel32.WaitForSingleObject
        self._wait.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self._wait.restype = wintypes.DWORD
        self._close_handle = kernel32.CloseHandle
        self._close_handle.argtypes = [wintypes.HANDLE]
        self._close_handle.restype = wintypes.BOOL
        self._allow_foreground = user32.AllowSetForegroundWindow
        self._allow_foreground.argtypes = [wintypes.DWORD]
        self._allow_foreground.restype = wintypes.BOOL

    def create_mutex(self, name: str) -> tuple[int, bool]:
        error_already_exists = 183
        handle = self._create_mutex(None, False, name)
        already_existed = win32_last_error() == error_already_exists
        return int(handle or 0), already_existed

    def create_event(self, name: str) -> int:
        # Auto-reset: each signal wakes the running instance exactly once.
        return int(self._create_event(None, False, False, name) or 0)

    def signal(self, handle: int) -> bool:
        return bool(self._set_event(handle))

    def wait(self, handle: int, timeout_ms: int) -> bool | None:
        wait_object_0 = 0x0
        wait_timeout = 0x102
        result = self._wait(handle, timeout_ms)
        if result == wait_object_0:
            return True
        if result == wait_timeout:
            return False
        return None

    def allow_foreground(self) -> None:
        asfw_any = 0xFFFFFFFF
        self._allow_foreground(asfw_any)

    def close(self, handle: int) -> None:
        self._close_handle(handle)


ActivationRequest = dict[str, Any]


class DesktopInstance:
    """Ownership of the Desktop for one config directory (the first instance).

    :meth:`listen` runs ``on_activate`` on a daemon thread each time a later
    launch asks this instance to come to the front, passing the request that
    launch handed over (or ``None``); :meth:`close` releases the guard on exit.
    Both are no-ops for the platform-neutral instance.
    """

    def __init__(
        self,
        api: InstanceApi | None = None,
        mutex: int = 0,
        event: int = 0,
        *,
        request_path: Path | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._api = api
        self._mutex = mutex
        self._event = event
        self._request_path = request_path
        self._clock = clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def listen(self, on_activate: Callable[[ActivationRequest | None], None]) -> None:
        """Start the activation listener once."""

        if self._api is None or not self._event or self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._wait_for_activation,
            args=(self._api, self._event, on_activate),
            name="vbot-desktop-activation",
            daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        """Stop listening and release the guard; idempotent."""

        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2 * _ACTIVATION_POLL_MS / 1000)
        api = self._api
        self._api = None
        if api is None:
            return
        for handle in (self._event, self._mutex):
            if handle:
                api.close(handle)
        self._event = 0
        self._mutex = 0

    def _wait_for_activation(
        self,
        api: InstanceApi,
        event: int,
        on_activate: Callable[[ActivationRequest | None], None],
    ) -> None:
        while not self._stop.is_set():
            signaled = api.wait(event, _ACTIVATION_POLL_MS)
            if signaled is None:
                logger.warning("Desktop activation listener stopped: waiting failed")
                return
            if signaled and not self._stop.is_set():
                logger.info("Another Desktop launch asked this window to come to the front")
                request = (
                    _take_activation_request(self._request_path, now=self._clock())
                    if self._request_path is not None
                    else None
                )
                try:
                    on_activate(request)
                except Exception:
                    logger.warning(
                        "Desktop window could not be brought to the front", exc_info=True
                    )


def instance_scope(config_directory: Path) -> str:
    """Return the kernel-object name stem for one Desktop config directory."""

    resolved = os.path.normcase(os.path.abspath(os.fspath(config_directory)))
    digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:16]
    return f"Local\\vBot.Desktop.{digest}"


def claim_desktop_instance(
    config_directory: Path,
    *,
    request: Mapping[str, Any] | None = None,
    handoff: str | None = None,
    api: InstanceApi | None = None,
    clock: Callable[[], float] = time.time,
    monotonic: Callable[[], float] = time.monotonic,
    pause: Callable[[float], None] = time.sleep,
) -> DesktopInstance | None:
    """Claim the Desktop for ``config_directory`` or hand off to the running one.

    Returns the owning :class:`DesktopInstance`, or ``None`` after signaling an
    already running instance to come to the front (the caller must then exit
    without creating a window). A second process would otherwise share the
    WebView2 profile, and with different browser arguments WebView2 refuses to
    start and leaves a blank window. The scope is the config directory, so
    independent settings directories (tests, portable setups) never collide.
    Other platforms have no guard; failures to create the guard are logged and
    the launch continues unguarded.

    ``request`` (a JSON object) travels with the signal: it is written
    atomically to :data:`ACTIVATION_REQUEST_FILE_NAME` in the config directory
    first, and the running instance's listener takes it (reads and deletes the
    file) when it wakes. A request older than
    :data:`ACTIVATION_REQUEST_MAX_AGE_SECONDS` or malformed reaches the listener
    as ``None``. A request that cannot be written is logged; the running
    instance still comes to the front.

    ``handoff`` makes this launch the successor of a restarting Desktop (see
    :mod:`desktop.restart`): instead of a request to come to the front, the
    running instance receives ``{"handoff": handoff}``, and this launch waits
    up to :data:`HANDOFF_CLAIM_TIMEOUT_SECONDS` for it to exit and release the
    guard, then owns the Desktop. It returns ``None`` when the guard stays
    taken.
    """

    if sys.platform != "win32":
        return DesktopInstance()
    request_path = Path(config_directory) / ACTIVATION_REQUEST_FILE_NAME
    try:
        instance_api = api if api is not None else _Win32InstanceApi()
        scope = instance_scope(config_directory)
        mutex, already_running = instance_api.create_mutex(f"{scope}.instance")
        if not mutex:
            logger.warning("Desktop single-instance guard could not be created")
            return DesktopInstance()
        event = instance_api.create_event(f"{scope}.activate")
    except Exception:
        logger.warning("Desktop single-instance guard could not be created", exc_info=True)
        return DesktopInstance()
    if not already_running:
        if not event:
            logger.warning("Desktop activation event could not be created")
        # A request left over from an ended instance expires on its own; deleting
        # it here could drop the request of a launch that raced this one.
        return DesktopInstance(instance_api, mutex, event, request_path=request_path, clock=clock)
    if handoff is not None:
        request = {"handoff": handoff}
    try:
        if event:
            if request is not None:
                _write_activation_request(request_path, request, created_at=clock())
            instance_api.allow_foreground()
            instance_api.signal(event)
    finally:
        for handle in (event, mutex):
            if handle:
                instance_api.close(handle)
    if handoff is None:
        return None
    return _claim_after_predecessor(
        instance_api, scope, request_path, clock=clock, monotonic=monotonic, pause=pause
    )


def _claim_after_predecessor(
    api: InstanceApi,
    scope: str,
    request_path: Path,
    *,
    clock: Callable[[], float],
    monotonic: Callable[[], float],
    pause: Callable[[float], None],
) -> DesktopInstance | None:
    """Wait for the restarting Desktop to release the guard, then own it."""

    deadline = monotonic() + HANDOFF_CLAIM_TIMEOUT_SECONDS
    while monotonic() < deadline:
        pause(_HANDOFF_CLAIM_POLL_SECONDS)
        mutex, already_running = api.create_mutex(f"{scope}.instance")
        if not mutex:
            logger.warning("Desktop single-instance guard could not be created")
            return DesktopInstance()
        if not already_running:
            event = api.create_event(f"{scope}.activate")
            if not event:
                logger.warning("Desktop activation event could not be created")
            logger.info("The previous Desktop handed over; this Desktop now owns the window")
            return DesktopInstance(api, mutex, event, request_path=request_path, clock=clock)
        # The guard disappears only when no process holds a handle to it.
        api.close(mutex)
    logger.warning("The previous Desktop did not hand over in time; not opening a window")
    return None


def _write_activation_request(path: Path, request: Mapping[str, Any], *, created_at: float) -> None:
    """Write the request for the running instance in one atomic replace."""

    try:
        payload = json.dumps({"created_at": created_at, "request": dict(request)})
        path.parent.mkdir(parents=True, exist_ok=True)
        # Leaving the block removes the temporary file unless it replaced *path*.
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            delete_on_close=False,
            prefix=f".{path.name}.",
            suffix=".tmp",
        ) as temporary_file:
            temporary_file.write(payload)
            temporary_file.close()
            Path(temporary_file.name).replace(path)
    except (OSError, TypeError, ValueError):
        logger.warning("The request for the running Desktop could not be written", exc_info=True)


def _take_activation_request(path: Path, *, now: float) -> ActivationRequest | None:
    """Read and delete the handed-over request; ``None`` when absent, stale, or malformed."""

    try:
        payload = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError):
        logger.warning("The request of another Desktop launch could not be read", exc_info=True)
        payload = None
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning("The request of another Desktop launch could not be deleted", exc_info=True)
    if payload is None:
        return None
    try:
        document = json.loads(payload)
    except ValueError:
        document = None
    created_at = document.get("created_at") if isinstance(document, dict) else None
    request = document.get("request") if isinstance(document, dict) else None
    if (
        not isinstance(created_at, int | float)
        or isinstance(created_at, bool)
        or not isinstance(request, dict)
    ):
        logger.warning("Ignoring a malformed request from another Desktop launch")
        return None
    age = now - created_at
    if not math.isfinite(age) or abs(age) > ACTIVATION_REQUEST_MAX_AGE_SECONDS:
        logger.info("Ignoring a stale request from another Desktop launch")
        return None
    return request


# -- WebView2 browser arguments -------------------------------------------------


def _is_loopback_host(host: str) -> bool:
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def http_origin(host: str, port: int) -> str | None:
    """Return the ``http://`` origin of a server, or ``None`` when unusable.

    IPv6 literals are bracketed; port 80 is implied, as browsers serialize it.
    """

    normalized = host.strip().strip("[]").lower()
    if not normalized or not set(normalized) <= _ORIGIN_HOST_CHARACTERS:
        return None
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        return None
    if ":" in normalized:
        try:
            normalized = f"[{ipaddress.IPv6Address(normalized).compressed}]"
        except ValueError:
            return None
    return f"http://{normalized}" if port == 80 else f"http://{normalized}:{port}"


def url_origin(url: str | None) -> str | None:
    """Return the serialized origin of an absolute ``http(s)`` URL."""

    if not url:
        return None
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in {"http", "https"} or not host:
        return None
    default_port = 80 if parts.scheme == "http" else 443
    if ":" in host:
        host = f"[{host}]"
    if port is None or port == default_port:
        return f"{parts.scheme}://{host}"
    return f"{parts.scheme}://{host}:{port}"


def trusted_http_origins(targets: Iterable[tuple[str, int]]) -> tuple[str, ...]:
    """Return the sorted, unique non-loopback ``http://`` origins of ``targets``.

    Loopback is already a secure context and never needs the switch.
    """

    origins = set()
    for host, port in targets:
        if _is_loopback_host(host.strip().strip("[]").lower()):
            continue
        origin = http_origin(host, port)
        if origin is not None:
            origins.add(origin)
    return tuple(sorted(origins))


def build_browser_arguments(existing: str | None, origins: Iterable[str]) -> str:
    """Merge the Desktop switches into a pre-existing WebView2 argument string.

    Comma-list switches (secure origins, disabled features) are merged into one
    switch each; an explicit pre-existing autoplay policy wins over ours. Other
    pre-existing switches are kept in order.
    """

    kept: list[str] = []
    lists: dict[str, list[str]] = {name: [] for name in _LIST_SWITCHES}
    for token in (existing or "").split():
        name, separator, value = token.partition("=")
        if separator and name in lists:
            lists[name].extend(item for item in value.split(",") if item)
        else:
            kept.append(token)
    if not any(token.partition("=")[0] == "--autoplay-policy" for token in kept):
        kept.append(_AUTOPLAY_SWITCH)
    lists[_DISABLE_FEATURES_SWITCH].extend(_PYWEBVIEW_DISABLED_FEATURES)
    lists[_SECURE_ORIGIN_SWITCH] = sorted({*lists[_SECURE_ORIGIN_SWITCH], *origins})
    for name in _LIST_SWITCHES:
        values = list(dict.fromkeys(lists[name]))
        if values:
            kept.append(f"{name}={','.join(values)}")
    return " ".join(kept)


def webview_secure_origins(targets: Iterable[tuple[str, int]]) -> tuple[str, ...]:
    """Return the remote HTTP origins WebView2 treats as secure on this platform."""

    if sys.platform != "win32":
        return ()
    return trusted_http_origins(targets)


def apply_browser_arguments(origins: Iterable[str]) -> None:
    """Export the Desktop WebView2 switches before ``webview.start`` creates WebView2.

    WebView2 reads the variable once per process when its environment is
    created, so the origin list is fixed until the Desktop restarts. Elevated
    processes ignore it. Other platforms' backends do not read it.
    """

    if sys.platform != "win32":
        return
    value = build_browser_arguments(os.environ.get(WEBVIEW2_BROWSER_ARGUMENTS_ENV), origins)
    os.environ[WEBVIEW2_BROWSER_ARGUMENTS_ENV] = value
    logger.info("WebView2 browser arguments: %s", value)


# -- WebView2 microphone permission ---------------------------------------------


def allow_server_microphone(window: Any, allowed_origin: Callable[[], str | None]) -> None:
    """Grant microphone requests from the connected server without a prompt.

    pywebview leaves WebView2 ``PermissionRequested`` unhandled, so WebView2
    would prompt and remember the answer in the profile. A microphone request
    whose origin equals ``allowed_origin()`` (read at request time) is allowed
    for this request only; every other request keeps WebView2's default
    handling. The handler runs on the GUI thread and must not block. A failure
    is logged once per stage and leaves the default prompt in place.
    """

    if sys.platform != "win32":
        return
    failures: set[str] = set()
    webview2_core: list[Any] = []

    def report(stage: str) -> None:
        if stage not in failures:
            failures.add(stage)
            logger.warning("Desktop microphone permission hook failed (%s)", stage, exc_info=True)

    def permission_requested(_sender: Any, args: Any) -> None:
        try:
            core = webview2_core[0]
            if args.PermissionKind != core.CoreWebView2PermissionKind.Microphone:
                return
            origin = url_origin(str(args.Uri))
            if origin is None or origin != allowed_origin():
                return
            args.State = core.CoreWebView2PermissionState.Allow
            # Older WebView2 SDKs have no per-request persistence switch.
            with contextlib.suppress(AttributeError):
                args.SavesInProfile = False
        except Exception:
            report("request")

    def initialization_completed(sender: Any, args: Any) -> None:
        try:
            if not args.IsSuccess:
                return
            # pywebview has loaded the WebView2 assemblies by now.
            webview2_core.append(importlib.import_module("Microsoft.Web.WebView2.Core"))
            sender.CoreWebView2.PermissionRequested += permission_requested
        except Exception:
            report("initialization")

    def before_show() -> None:
        # pywebview runs before_show synchronously on the GUI thread, before the
        # WebView2 control can complete its asynchronous initialization.
        try:
            window.native.browser.webview.CoreWebView2InitializationCompleted += (
                initialization_completed
            )
        except Exception:
            report("attach")

    window.events.before_show += before_show
