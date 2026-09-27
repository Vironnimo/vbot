"""The keep-awake controller holds at most one system power request."""

from __future__ import annotations

from typing import Any

import pytest

from core.runtime import keep_awake
from core.runtime.keep_awake import KeepAwakeController


class FakeLogger:
    def __init__(self) -> None:
        self.records: list[tuple[str, str]] = []

    def debug(self, msg: str, *args: object) -> None:
        self.records.append(("debug", msg))

    def info(self, msg: str, *args: object) -> None:
        self.records.append(("info", msg))

    def warning(self, msg: str, *args: object) -> None:
        self.records.append(("warning", msg))


@pytest.fixture()
def fake_power(monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, Any], list[str]]:
    """Stub the platform seams and record acquire/release calls."""
    state: dict[str, Any] = {"supported": True, "acquire_result": 42, "release_result": True}
    calls: list[str] = []

    def fake_acquire() -> int | None:
        if not state["supported"]:
            return None
        calls.append("acquire")
        result: int | None = state["acquire_result"]
        return result

    def fake_release(handle: int) -> bool:
        calls.append(f"release:{handle}")
        return bool(state["release_result"])

    monkeypatch.setattr(keep_awake, "_power_request_supported", lambda: state["supported"])
    monkeypatch.setattr(keep_awake, "_acquire", fake_acquire)
    monkeypatch.setattr(keep_awake, "_release", fake_release)
    return state, calls


def test_enabling_holds_one_power_request_until_disabled_or_closed(fake_power) -> None:
    _, calls = fake_power
    controller = KeepAwakeController()
    # Disabling or closing before any request is a no-op.
    controller.set_enabled(False)
    controller.close()
    assert calls == []

    controller.set_enabled(True)
    controller.set_enabled(True)
    assert controller.active is True
    controller.set_enabled(False)
    assert controller.active is False
    assert calls == ["acquire", "release:42"]

    controller.set_enabled(True)
    controller.close()
    controller.close()
    assert controller.active is False
    assert calls == ["acquire", "release:42", "acquire", "release:42"]


def test_rejected_release_still_deactivates_with_a_warning(fake_power) -> None:
    state, calls = fake_power
    logger = FakeLogger()
    controller = KeepAwakeController(logger)
    controller.set_enabled(True)

    state["release_result"] = False
    controller.set_enabled(False)

    assert controller.active is False
    assert calls == ["acquire", "release:42"]
    assert ("warning", "Keep-awake release was rejected by the platform") in logger.records


@pytest.mark.parametrize(
    ("supported", "expected"),
    [
        (True, ("warning", "Keep-awake requested but Windows refused the power request")),
        (False, ("debug", "Keep-awake requested but this platform has no power-request API")),
    ],
)
def test_unavailable_power_request_leaves_keep_awake_inactive(
    fake_power, supported: bool, expected: tuple[str, str]
) -> None:
    state, _ = fake_power
    state["supported"] = supported
    state["acquire_result"] = None
    logger = FakeLogger()
    controller = KeepAwakeController(logger)

    controller.set_enabled(True)

    assert controller.active is False
    assert logger.records == [expected]
