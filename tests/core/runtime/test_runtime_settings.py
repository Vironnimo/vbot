"""Settings changes refresh the complete live Runtime graph."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import Mock

import pytest

from core.runtime import Runtime
from core.utils.config import Config
from tests.core.runtime.runtime_test_support import (
    CAPABILITY_EXT_SOURCE,
    dispatch_tool,
    write_extension,
    write_settings,
    write_skill,
)


@pytest.mark.parametrize("enable", [False, True])
def test_extension_change_also_applies_recall_and_skill_changes(
    config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enable: bool
) -> None:
    write_extension(config.data_dir, "capabilities_ext", CAPABILITY_EXT_SOURCE)
    write_settings(
        config.data_dir,
        {
            "extensions": {"disabled": ["capabilities_ext"] if enable else []},
            "recall": {"backend": "sqlite_fts" if enable else "ext_recall"},
        },
    )
    runtime = Runtime(config)
    runtime.start()
    try:
        previous = runtime.storage.load_settings()
        skill_root = tmp_path / "new-skills"
        write_skill(skill_root, "new-skill", "Test newly configured directory.")
        runtime.storage.update_settings_sections(
            {
                "extensions": {"disabled": [] if enable else ["capabilities_ext"]},
                "recall": {"backend": "ext_recall" if enable else "sqlite_fts"},
                "skills": {"directories": [str(skill_root)]},
            }
        )
        recall_reload = Mock(wraps=runtime.reload_recall_backend)
        monkeypatch.setattr(runtime, "reload_recall_backend", recall_reload)

        commands_changed = asyncio.run(
            runtime.apply_settings_change(previous, runtime.storage.load_settings())
        )

        assert commands_changed is True
        assert (runtime.recall_backend.__class__.__name__ == "ExtBackend") is enable
        assert ("ext_echo" in {tool.name for tool in runtime.tools.list_tools()}) is enable
        assert "new-skill" in {skill.name for skill in runtime.skills_for(None).list_all()}
        # The full Extension reload uses its injected Recall callback; only
        # surgical disable needs the independent Settings-driven refresh.
        assert recall_reload.call_count == (0 if enable else 1)
    finally:
        runtime.stop()


def test_unchanged_settings_do_not_refresh_live_services(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = Runtime(config, safe_startup_mode="test")
    runtime.start()
    try:
        skills = runtime.skills_for(None)
        recall = runtime.recall_backend
        keep_awake = Mock()
        timezone = Mock()
        monkeypatch.setattr(runtime, "reload_keep_awake", keep_awake)
        monkeypatch.setattr(runtime, "reload_timezone", timezone)
        settings = runtime.storage.load_settings()
        assert asyncio.run(runtime.apply_settings_change(settings, settings)) is False
        assert runtime.skills_for(None) is skills
        assert runtime.recall_backend is recall
        keep_awake.assert_not_called()
        timezone.assert_not_called()
    finally:
        runtime.stop()


def test_session_search_periods_follow_the_current_timezone_setting(config: Config) -> None:
    write_settings(config.data_dir, {"timezone": "Asia/Tokyo"})
    runtime = Runtime(config, safe_startup_mode="test")
    runtime.start()
    try:
        arguments = {"query": "needle", "period": "2026-07-01T09:00/2026-07-01T10:00"}

        def search_period() -> str:
            result = dispatch_tool(
                runtime, "session_search", config.data_dir, dict(arguments), agent_id="main"
            )
            assert result["ok"] is True
            period = result["data"]["period"]
            assert isinstance(period, str)
            return period

        before = search_period()
        runtime.storage.update_settings_sections({"server": {"timezone": "America/New_York"}})
        after = search_period()

        assert before == "2026-07-01T09:00:00+09:00/2026-07-01T10:00:00+09:00 (Asia/Tokyo)"
        assert after == "2026-07-01T09:00:00-04:00/2026-07-01T10:00:00-04:00 (America/New_York)"
    finally:
        runtime.stop()
