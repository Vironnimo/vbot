"""Tests for validated native Desktop system actions."""

from __future__ import annotations

from typing import Any, cast

import pytest

from desktop.system_actions import DesktopSystemActions


def test_system_actions_delegate_valid_clipboard_and_browser_requests() -> None:
    copied: list[str] = []
    opened: list[str] = []

    def open_url(url: str) -> bool:
        opened.append(url)
        return True

    actions = DesktopSystemActions(
        clipboard_writer=copied.append,
        clipboard_reader=lambda: "paste me",
        external_url_opener=open_url,
    )

    actions.set_clipboard_text("copy me")
    assert actions.get_clipboard_text() == "paste me"
    actions.open_external_url("https://example.com/path?q=1")

    assert copied == ["copy me"]
    assert opened == ["https://example.com/path?q=1"]


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

    with pytest.raises(ValueError, match=r"absolute HTTP\(S\) URL"):
        actions.open_external_url(url)

    assert opened == []


def test_system_actions_reject_invalid_clipboard_and_browser_results() -> None:
    actions = DesktopSystemActions(
        clipboard_writer=lambda _text: None,
        clipboard_reader=lambda: cast(Any, None),
        external_url_opener=lambda _url: False,
    )

    with pytest.raises(ValueError, match="plain text"):
        actions.set_clipboard_text(None)
    with pytest.raises(ValueError, match="plain text"):
        actions.get_clipboard_text()
    with pytest.raises(RuntimeError, match="default browser"):
        actions.open_external_url("https://example.com")
