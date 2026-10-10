"""Placing dictated text: paste into the original window, otherwise leave it in the clipboard."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import pytest

from desktop.dictation.insertion import (
    INSERT_CLIPBOARD,
    INSERT_FAILED,
    INSERT_PASTED,
    MODIFIER_RELEASE_TIMEOUT_SECONDS,
    ClipboardTextInserter,
)

TARGET = 7


@dataclass
class FakeHost:
    """Win32 host double: one clipboard, one focused window, scripted key state."""

    clipboard: str = "previous"
    foreground: int = TARGET
    reachable: bool = True
    modifier_polls: int = 0
    paste_accepted: bool = True
    sequence: int = 1
    clipboard_busy: bool = False
    pastes: int = 0
    events: list[str] = field(default_factory=list)

    def foreground_window(self) -> int:
        return self.foreground

    def can_send_input_to(self, window: int) -> bool:
        return self.reachable

    def modifier_held(self) -> bool:
        if self.modifier_polls > 0:
            self.modifier_polls -= 1
            return True
        return False

    def send_paste(self) -> bool:
        self.pastes += 1
        self.events.append(f"paste:{self.clipboard}")
        return self.paste_accepted

    def clipboard_sequence(self) -> int:
        return self.sequence

    def read_clipboard_text(self) -> str:
        return self.clipboard

    def write_clipboard_text(self, text: str) -> None:
        if self.clipboard_busy:
            raise OSError("clipboard busy")
        self.clipboard = text
        self.sequence += 1


class Scheduler:
    def __init__(self) -> None:
        self.pending: list[tuple[float, Callable[[], None]]] = []

    def __call__(self, delay: float, action: Callable[[], None]) -> None:
        self.pending.append((delay, action))

    def run(self) -> None:
        for _delay, action in self.pending:
            action()


class Clock:
    def __init__(self, on_wait: Callable[[], None] | None = None) -> None:
        self.now = 0.0
        self.on_wait = on_wait

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        if self.on_wait is not None:
            action, self.on_wait = self.on_wait, None
            action()


def _inserter(
    host: FakeHost, scheduler: Scheduler, *, on_wait: Callable[[], None] | None = None
) -> ClipboardTextInserter:
    clock = Clock(on_wait)
    return ClipboardTextInserter(host, clock=clock, sleep=clock.sleep, schedule=scheduler)


@pytest.mark.parametrize("copied_at", ["never", "while-waiting", "after-paste"])
def test_a_paste_restores_the_previous_text_unless_something_new_was_copied(
    copied_at: str,
) -> None:
    host, scheduler = FakeHost(modifier_polls=3), Scheduler()

    def copy_during_wait() -> None:
        if copied_at == "while-waiting":
            host.write_clipboard_text("copied by the user")

    assert (
        _inserter(host, scheduler, on_wait=copy_during_wait).insert("Hallo Welt", TARGET)
        == INSERT_PASTED
    )
    assert host.events == ["paste:Hallo Welt"]
    if copied_at == "after-paste":
        host.write_clipboard_text("copied by the user")
    scheduler.run()

    assert host.clipboard == ("previous" if copied_at == "never" else "copied by the user")


@pytest.mark.parametrize(
    ("setup", "during_wait"),
    [
        (lambda host: setattr(host, "foreground", 9), False),
        (lambda host: setattr(host, "foreground", 0), False),
        (lambda host: setattr(host, "reachable", False), False),
        (lambda host: setattr(host, "paste_accepted", False), False),
        (
            lambda host: setattr(
                host, "modifier_polls", round(MODIFIER_RELEASE_TIMEOUT_SECONDS / 0.02) + 5
            ),
            False,
        ),
        (lambda host: setattr(host, "foreground", 9), True),
        (lambda host: setattr(host, "foreground", 0), True),
    ],
    ids=[
        "focus-moved",
        "no-focus",
        "elevated",
        "paste-refused",
        "modifier-held",
        "focus-moved-during-wait",
        "focus-lost-during-wait",
    ],
)
def test_without_a_safe_paste_the_text_stays_in_the_clipboard(
    setup: Callable[[FakeHost], None],
    during_wait: bool,
) -> None:
    host, scheduler = FakeHost(), Scheduler()
    if during_wait:
        host.modifier_polls = 1
    else:
        setup(host)

    outcome = _inserter(
        host, scheduler, on_wait=(lambda: setup(host)) if during_wait else None
    ).insert("Hallo Welt", TARGET)

    assert outcome == INSERT_CLIPBOARD
    assert host.clipboard == "Hallo Welt"
    assert scheduler.pending == []
    assert host.pastes <= 1  # only a refused paste was attempted
    if during_wait:
        assert host.pastes == 0


def test_a_clipboard_that_cannot_be_written_fails_the_insert() -> None:
    host = FakeHost(clipboard_busy=True)

    assert _inserter(host, Scheduler()).insert("Hallo", TARGET) == INSERT_FAILED
    assert host.pastes == 0
