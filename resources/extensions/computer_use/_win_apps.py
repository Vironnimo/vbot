"""Application identity on Windows: keys, categories, Start-menu apps and launching.

An application is known by identity keys: its lowercased executable path,
``exe:<basename>`` (omitted for generic hosts such as ``python.exe`` that run
many different apps) and ``pkg:<package family>`` for packaged apps. A window
belongs to the app of its process; ApplicationFrameHost windows belong to the
UWP app they host, WebView2 popups to their owner window's app, the taskbar,
desktop and Start surfaces to File Explorer, and vBot's own processes to the
single key ``vbot:self``. Start-menu apps come from the shell's Apps folder.
"""

from __future__ import annotations

import base64
import ctypes as ct
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ElementTree
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from . import _win32
from .target import AppInfo, TargetError

_LOGGER = logging.getLogger("vbot.extensions.computer_use")

OWN_KEY = "vbot:self"
OWN_APP = AppInfo("vBot", frozenset({OWN_KEY}), "other", running=True, launchable=False)
FILE_EXPLORER = "File Explorer"
EXPLORER_APP_ID = "microsoft.windows.explorer"

BROWSERS = frozenset(
    f"{name}.exe"
    for name in (
        "chrome", "msedge", "firefox", "brave", "opera", "vivaldi", "iexplore", "arc",
        "chromium", "thorium", "librewolf", "waterfox", "floorp", "zen", "palemoon", "yandex",
    )
)  # fmt: skip
TERMINALS = frozenset(
    f"{name}.exe"
    for name in (
        "windowsterminal", "wt", "openconsole", "cmd", "powershell", "pwsh", "conhost",
        "wezterm-gui", "alacritty", "mintty", "putty", "kitty", "hyper", "tabby", "warp",
        "conemu", "conemu64", "cmder", "mobaxterm", "termius", "ghostty", "wsl", "bash",
        "powershell_ise",
    )
)  # fmt: skip
IDES = frozenset(
    f"{name}.exe"
    for name in (
        "code", "code - insiders", "codium", "cursor", "windsurf", "antigravity", "zed",
        "devenv", "studio64", "idea64", "pycharm64", "rider64", "webstorm64", "clion64",
        "goland64", "phpstorm64", "rubymine64", "datagrip64", "dataspell64", "rustrover64",
        "aqua64", "fleet", "trae", "kiro", "positron", "eclipse", "netbeans64", "qtcreator",
        "sublime_text",
    )
)  # fmt: skip
# Package families whose executable the manifest may not reveal.
_TERMINAL_PACKAGES = ("microsoft.windowsterminal", "microsoft.powershell")
SHELL_CLASSES = frozenset({"Shell_TrayWnd", "Shell_SecondaryTrayWnd", "Progman", "WorkerW"})
# Start, search and the flyouts the taskbar opens belong to the shell like the taskbar.
SHELL_HOSTS = frozenset(
    {"startmenuexperiencehost.exe", "searchhost.exe", "shellexperiencehost.exe", "shellhost.exe"}
)
# Executables that run many unrelated apps; only their full path identifies an app.
GENERIC_HOSTS = frozenset(
    f"{name}.exe"
    for name in (
        "python", "pythonw", "py", "pyw", "java", "javaw", "node", "electron", "rundll32",
        "dllhost", "mmc", "msiexec", "wscript", "cscript", "mshta", "hh", "explorer",
        "msedgewebview2", "applicationframehost", "svchost", "control",
    )
)  # fmt: skip
# Processes whose top-level popups serve another app's window (their owner).
HELPER_PROCESSES = frozenset({"msedgewebview2.exe"})
_UWP_HOST = "applicationframehost.exe"
_UWP_FRAME, _UWP_CORE = "ApplicationFrameWindow", "Windows.UI.Core.CoreWindow"
_VBOT_EXECUTABLE = re.compile(r"vbot(\.[a-z0-9]+)?\.exe")
_TIER_RANK = {"browser": 0, "terminal": 1, "ide": 1}
_DISCOVERY_PERIOD = 60.0
_DISCOVERY_TIMEOUT = 30.0
_DISCOVERY_SCRIPT = r"""
$apps = 'shell:::{4234d49b-0245-4df3-b780-3893943456e1}'
$folder = (New-Object -ComObject Shell.Application).NameSpace($apps)
$rows = foreach ($item in $folder.Items()) {
  [pscustomobject]@{
    n = [string]$item.Name
    a = [string]$item.Path
    t = [string]$item.ExtendedProperty('System.Link.TargetParsingPath')
    f = [string]$item.ExtendedProperty('System.AppUserModel.PackageFamilyName')
  }
}
$json = ConvertTo-Json -InputObject @($rows) -Compress
[regex]::Replace($json, '[^\x00-\x7F]', { param($m) '\u{0:x4}' -f [int][char]$m.Value })
"""


