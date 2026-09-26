"""Settings changes refresh the complete live Runtime graph."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import Mock

import pytest

from core.runtime import Runtime
from core.utils.config import Config
from tests.core.runtime.runtime_extensions_test_support import (
    _CAPABILITY_EXT_SOURCE,
    _write_extension,
    _write_settings,
)
from tests.core.runtime.runtime_extensions_test_support import (
    _clean_extension_modules as _clean_extension_modules,
)


@pytest.mark.parametrize("enable", [False, True])
def test_extension_change_also_applies_recall_and_skill_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enable: bool
) -> None:
    config = Config(data_dir=tmp_path / "data")
    _write_extension(config.data_dir, "capabilities_ext", _CAPABILITY_EXT_SOURCE)
    _write_settings(
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
        skill = skill_root / "new-skill"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            "---\nname: new-skill\ndescription: Test newly configured directory.\n---\nBody.\n",
            encoding="utf-8",
        )
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = Runtime(Config(data_dir=tmp_path / "data"), safe_startup_mode="test")
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


def test_session_search_periods_follow_the_current_timezone_setting(tmp_path: Path) -> None:
    from tests.core.tools.session_search_helpers import make_context, success

    config = Config(data_dir=tmp_path / "data")
    _write_settings(config.data_dir, {"timezone": "Asia/Tokyo"})
    runtime = Runtime(config, safe_startup_mode="test")
    runtime.start()
    try:
        context = make_context(tmp_path, agent_id="main")
        arguments = {"query": "needle", "period": "2026-07-01T09:00/2026-07-01T10:00"}

        before = success(asyncio.run(runtime.tools.dispatch(context, dict(arguments))))
        runtime.storage.update_settings_sections({"server": {"timezone": "America/New_York"}})
        after = success(asyncio.run(runtime.tools.dispatch(context, dict(arguments))))

        assert before["period"] == (
            "2026-07-01T09:00:00+09:00/2026-07-01T10:00:00+09:00 (Asia/Tokyo)"
        )
        assert after["period"] == (
            "2026-07-01T09:00:00-04:00/2026-07-01T10:00:00-04:00 (America/New_York)"
        )
    finally:
        runtime.stop()
