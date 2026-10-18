"""Desktop daily log formatting and bounded crash-log generations."""

from __future__ import annotations

import logging
import os
import types
from pathlib import Path
from typing import Any

import pytest

from desktop import _logging as desktop_logging


def test_desktop_logging_writes_structured_daily_file(tmp_path: Path) -> None:
    handler = desktop_logging.configure_desktop_logging(tmp_path)
    assert handler is not None
    try:
        logging.getLogger("vbot.desktop.wakeword.controller").warning(
            "Voice state: error (speech_to_text_unconfigured)"
        )
        handler.flush()

        log_files = list((tmp_path / "logs").glob("*.log"))
        assert len(log_files) == 1
        content = log_files[0].read_text(encoding="utf-8")
        assert "[WARN] vbot.desktop.wakeword.controller" in content
        assert "error (speech_to_text_unconfigured)" in content
    finally:
        desktop_logging.close_desktop_logging(handler)


def test_desktop_crash_log_keeps_earlier_crashes_in_bounded_generations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    enabled: list[dict[str, Any]] = []

    def enable(*, file: Any, **options: Any) -> None:
        enabled.append({"path": Path(file.name).resolve(), **options})
        # The real fault handler would keep it open and take over the test process's own.
        file.close()

    monkeypatch.setattr(desktop_logging, "faulthandler", types.SimpleNamespace(enable=enable))
    monkeypatch.setattr(desktop_logging, "_DESKTOP_CRASH_LOG_ROTATE_BYTES", 64)
    crash_log = tmp_path / "logs" / desktop_logging.DESKTOP_CRASH_LOG_NAME
    crash_log.parent.mkdir()
    crash_log.write_bytes(b"Windows fatal exception: access violation\n")

    desktop_logging.enable_desktop_crash_log(tmp_path)
    # A start after a crash keeps what the crashed process wrote, then names itself
    # in one line of the log format, which the dump of a later crash follows.
    first = crash_log.read_text(encoding="utf-8")
    crashed, started = first.splitlines()
    assert crashed == "Windows fatal exception: access violation"
    assert "[INFO] vbot.desktop - " in started and f"pid={os.getpid()}" in started

    desktop_logging.enable_desktop_crash_log(tmp_path)
    # An oversized crash log moves to its single previous generation.
    assert crash_log.with_name(f"{crash_log.name}.1").read_text(encoding="utf-8") == first
    assert len(crash_log.read_text(encoding="utf-8").splitlines()) == 1
    # Every thread's Python stack, plus the C stack where the platform provides one.
    assert enabled == [{"path": crash_log.resolve(), "all_threads": True, "c_stack": True}] * 2