def executable_name(path: str) -> str:
    return path.replace("/", "\\").rsplit("\\", 1)[-1].lower()


def category_for(executable: str, family: str | None = None) -> str:
    """The access category of an app from its executable name (and package family)."""
    name = executable.lower()
    if name in BROWSERS:
        return "browser"
    if name in TERMINALS or (family and family.lower().startswith(_TERMINAL_PACKAGES)):
        return "terminal"
    if name in IDES:
        return "ide"
    return "other"


def strictest(first: str, second: str) -> str:
    """The category with the more restrictive tier; *first* on a tie."""
    return second if _TIER_RANK.get(second, 2) < _TIER_RANK.get(first, 2) else first


def identity_keys(path: str | None, family: str | None) -> frozenset[str]:
    keys: set[str] = set()
    if path:
        keys.add(path.lower())
        executable = executable_name(path)
        if executable not in GENERIC_HOSTS:
            keys.add(f"exe:{executable}")
    if family:
        keys.add(f"pkg:{family.lower()}")
    return frozenset(keys)


def is_vbot_executable(executable: str) -> bool:
    """vBot's installed executables: ``vBot.exe`` and its ``vBot.<role>.exe`` hosts."""
    return bool(_VBOT_EXECUTABLE.fullmatch(executable.lower()))


def hosted_process(frame_pid: int, children: Iterable[tuple[str, int]]) -> int:
    """The process of the UWP app an ApplicationFrameWindow hosts (its CoreWindow child)."""
    for class_name, pid in children:
        if class_name == _UWP_CORE and pid and pid != frame_pid:
            return pid
    return frame_pid


def explorer_app(windows_dir: str, category: str = "other") -> AppInfo:
    path = f"{windows_dir.rstrip(chr(92))}\\explorer.exe"
    keys = identity_keys(path, None) | {"exe:explorer.exe"}
    return AppInfo(FILE_EXPLORER, keys, category, running=True, launchable=True)


# Start menu


@dataclass(frozen=True)
class StartRow:
    """One Apps-folder entry as the shell lists it."""

    name: str
    app_id: str
    target: str = ""
    family: str = ""
    executable: str = ""  # a packaged app's manifest executable, when known


@dataclass(frozen=True)
class StartApp:
    name: str
    app_id: str
    keys: frozenset[str]
    category: str
    identity: str  # entries with one identity are one app; the best-ranked name wins
    rank: tuple[int, int, str]


def start_app(row: StartRow, windows_dir: str) -> StartApp | None:
    """Interpret one Apps-folder entry; ``None`` for documents, links and folders."""
    name, app_id, target = row.name.strip(), row.app_id.strip(), row.target.strip()
    lowered = target.lower()
    executable = executable_name(lowered) if re.match(r"[a-z]:\\", lowered) else ""
    if not name or not app_id:
        return None
    # Shortcuts with arguments get generated ids; the app's own entry names it best.
    generated = int(app_id.lower().startswith("microsoft.autogenerated."))
    rank = (generated, len(name), name.casefold())
    if app_id.lower() == EXPLORER_APP_ID or executable == "explorer.exe":
        # Every Explorer shortcut (folders, the Explorer entry itself) is File Explorer.
        keys = explorer_app(windows_dir).keys
        rank = (0, 0, "") if app_id.lower() == EXPLORER_APP_ID else (1, rank[1], rank[2])
        return StartApp(FILE_EXPLORER, app_id, keys, "other", "explorer", rank)
    if is_vbot_executable(executable):
        return StartApp(OWN_APP.name, app_id, OWN_APP.keys, "other", OWN_KEY, rank)
    if row.family:
        keys = identity_keys(None, row.family)
        category, identity = category_for(row.executable, row.family), app_id.lower()
    elif executable.endswith(".msc"):  # consoles run in the Management Console
        keys = identity_keys(f"{windows_dir}\\System32\\mmc.exe", None)
        category, identity = "other", lowered
    elif executable.endswith(".exe"):
        keys, category, identity = identity_keys(target, None), category_for(executable), lowered
    else:
        return None
    return StartApp(name, app_id, keys, category, identity, rank)


