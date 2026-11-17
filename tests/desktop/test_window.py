"""Window geometry at the native window owner's interface."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest

from desktop import window as desktop_window
from desktop.connection import ConnectionController
from tests.desktop.window_test_support import FakeFrame, FakeScreen


@pytest.mark.parametrize(
    ("saved_size", "screen", "size"),
    [
        (None, FakeScreen(1920, 1080), (1440, 864)),
        ((1380, 900), FakeScreen(1920, 1080), (1380, 900)),
        ((1800, 1100), FakeScreen(1280, 720), (1280, 720)),
        (
            (2048, 1152),
            FakeScreen(2048, 1152, frame=FakeFrame(Width=2048, Height=1104)),
            (2048, 1104),
        ),
        (None, None, (1280, 800)),
    ],
    ids=["first-run", "remembered-fits", "smaller-screen", "taskbar-work-area", "no-screen"],
)
def test_resolve_window_layout_bounds_the_size_to_the_primary_work_area(
    saved_size: tuple[int, int] | None, screen: FakeScreen | None, size: tuple[int, int]
) -> None:
    layout = desktop_window.resolve_window_layout(saved_size, screen)

    assert (layout.width, layout.height) == size
    assert (layout.minimum_width, layout.minimum_height) == (800, 600)
    assert layout.screen is screen


@pytest.mark.parametrize(
    ("page_url", "expected"),
    [
        ("http://nas.lan/#settings/desktop", "#settings/desktop"),
        ("http://nas.lan:8080/#settings/desktop", None),
    ],
)
def test_restart_restores_the_page_location_for_the_active_origin(
    page_url: str, expected: str | None
) -> None:
    controller = cast(
        ConnectionController, SimpleNamespace(active_server_url=lambda: "http://NAS.lan:80/")
    )
    window = SimpleNamespace(get_current_url=lambda: page_url)

    assert desktop_window.page_location([window], controller) == expected
