"""The private Python environment, the browser and the fonts the PDF tools use."""

from __future__ import annotations

import glob
import importlib.util
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

from .common import SCRIPT, CommandError, tool_command

PACKAGES = ["pypdf>=5.1", "pypdfium2>=4.30", "pillow>=10.1", "reportlab>=4.2", "cryptography>=42"]
MODULES = {"pypdf": "pypdf", "pypdfium2": "pypdfium2", "PIL": "pillow"}
MODULES.update({"reportlab": "reportlab", "cryptography": "cryptography"})
REEXEC_MARKER = "VBOT_PDF_TOOLS_REEXEC"
SAMPLE_CHARACTERS = {"emoji": "\U0001f600", "Chinese, Japanese, Korean": "中"}


def environment_root() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "vbot" / "pdf-tools" / "env"


def environment_python() -> Path:
    root = environment_root()
    if os.name == "nt":
        return root / "Scripts" / "python.exe"
    return root / "bin" / "python"


def use_private_environment(script: Path) -> None:
    """Run this command again inside the private environment when one exists."""
    python = environment_python()
    if os.environ.get(REEXEC_MARKER) or not python.is_file():
        return
    try:
        if Path(sys.prefix).resolve() == environment_root().resolve():
            return
    except OSError:
        return
    environment = dict(os.environ)
    environment[REEXEC_MARKER] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    completed = subprocess.run([str(python), str(script), *sys.argv[1:]], env=environment)
    raise SystemExit(completed.returncode)


# Setup report and installation


def setup(install: bool, install_browser: bool) -> list[str]:
    if install or install_browser:
        _install_packages()
        if install_browser:
            _install_playwright_browser()
        python = environment_python()
        completed = subprocess.run(
            [str(python), tool_script(), "setup"],
            env=dict(os.environ, **{REEXEC_MARKER: "1", "PYTHONIOENCODING": "utf-8"}),
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        report = (completed.stdout + completed.stderr).strip().splitlines()
        return ["Installed the Python packages into " + str(environment_root()), *report]
    return _report()


def tool_script() -> str:
    return str(SCRIPT)


def _report() -> list[str]:
    missing = [package for module, package in MODULES.items() if not _has(module)]
    browser = find_browser(None)
    lines: list[str] = []
    ready = not missing and browser.path is not None
    if ready:
        lines.append("PDF tools ready.")
    elif missing:
        lines.append("PDF tools not ready: Python packages are missing.")
    else:
        lines.append("PDF tools ready except HTML documents: no usable browser.")
    location = "private environment" if _in_private_environment() else "system Python"
    lines.append(f"Python: {sys.executable} ({platform.python_version()}, {location})")
    installed = [
        f"{package} {_version(module)}" for module, package in MODULES.items() if _has(module)
    ]
    if installed:
        lines.append("Packages: " + ", ".join(installed))
    if missing:
        lines.append(
            f"Missing packages: {', '.join(missing)}. Run `{tool_command('setup --install')}`; "
            "it installs them into a private environment and changes nothing else."
        )
    if browser.path:
        lines.append(f"Browser for HTML documents: {browser.path}")
    else:
        lines.append("Browser for HTML documents: none found. " + browser.problem)
        lines.extend(browser_install_advice())
    fonts = font_coverage_report()
    if fonts:
        lines.append(fonts)
    return lines


def _in_private_environment() -> bool:
    try:
        return Path(sys.prefix).resolve() == environment_root().resolve()
    except OSError:
        return False


def _has(module: str) -> bool:
    return importlib.util.find_spec(module) is not None


def _version(module: str) -> str:
    try:
        from importlib.metadata import version

        return version(MODULES[module])
    except Exception:
        return "?"


def _install_packages() -> None:
    root = environment_root()
    python = environment_python()
    if not python.is_file():
        root.parent.mkdir(parents=True, exist_ok=True)
        created = subprocess.run(
            [sys.executable, "-m", "venv", str(root)], capture_output=True, text=True
        )
        if (created.returncode != 0 or not python.is_file()) and not _create_with_uv(root):
            detail = (created.stderr or created.stdout).strip().splitlines()
            reason = detail[-1] if detail else f"exit code {created.returncode}"
            shutil.rmtree(root, ignore_errors=True)
            raise CommandError(
                "Could not create the private Python environment: "
                f"{reason}. On Debian, Ubuntu and Raspberry Pi OS, ask the user to run "
                "`sudo apt install python3-venv`, then repeat "
                f"`{tool_command('setup --install')}`."
            )
    installed = subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--upgrade",
            "--quiet",
            *PACKAGES,
        ],
        capture_output=True,
        text=True,
    )
    if installed.returncode != 0:
        detail = (installed.stderr or installed.stdout).strip().splitlines()
        raise CommandError(
            "Installing the Python packages failed: "
            + (" | ".join(detail[-3:]) if detail else f"exit code {installed.returncode}")
        )