class StartIndex:
    """Deduplicated Start-menu apps with unique names, and lookups by identity key."""

    def __init__(self, rows: Sequence[StartRow] = (), windows_dir: str = "C:\\Windows") -> None:
        best: dict[str, StartApp] = {}
        for row in rows:
            app = start_app(row, windows_dir)
            if app is not None and (app.identity not in best or app.rank < best[app.identity].rank):
                best[app.identity] = app
        apps = sorted(best.values(), key=lambda app: (app.name.casefold(), app.app_id.lower()))
        seen: dict[str, int] = {}
        for index, app in enumerate(apps):
            count = seen[app.name.casefold()] = seen.get(app.name.casefold(), 0) + 1
            if count > 1:  # Windows' own convention for repeated Start names
                apps[index] = replace(app, name=f"{app.name} ({count})")
        self.apps: tuple[StartApp, ...] = tuple(apps)
        self._by_key: dict[str, list[StartApp]] = {}
        for app in self.apps:
            for key in app.keys:
                self._by_key.setdefault(key, []).append(app)

    def lookup(self, keys: frozenset[str]) -> StartApp | None:
        """The one Start app these keys identify: by path or package, else by unique exe."""
        for exact in (False, True):
            found = {
                app.identity: app
                for key in keys
                if key.startswith("exe:") == exact
                for app in self._by_key.get(key, ())
            }
            if len(found) == 1:
                return next(iter(found.values()))
            if found:
                return None
        return None


def merge_apps(start: Sequence[StartApp], running: Sequence[AppInfo]) -> list[AppInfo]:
    """Installed apps marked running where a running app matches them, plus the rest.

    A running app matches by identity key, else by name: apps the user sees under
    one name are one app, so a grant by that name covers all of their windows.
    """
    merged = [
        AppInfo(app.name, app.keys, app.category, False, launchable=OWN_KEY not in app.keys)
        for app in start
    ]
    for app in running:
        matches = [index for index, entry in enumerate(merged) if entry.matches(app)] or [
            index
            for index, entry in enumerate(merged)
            if entry.name.casefold() == app.name.casefold()
        ]
        if not matches:
            merged.append(app)
        for index in matches:
            entry = merged[index]
            category = strictest(entry.category, app.category)
            merged[index] = replace(
                entry, keys=entry.keys | app.keys, category=category, running=True
            )
    return sorted(merged, key=lambda app: app.name.casefold())


def discover_start_rows(timeout: float = _DISCOVERY_TIMEOUT) -> list[StartRow]:
    """List the shell's Apps folder (what Start shows) through one hidden PowerShell."""
    if sys.platform != "win32":
        return []
    windows_dir = _win32.windows_directory()
    powershell = Path(windows_dir) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    script = base64.b64encode(_DISCOVERY_SCRIPT.encode("utf-16-le")).decode("ascii")
    completed = subprocess.run(
        [str(powershell), "-NoProfile", "-NonInteractive", "-EncodedCommand", script],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=timeout,
        creationflags=subprocess.CREATE_NO_WINDOW,
        check=False,
    )
    entries = json.loads(completed.stdout.decode("ascii", "replace").strip() or "[]")
    rows: list[StartRow] = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        family = str(entry.get("f") or "")
        app_id = str(entry.get("a") or "")
        executable = ""
        if family and "!" in app_id:
            executable = package_executable(family, app_id.split("!", 1)[1]) or ""
        rows.append(
            StartRow(
                str(entry.get("n") or ""), app_id, str(entry.get("t") or ""), family, executable
            )
        )
    return rows


