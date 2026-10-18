"""Screen/work-area doubles shared by window geometry and launch assembly tests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class FakeFrame:
    """Minimal work-area rectangle double (Windows PascalCase convention)."""

    X: int = 0
    Y: int = 0
    Width: int = 1600
    Height: int = 1000


@dataclass
class FakeScreen:
    """pywebview Screen double with origin, scale, and optional work area."""

    width: int = 1600
    height: int = 1000
    x: int = 0
    y: int = 0
    scale: float = 1.0
    frame: Any = None

    def __post_init__(self) -> None:
        if self.frame is None:
            self.frame = FakeFrame(Width=self.width, Height=self.height)