def _create_with_uv(root: Path) -> bool:
    uv = shutil.which("uv")
    if uv is None:
        return False
    created = subprocess.run(
        [uv, "venv", "--seed", "--python", sys.executable, str(root)],
        capture_output=True,
        text=True,
    )
    return created.returncode == 0 and environment_python().is_file()


def _install_playwright_browser() -> None:
    python = str(environment_python())
    steps = [
        [python, "-m", "pip", "install", "--disable-pip-version-check", "--quiet", "playwright"],
        [python, "-m", "playwright", "install", "--only-shell", "--no-progress", "chromium"],
    ]
    for step in steps:
        completed = subprocess.run(step, capture_output=True, text=True)
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip().splitlines()
            raise CommandError(
                "Installing the browser failed: "
                + (" | ".join(detail[-3:]) if detail else f"exit code {completed.returncode}")
            )


# Browser discovery


class Browser:
    def __init__(self, path: str | None, problem: str = "") -> None:
        self.path = path
        self.problem = problem


def find_browser(explicit: str | None) -> Browser:
    if explicit:
        path = shutil.which(explicit) or explicit
        if not Path(path).is_file():
            return Browser(None, f"--browser {explicit} is not an executable file.")
        problem = _missing_libraries(path)
        return Browser(None, problem) if problem else Browser(path)
    problems: list[str] = []
    for candidate in _browser_candidates():
        problem = _missing_libraries(candidate)
        if problem:
            problems.append(problem)
            continue
        return Browser(candidate)
    return Browser(None, " ".join(problems))