class StartApps:
    """The Start index, discovered in the background and refreshed at most once a period."""

    def __init__(
        self,
        discover: Callable[[], list[StartRow]] = discover_start_rows,
        period: float = _DISCOVERY_PERIOD,
        clock: Callable[[], float] = time.monotonic,
        windows_dir: str = "C:\\Windows",
    ) -> None:
        self._discover, self._period, self._clock = discover, period, clock
        self._windows_dir = windows_dir
        self._lock = threading.Lock()
        self._index: StartIndex | None = None
        self._loaded_at: float | None = None
        self._loading = False
        self._attempted = threading.Event()
        self._failing = False

    def index(self, wait: float = 0.0) -> StartIndex:
        """The latest index; starts a refresh when stale and waits only for a first one."""
        with self._lock:
            stale = self._loaded_at is None or self._clock() - self._loaded_at >= self._period
            if stale and not self._loading:
                self._loading = True
                threading.Thread(
                    target=self._load, name="computer-use-start-apps", daemon=True
                ).start()
            index = self._index
        if index is None and wait > 0:
            self._attempted.wait(wait)
            index = self._index
        return index or StartIndex()

    def _load(self) -> None:
        index: StartIndex | None = None
        try:
            index = StartIndex(self._discover(), self._windows_dir)
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            if not self._failing:
                _LOGGER.warning("Start menu app discovery failed (error=%s)", type(error).__name__)
            self._failing = True
        finally:
            with self._lock:
                self._loading = False
                self._loaded_at = self._clock()
                if index is not None:
                    self._index, self._failing = index, False
            self._attempted.set()


