"""Change pacing: each Swarm change reaches clients at once, then at most once a second."""

from types import SimpleNamespace

import pytest

from resources.extensions.swarm import _change_pacing
from resources.extensions.swarm._change_pacing import ChangePacing
from tests.resources.extensions.swarm.swarm_test_support import QuietTimers


def test_changes_within_a_second_are_published_once_when_it_ends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = QuietTimers()
    monkeypatch.setattr(_change_pacing, "asyncio", SimpleNamespace(get_running_loop=lambda: loop))
    published: list[tuple[str, str, int]] = []
    pacing = ChangePacing(lambda *change: published.append(change))

    pacing.changed("participants", "swarm")
    pacing.changed("participants", "swarm", 3)
    pacing.changed("participants", "swarm", 2)
    # Another resource or id has its own window.
    pacing.changed("swarms", "swarm", 1)
    pacing.changed("participants", "other")
    assert published == [
        ("participants", "swarm", 0),
        ("swarms", "swarm", 1),
        ("participants", "other", 0),
    ]
    assert [timer.delay for timer in loop.timers] == [_change_pacing.INTERVAL_SECONDS] * 3

    # The window's changes are published once, with their highest revision, and
    # open the next window; a window without changes publishes nothing.
    loop.timers[0].fire()
    loop.timers[1].fire()
    assert published[3:] == [("participants", "swarm", 3)]
    pacing.changed("participants", "swarm", 4)
    pacing.changed("swarms", "swarm", 5)
    assert published[4:] == [("swarms", "swarm", 5)]
    loop.timers[3].fire()
    assert published[5:] == [("participants", "swarm", 4)]

    # Closing drops what is unpublished: the retiring registration refreshes every page.
    pacing.changed("participants", "other", 6)
    pacing.close()
    assert [timer.cancelled for timer in loop.timers] == [False, False, True, False, True, True]
    loop.timers[2].fire()
    assert published[6:] == []
