"""Settings changes refresh the complete live Runtime graph."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from core.runtime import Runtime, SettingsChangeEffects
from core.sessions import ArchiveEntryFilter
from core.utils.config import Config
from tests.core.runtime.runtime_test_support import (
    CAPABILITY_EXT_SOURCE,
    dispatch_tool,
    write_extension,
    write_settings,
    write_skill,
)


def test_extension_change_also_applies_recall_and_skill_changes(
    config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_extension(config.data_dir, "capabilities_ext", CAPABILITY_EXT_SOURCE)
    write_settings(
        config.data_dir,
        {"extensions": {"disabled": []}, "recall": {"backend": "ext_recall"}},
    )
    runtime = Runtime(config)
    runtime.start()
    try:
        recall_reload = Mock(wraps=runtime.reload_recall_backend)
        monkeypatch.setattr(runtime, "reload_recall_backend", recall_reload)

        def change(*, enable: bool) -> None:
            """Disable (surgical path) or re-enable (full reload) the Extension."""
            previous = runtime.storage.load_settings()
            skill_root = tmp_path / f"skills-enable-{enable}"
            write_skill(skill_root, f"new-skill-{enable}", "Test newly configured directory.")
            runtime.storage.update_settings_sections(
                {
                    "extensions": {"disabled": [] if enable else ["capabilities_ext"]},
                    "recall": {"backend": "ext_recall" if enable else "sqlite_fts"},
                    "skills": {"directories": [str(skill_root)]},
                }
            )
            recall_reload.reset_mock()

            effects = asyncio.run(
                runtime.apply_settings_change(previous, runtime.storage.load_settings())
            )

            assert effects == SettingsChangeEffects(commands_changed=True, skills_changed=True)
            assert (runtime.recall_backend.__class__.__name__ == "ExtBackend") is enable
            assert ("ext_echo" in {tool.name for tool in runtime.tools.list_tools()}) is enable
            skills = {skill.name for skill in runtime.skills_for(None).list_all()}
            assert f"new-skill-{enable}" in skills
            # The full Extension reload uses its injected Recall callback; only
            # surgical disable needs the independent Settings-driven refresh.
            assert recall_reload.call_count == (0 if enable else 1)

        change(enable=False)
        change(enable=True)
    finally:
        runtime.stop()


_NO_EFFECTS: dict[str, Any] = {
    "extension_reloads": 0,
    "disabled_changes": [],
    "skills_reloads": 0,
    "recall_reloads": 0,
    "keep_awake_reloads": 0,
    "timezone_reloads": 0,
    "speech_preloads": [],
    "embedding_binding_changes": 0,
    "retention_changes": 0,
    "commands_changed": False,
    "skills_changed": False,
}
_SKILLS: dict[str, Any] = {"skill_directories": ["~/extra-skills"]}
_RECALL: dict[str, Any] = {"recall": {"backend": "sqlite_fts"}}


def _extensions(*disabled: str, config: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"extensions": {"disabled": list(disabled), "config": config or {}}}


@pytest.mark.parametrize(
    ("previous", "current", "refresh_sections", "expected"),
    [
        pytest.param({}, {}, (), {}, id="unchanged"),
        pytest.param({}, {"web_search": {"provider": "searxng"}}, (), {}, id="unrelated"),
        pytest.param(
            {},
            {"extension_directories": ["~/extra-extensions"]},
            (),
            {"extension_reloads": 1, "commands_changed": True, "skills_changed": True},
            id="extension-directories",
        ),
        pytest.param(
            {},
            _SKILLS,
            (),
            {"skills_reloads": 1, "skills_changed": True},
            id="skill-directories",
        ),
        # An explicit section save refreshes even when its values are unchanged.
        pytest.param(
            {}, {}, ("skills",), {"skills_reloads": 1, "skills_changed": True}, id="skills-resave"
        ),
        pytest.param({}, _RECALL, (), {"recall_reloads": 1}, id="recall"),
        pytest.param({}, {}, ("recall",), {"recall_reloads": 1}, id="recall-resave"),
        # Disabling takes the surgical path, which refreshes Skills itself but
        # not an independently changed Recall selection.
        pytest.param(
            {},
            _extensions("one"),
            (),
            {"disabled_changes": [{"one"}], "commands_changed": True, "skills_changed": True},
            id="disable",
        ),
        pytest.param(
            {},
            {**_extensions("one"), **_SKILLS, **_RECALL},
            (),
            {
                "disabled_changes": [{"one"}],
                "recall_reloads": 1,
                "commands_changed": True,
                "skills_changed": True,
            },
            id="disable-with-skills-and-recall",
        ),
        # Enabling any name rebuilds the whole Extension layer, which applies
        # the complete disabled set and refreshes Skills and Recall itself.
        pytest.param(
            _extensions("one"),
            _extensions(),
            (),
            {"extension_reloads": 1, "commands_changed": True, "skills_changed": True},
            id="enable",
        ),
        pytest.param(
            _extensions("one"),
            {**_extensions("two"), **_SKILLS, **_RECALL},
            ("skills", "recall"),
            {"extension_reloads": 1, "commands_changed": True, "skills_changed": True},
            id="enable-and-disable-with-skills-and-recall",
        ),
        # Extension config applies live through the Extension's config reader.
        pytest.param(
            _extensions("one"),
            _extensions("one", config={"two": {"url": "http://y"}}),
            (),
            {},
            id="config-only",
        ),
        # Raw Settings accept null as the default empty disabled set.
        pytest.param({"extensions": {"disabled": None}}, _extensions(), (), {}, id="null-disabled"),
        pytest.param({}, {"keep_awake": True}, (), {"keep_awake_reloads": 1}, id="keep-awake"),
        pytest.param(
            {}, {"timezone": "America/New_York"}, (), {"timezone_reloads": 1}, id="timezone"
        ),
        # A changed speech binding may ask for its local model to preload; an
        # unchanged one is not loaded again.
        pytest.param(
            {"model_tasks": {"speech_to_text": {"target": "local/parakeet"}}},
            {
                "model_tasks": {
                    "speech_to_text": {"target": "local/parakeet"},
                    "text_to_speech": {"target": "local/chatterbox"},
                }
            },
            (),
            {"speech_preloads": [["text_to_speech"]]},
            id="speech-binding",
        ),
        # A new embedding binding is indexed for without waiting for the backoff.
        pytest.param(
            {},
            {"model_tasks": {"text_embedding": {"target": "openrouter/embed"}}},
            (),
            {"embedding_binding_changes": 1},
            id="text-embedding-binding",
        ),
        # A changed retention period wakes the archive's retention sweep.
        pytest.param(
            {}, {"archive": {"retention_days": 14}}, (), {"retention_changes": 1}, id="retention"
        ),
    ],
)
def test_settings_changes_refresh_only_their_live_services(
    shared_runtime: Runtime,
    monkeypatch: pytest.MonkeyPatch,
    previous: dict[str, Any],
    current: dict[str, Any],
    refresh_sections: tuple[str, ...],
    expected: dict[str, Any],
) -> None:
    extension_reload = AsyncMock()
    disabled_change = AsyncMock()
    skills_reload = AsyncMock()
    recall_reload = Mock()
    keep_awake_reload = Mock()
    timezone_reload = Mock()
    speech_preload = Mock()
    binding_change = Mock()
    retention_change = Mock()
    monkeypatch.setattr(shared_runtime, "reload_extensions", extension_reload)
    monkeypatch.setattr(shared_runtime, "apply_extension_disabled_change", disabled_change)
    monkeypatch.setattr(shared_runtime, "reload_skills_async", skills_reload)
    monkeypatch.setattr(shared_runtime, "reload_recall_backend", recall_reload)
    monkeypatch.setattr(shared_runtime, "reload_keep_awake", keep_awake_reload)
    monkeypatch.setattr(shared_runtime, "reload_timezone", timezone_reload)
    monkeypatch.setattr(shared_runtime.speech, "preload_configured", speech_preload)
    monkeypatch.setattr(shared_runtime.recall, "embedding_binding_changed", binding_change)
    monkeypatch.setattr(shared_runtime.archive, "retention_changed", retention_change)

    effects = asyncio.run(
        shared_runtime.apply_settings_change(previous, current, refresh_sections=refresh_sections)
    )

    assert {
        "extension_reloads": extension_reload.await_count,
        "disabled_changes": [call.args[0] for call in disabled_change.await_args_list],
        "skills_reloads": skills_reload.await_count,
        "recall_reloads": recall_reload.call_count,
        "keep_awake_reloads": keep_awake_reload.call_count,
        "timezone_reloads": timezone_reload.call_count,
        "speech_preloads": [list(call.args[0]) for call in speech_preload.call_args_list],
        "embedding_binding_changes": binding_change.call_count,
        "retention_changes": retention_change.call_count,
        "commands_changed": effects.commands_changed,
        "skills_changed": effects.skills_changed,
    } == {**_NO_EFFECTS, **expected}


def test_archive_retention_never_falls_back_to_the_default_period(runtime: Runtime) -> None:
    def listed() -> tuple[int | None, bool]:
        page = asyncio.run(runtime.archive.list(ArchiveEntryFilter()))
        return page.retention_days, page.retention_unknown

    write_settings(runtime.storage.data_dir, {"archive": {"retention_days": 14}})
    assert listed() == (14, False)
    # A damaged file would read as the default 30 days, which may be shorter.
    runtime.storage.settings_path.write_text('{"archive": ', encoding="utf-8")
    assert runtime.storage.load_archive_settings() == {"retention_days": 30}
    assert listed() == (None, True)


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