def launch(target: str) -> None:
    """Start an app through the Windows shell (an AppsFolder id or executable path).

    The shell starts it, so the app neither inherits vBot's environment nor ends
    with it; this call does not wait for the app.
    """
    if sys.platform != "win32":
        raise TargetError("Computer Use needs a Windows desktop.", "computer_use_unavailable")
    explorer = Path(_win32.windows_directory()) / "explorer.exe"
    try:
        process = subprocess.Popen(
            [str(explorer), target],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except OSError as error:
        raise TargetError(f"Windows could not start the app ({type(error).__name__}).") from error
    threading.Thread(target=process.wait, name="computer-use-launch", daemon=True).start()


# Processes and windows


@dataclass(frozen=True)
class ProcessFacts:
    pid: int
    path: str | None
    family: str | None
    integrity: int  # -1: gone; INTEGRITY_DENIED: its token cannot be read

    @property
    def executable(self) -> str:
        return executable_name(self.path) if self.path else ""


INTEGRITY_DENIED = 1 << 20


def process_facts(pid: int) -> ProcessFacts:
    bound = _win32.api()
    kernel32 = bound.kernel32
    process = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not process:
        denied = _win32.last_error() == 5  # ERROR_ACCESS_DENIED: elevated or protected
        return ProcessFacts(pid, None, None, INTEGRITY_DENIED if denied else -1)
    try:
        size = ct.c_uint32(32768)
        path = ct.create_unicode_buffer(size.value)
        found = kernel32.QueryFullProcessImageNameW(process, 0, path, ct.byref(size))
        length = ct.c_uint32(256)
        family = ct.create_unicode_buffer(length.value)
        packaged = kernel32.GetPackageFamilyName(process, ct.byref(length), family) == 0
        return ProcessFacts(
            pid,
            path.value if found else None,
            family.value if packaged and family.value else None,
            token_integrity(process),
        )
    finally:
        kernel32.CloseHandle(process)


def token_integrity(process: int) -> int:
    """Mandatory integrity level of a process token; UIPI blocks input to higher levels."""
    advapi32, kernel32 = _win32.api().advapi32, _win32.api().kernel32
    token = ct.c_void_p()
    if not advapi32.OpenProcessToken(process, 0x8, ct.byref(token)):  # TOKEN_QUERY
        return INTEGRITY_DENIED
    try:
        buffer = ct.create_string_buffer(128)
        needed = ct.c_uint32()
        if not advapi32.GetTokenInformation(token, 25, buffer, len(buffer), ct.byref(needed)):
            return INTEGRITY_DENIED  # 25: TokenIntegrityLevel
        sid = ct.c_void_p.from_buffer(buffer).value  # TOKEN_MANDATORY_LABEL.Label.Sid
        count = advapi32.GetSidSubAuthorityCount(sid).contents.value
        return int(advapi32.GetSidSubAuthority(sid, count - 1).contents.value)
    finally:
        kernel32.CloseHandle(token)


def file_description(path: str) -> str | None:
    """The FileDescription of an executable's version resource, if it has one."""
    version = _win32.api().version
    size = int(version.GetFileVersionInfoSizeW(path, None))
    data = ct.create_string_buffer(size)
    if not size or not version.GetFileVersionInfoW(path, 0, size, data):
        return None
    pointer, length = ct.c_void_p(), ct.c_uint()
    languages = ["040904b0", "040904e4", "000004b0"]
    if (
        version.VerQueryValueW(
            data, "\\VarFileInfo\\Translation", ct.byref(pointer), ct.byref(length)
        )
        and length.value >= 4
    ):
        words = ct.cast(pointer, ct.POINTER(ct.c_uint16))
        languages.insert(0, f"{words[0]:04x}{words[1]:04x}")
    for language in languages:
        key = f"\\StringFileInfo\\{language}\\FileDescription"
        if version.VerQueryValueW(data, key, ct.byref(pointer), ct.byref(length)) and length.value:
            text = ct.wstring_at(pointer.value or 0, length.value).rstrip("\0").strip()
            if text:
                return text
    return None


def package_executable(family: str, application: str) -> str | None:
    """The executable name a packaged app id declares in its package manifest."""
    if sys.platform != "win32":
        return None
    kernel32 = _win32.api().kernel32
    count, length = ct.c_uint32(), ct.c_uint32()
    kernel32.GetPackagesByPackageFamily(family, ct.byref(count), None, ct.byref(length), None)
    if not count.value or not length.value:
        return None
    names = (ct.c_wchar_p * count.value)()
    buffer = ct.create_unicode_buffer(length.value)
    if kernel32.GetPackagesByPackageFamily(
        family, ct.byref(count), names, ct.byref(length), buffer
    ):
        return None
    size = ct.c_uint32(1024)
    folder = ct.create_unicode_buffer(size.value)
    if kernel32.GetPackagePathByFullName(names[0], ct.byref(size), folder):
        return None
    try:
        root = ElementTree.parse(Path(folder.value) / "AppxManifest.xml").getroot()
    except OSError, ElementTree.ParseError:
        return None
    for element in root.iter():
        if (
            element.tag.rsplit("}", 1)[-1] == "Application"
            and element.get("Id", "").lower() == application.lower()
        ):
            return executable_name(element.get("Executable") or "") or None
    return None


class AppResolver:
    """Resolves windows to their apps for one enumeration, once per process."""

    def __init__(
        self, index: StartIndex, descriptions: dict[str, str | None], own_integrity: int
    ) -> None:
        self._index, self._descriptions = index, descriptions
        self._own_integrity = own_integrity
        self._windows_dir = _win32.windows_directory()
        self._facts: dict[int, ProcessFacts] = {}

    def facts(self, pid: int) -> ProcessFacts:
        if pid not in self._facts:
            self._facts[pid] = process_facts(pid)
        return self._facts[pid]

    def elevated(self, facts: ProcessFacts) -> bool:
        """Whether UIPI blocks input to the process (a higher integrity level than vBot)."""
        return facts.integrity >= 0 and facts.integrity > self._own_integrity

    def window(self, handle: int, depth: int = 0) -> tuple[AppInfo, ProcessFacts]:
        class_name = _win32.window_class(handle)
        pid = _win32.window_process(handle)[0]
        facts = self.facts(pid)
        if facts.executable == _UWP_HOST and class_name == _UWP_FRAME:
            children = [
                (_win32.window_class(child), _win32.window_process(child)[0])
                for child in _win32.child_windows(handle)
            ]
            facts = self.facts(hosted_process(pid, children))
        if facts.executable in HELPER_PROCESSES and depth < 4:
            owner = _win32.window_owner(handle)
            if owner:
                return self.window(owner, depth + 1)
        if facts.pid == os.getpid() or is_vbot_executable(facts.executable):
            return OWN_APP, facts
        if facts.executable in SHELL_HOSTS or (
            class_name in SHELL_CLASSES and facts.executable == "explorer.exe"
        ):
            return explorer_app(self._windows_dir, "shell"), facts
        if facts.executable == "explorer.exe":
            return explorer_app(self._windows_dir), facts
        return self.app(facts), facts

    def app(self, facts: ProcessFacts) -> AppInfo:
        keys = identity_keys(facts.path, facts.family)
        category = category_for(facts.executable, facts.family)
        start = self._index.lookup(keys) if keys else None
        if start is not None:
            return AppInfo(
                start.name, keys | start.keys, strictest(start.category, category), True, True
            )
        if not facts.path:
            return AppInfo("Unknown app", keys, category, running=True, launchable=False)
        if facts.path not in self._descriptions:
            self._descriptions[facts.path] = file_description(facts.path)
        name = self._descriptions[facts.path] or Path(facts.path).stem
        return AppInfo(name, keys, category, running=True, launchable=facts.family is None)
