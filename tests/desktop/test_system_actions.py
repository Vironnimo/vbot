"""Validation of native Desktop system actions; the bridge test covers delegation."""

from __future__ import annotations

import importlib.util
import types
import webbrowser
from pathlib import Path
from typing import Any, cast

import pytest

from desktop.system_actions import DesktopSystemActions, guard_page_windows


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "data:text/plain,unsafe",
        "file:///etc/passwd",
        "/relative/path",
        " https://example.com",
        "http://",
    ],
)
def test_open_external_url_rejects_non_web_targets(url: str) -> None:
    opened: list[str] = []

    def open_url(target: str) -> bool:
        opened.append(target)
        return True

    actions = DesktopSystemActions(external_url_opener=open_url)

    with pytest.raises(ValueError):
        actions.open_external_url(url)

    assert opened == []


def test_system_actions_reject_invalid_clipboard_and_browser_results() -> None:
    actions = DesktopSystemActions(
        clipboard_writer=lambda _text: None,
        clipboard_reader=lambda: cast(Any, None),
        external_url_opener=lambda _url: False,
    )

    with pytest.raises(ValueError):
        actions.set_clipboard_text(None)
    with pytest.raises(ValueError):
        actions.get_clipboard_text()
    with pytest.raises(RuntimeError):
        actions.open_external_url("https://example.com")


def test_a_page_opens_new_windows_only_as_web_links() -> None:
    backend = types.ModuleType("webview.platforms.fake")
    backend.webbrowser = webbrowser  # type: ignore[attr-defined]
    opened: list[str] = []

    def open_url(target: str) -> bool:
        opened.append(target)
        return True

    actions = DesktopSystemActions(external_url_opener=open_url)
    modules = {"webview.platforms.fake": backend, "webview.util": types.ModuleType("u")}

    assert guard_page_windows(actions, modules) == 1
    guard = backend.webbrowser  # type: ignore[attr-defined]
    assert guard.open("https://example.com/") is True
    assert guard.open("file:///C:/Windows/System32/calc.exe") is False
    assert guard.open("ms-settings:privacy") is False
    assert opened == ["https://example.com/"]
    # Guarding again keeps the guard.
    assert guard_page_windows(actions, modules) == 1
    assert backend.webbrowser is guard  # type: ignore[attr-defined]


def test_pywebview_opens_a_pages_new_window_with_its_backends_webbrowser() -> None:
    """What ``guard_page_windows`` relies on, in the installed pywebview."""
    pytest.importorskip("webview")
    spec = importlib.util.find_spec("webview.platforms.edgechromium")
    assert spec is not None and spec.origin is not None
    platforms = Path(spec.origin).parent
    edge = (platforms / "edgechromium.py").read_text(encoding="utf-8")
    handler = edge.split("def on_new_window_request", 1)[1].split("\n    def ", 1)[0]
    assert "webbrowser.open(" in handler
    for backend in platforms.glob("*.py"):
        source = backend.read_text(encoding="utf-8")
        if "webbrowser" in source:
            assert "\nimport webbrowser\n" in source, backend.name
            assert "from webbrowser" not in source, backend.name