def _browser_candidates() -> list[str]:
    found: list[str] = []

    def add(path: str | None) -> None:
        if path and Path(path).is_file() and path not in found:
            found.append(path)

    if os.name == "nt":
        roots = [
            os.environ.get(name) for name in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA")
        ]
        for root in filter(None, roots):
            add(str(Path(root) / "Microsoft" / "Edge" / "Application" / "msedge.exe"))
            add(str(Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe"))
            add(str(Path(root) / "Chromium" / "Application" / "chrome.exe"))
        for name in ("msedge", "chrome", "chromium"):
            add(shutil.which(name))
    elif sys.platform == "darwin":
        for app in ("Google Chrome", "Microsoft Edge", "Chromium", "Brave Browser"):
            add(f"/Applications/{app}.app/Contents/MacOS/{app}")
    else:
        names = [
            "chromium",
            "chromium-browser",
            "google-chrome-stable",
            "google-chrome",
            "microsoft-edge-stable",
            "microsoft-edge",
            "brave-browser",
        ]
        system = [shutil.which(name) for name in names]
        for path in system:
            if path and not os.path.realpath(path).startswith("/snap/"):
                add(path)
    for path in _playwright_browsers():
        add(path)
    if os.name != "nt" and sys.platform != "darwin":
        for name in ("chromium", "chromium-browser"):
            add(shutil.which(name))
    return found


def _playwright_browsers() -> list[str]:
    roots: list[Path] = []
    if os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        roots.append(Path(os.environ["PLAYWRIGHT_BROWSERS_PATH"]))
    if os.name == "nt":
        roots.append(Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "ms-playwright")
    elif sys.platform == "darwin":
        roots.append(Path.home() / "Library" / "Caches" / "ms-playwright")
    else:
        roots.append(
            Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "ms-playwright"
        )
    patterns = [
        "chromium_headless_shell-*/*/chrome-headless-shell",
        "chromium_headless_shell-*/*/chrome-headless-shell.exe",
        "chromium_headless_shell-*/*/headless_shell",
        "chromium_headless_shell-*/*/headless_shell.exe",
        "chromium-*/*/chrome",
        "chromium-*/*/chrome.exe",
    ]
    found: list[str] = []
    for root in roots:
        for pattern in patterns:
            found.extend(glob.glob(str(root / pattern)))
    return sorted(found, key=_revision, reverse=True)


def _revision(path: str) -> int:
    match = re.search(r"-(\d+)[\\/]", path)
    return int(match.group(1)) if match else 0


def _missing_libraries(path: str) -> str:
    """On Linux, name the system libraries a browser binary cannot load."""
    if not sys.platform.startswith("linux") or shutil.which("ldd") is None:
        return ""
    try:
        completed = subprocess.run(["ldd", path], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return ""
    missing = sorted(set(re.findall(r"^\s*(\S+) => not found", completed.stdout, re.MULTILINE)))
    if not missing:
        return ""
    problem = f"{path} cannot start because system libraries are missing ({', '.join(missing)})."
    playwright = environment_root() / ("Scripts" if os.name == "nt" else "bin") / "playwright"
    if playwright.is_file():
        return (
            f"{problem} Ask the user to run `sudo {environment_python()} -m playwright "
            "install-deps chromium` or to install those libraries with the system package manager."
        )
    return f"{problem} Ask the user to install those libraries with the system package manager."


def browser_install_advice() -> list[str]:
    if os.name == "nt":
        return [
            "Install Microsoft Edge or Google Chrome (ask the user), for example "
            "`winget install Microsoft.Edge`."
        ]
    if sys.platform == "darwin":
        return ["Install Google Chrome (ask the user): `brew install --cask google-chrome`."]
    distribution = _linux_distribution()
    without_root = (
        f"Without administrator rights: `{tool_command('setup --install-browser')}` downloads "
        "Chromium for the PDF tools only (about 300 MB); it can still need system libraries."
    )
    if distribution in ("debian", "raspbian"):
        return [
            "Debian and Raspberry Pi OS: ask the user to run `sudo apt install chromium`. "
            + without_root
        ]
    if distribution in ("ubuntu", "linuxmint", "pop"):
        return [
            f"{without_root} Alternative: ask the user to run `sudo snap install chromium`; that "
            "browser reads and writes only non-hidden folders in the home folder."
        ]
    if distribution in ("fedora", "rhel", "centos"):
        return [f"Ask the user to run `sudo dnf install chromium`. {without_root}"]
    if distribution in ("arch", "manjaro"):
        return [f"Ask the user to run `sudo pacman -S chromium`. {without_root}"]
    return [without_root]


def _linux_distribution() -> str:
    try:
        text = Path("/etc/os-release").read_text(encoding="utf-8")
    except OSError:
        return ""
    match = re.search(r"^ID=\"?([\w.-]+)", text, re.MULTILINE)
    return match.group(1).lower() if match else ""


# Fonts


def installed_font_coverage() -> set[int] | None:
    """Code points some installed font covers (Linux fontconfig); None when unknown."""
    if os.name == "nt" or sys.platform == "darwin" or shutil.which("fc-list") is None:
        return None
    try:
        completed = subprocess.run(
            ["fc-list", "--format", "%{charset}\\n"], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        return None
    covered: set[int] = set()
    for line in completed.stdout.splitlines():
        for part in line.split():
            start, _, end = part.partition("-")
            try:
                first = int(start, 16)
                last = int(end, 16) if end else first
            except ValueError:
                continue
            covered.update(range(first, last + 1))
    return covered


def font_coverage_report() -> str:
    covered = installed_font_coverage()
    if covered is None:
        return ""
    absent = [name for name, sample in SAMPLE_CHARACTERS.items() if ord(sample) not in covered]
    if not absent:
        return "Fonts: emoji and Chinese, Japanese, Korean characters are covered."
    packages = {"emoji": "fonts-noto-color-emoji", "Chinese, Japanese, Korean": "fonts-noto-cjk"}
    names = " ".join(packages[name] for name in absent)
    return (
        f"Fonts: no installed font has {' or '.join(absent)} characters; they print as empty "
        f"boxes. If a document needs them, ask the user to install fonts, on Debian, Ubuntu and "
        f"Raspberry Pi OS: `sudo apt install {names}`."
    )


def uncovered_characters(text: str) -> list[tuple[str, str]]:
    """Characters of `text` that no installed font covers, with a font package hint."""
    covered = installed_font_coverage()
    if covered is None:
        return []
    result: dict[str, str] = {}
    for character in text:
        code = ord(character)
        if code < 0x80 or character.isspace() or code in covered or character in result:
            continue
        if 0x200B <= code <= 0x200F or code in (0xFE0F, 0x2060):
            continue
        result[character] = _font_package(code)
    return list(result.items())


def _font_package(code: int) -> str:
    if 0x1F000 <= code <= 0x1FAFF or 0x2600 <= code <= 0x27BF:
        return "fonts-noto-color-emoji"
    if 0x2E80 <= code <= 0x9FFF or 0xAC00 <= code <= 0xD7AF or 0xF900 <= code <= 0xFAFF:
        return "fonts-noto-cjk"
    return "fonts-noto-core"
