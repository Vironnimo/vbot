"""Tests for the keep-awake power-request controller."""

from __future__ import annotations

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
def fake_power(monkeypatch: pytest.MonkeyPatch):
    """Stub the platform seams and record acquire/release calls."""

    state = {"supported": True, "acquire_result": 42, "release_result": True}
    calls: list[str] = []

    def fake_acquire() -> int | None:
        if not state["supported"]:
            return None
        calls.append("acquire")
        return state["acquire_result"]

    def fake_release(handle: int) -> bool:
        calls.append(f"release:{handle}")
        return bool(state["release_result"])

    monkeypatch.setattr(keep_awake, "_power_request_supported", lambda: state["supported"])
    monkeypatch.setattr(keep_awake, "_acquire", fake_acquire)
    monkeypatch.setattr(keep_awake, "_release", fake_release)
    return state, calls


def test_enable_acquires_and_disable_releases(fake_power) -> None:
    state, calls = fake_power
    controller = KeepAwakeController()

    controller.set_enabled(True)
    assert controller.active is True

    controller.set_enabled(False)
    assert controller.active is False
    assert calls == ["acquire", "release:42"]


def test_enable_is_idempotent(fake_power) -> None:
    _, calls = fake_power
    controller = KeepAwakeController()

    controller.set_enabled(True)
    controller.set_enabled(True)

    assert calls == ["acquire"]


def test_disable_without_enable_is_a_no_op(fake_power) -> None:
    _, calls = fake_power
    controller = KeepAwakeController()

    controller.set_enabled(False)
    controller.close()

    assert calls == []


def test_close_releases_after_enable(fake_power) -> None:
    state, calls = fake_power
    controller = KeepAwakeController()
    controller.set_enabled(True)

    state["release_result"] = False
    controller.close()

    assert controller.active is False
    assert calls == ["acquire", "release:42"]


def test_acquire_failure_on_supported_platform_warns(fake_power) -> None:
    state, _ = fake_power
    state["acquire_result"] = None
    logger = FakeLogger()
    controller = KeepAwakeController(logger)

    controller.set_enabled(True)

    assert controller.active is False
    assert logger.records == [
        ("warning", "Keep-awake requested but Windows refused the power request")
    ]


def test_unsupported_platform_is_silent_debug(fake_power) -> None:
    state, calls = fake_power
    state["supported"] = False
    logger = FakeLogger()
    controller = KeepAwakeController(logger)

    controller.set_enabled(True)

    assert controller.active is False
    assert calls == []
    assert logger.records == [
        (
            "debug",
            "Keep-awake requested but this platform has no power-request API",
        )
    ]


def test_release_failure_warns_but_deactivates(fake_power) -> None:
    state, _ = fake_power
    logger = FakeLogger()
    controller = KeepAwakeController(logger)
    controller.set_enabled(True)

    state["release_result"] = False
    controller.set_enabled(False)

    assert controller.active is False
    assert ("warning", "Keep-awake release was rejected by the platform") in logger.records
