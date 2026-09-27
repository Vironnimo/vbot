"""Validation of native Desktop system actions; the bridge test covers delegation."""

from __future__ import annotations

from typing import Any, cast

import pytest

from desktop.system_actions import DesktopSystemActions


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
