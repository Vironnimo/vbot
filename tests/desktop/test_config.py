"""Tests for the Voice configuration schema (``desktop.wakeword.config``)."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from desktop import settings as desktop_settings
from desktop.wakeword.config import (
    DEFAULT_MODEL_IDS,
    MAX_ACTIVE_PHRASES,
    CommandAction,
    LiveVoiceAction,
    MicrophoneSelection,
    PhraseConfig,
    ServerVoiceProfile,
    VoiceConfig,
    VoiceConfigError,
    apply_voice_changes,
    canonical_profile_key,
    config_status,
    parse_voice_config,
    set_enabled,
    voice_limits,
)

SERVER = "http://127.0.0.1:8421"
KNOWN_MODEL_IDS = frozenset(
    {
        "builtin/okay_nabu",
        "builtin/hey_nabu",
        "builtin/hey_jarvis",
        "builtin/alexa",
        *(f"custom/{index}" for index in range(MAX_ACTIVE_PHRASES + 1)),
    }
)


def _known(model_id: str) -> bool:
    return model_id in KNOWN_MODEL_IDS


def _apply(raw: object, changes: object, *, server_url: str = SERVER) -> dict[str, Any]:
    return apply_voice_changes(raw, changes, server_url=server_url, known_model_ids=_known)


# -- Parsing -------------------------------------------------------------------


@pytest.mark.parametrize("raw", [None, [], "wakeword", {}])
def test_parse_returns_defaults_for_missing_or_non_object_sections(raw: object) -> None:
    config = parse_voice_config(raw)

    assert config == VoiceConfig()
    assert config.enabled is False
    assert config.microphone is None
    assert config.echo_cancellation is True
    assert config.active_model_ids == DEFAULT_MODEL_IDS
    assert all(phrase.sensitivity == 0.5 for phrase in config.phrases)
    assert dict(config.profiles) == {}


def test_parse_reads_a_complete_section() -> None:
    config = parse_voice_config(
        {
            "enabled": True,
            "microphone": {"index": 4, "name": " Studio mic ", "host_api": "Windows WASAPI"},
            "echo_cancellation": False,
            "active_model_ids": ["builtin/hey_nabu", "builtin/hey_jarvis"],
            "model_sensitivities": {"builtin/hey_nabu": 0.55, "builtin/alexa": 0.9},
            "server_profiles": {
                SERVER: {
                    "target_agent_id": "main",
                    "session_behavior": "new",
                    "phrase_actions": {
                        "builtin/hey_jarvis": {
                            "type": "command",
                            "agent_id": "coder",
                            "session_behavior": "active",
                        },
                        "builtin/hey_nabu": {"type": "live_voice", "mode": "start"},
                    },
                }
            },
        }
    )

    assert config.enabled is True
    assert config.microphone == MicrophoneSelection(4, "Studio mic", "Windows WASAPI")
    assert config.echo_cancellation is False
    assert config.phrases == (
        PhraseConfig("builtin/hey_nabu", 0.55),
        PhraseConfig("builtin/hey_jarvis", 0.5),
    )
    assert config.profiles[SERVER] == ServerVoiceProfile(
        default_agent_id="main",
        default_session_behavior="new",
        phrase_actions={
            "builtin/hey_jarvis": CommandAction("coder", "active"),
            "builtin/hey_nabu": LiveVoiceAction("start"),
        },
    )


@pytest.mark.parametrize(
    "active_model_ids",
    [
        [],
        ["builtin/okay_nabu", "builtin/okay_nabu"],
        ["builtin/okay_nabu", ""],
        ["builtin/okay_nabu", 3],
        [f"custom/{index}" for index in range(MAX_ACTIVE_PHRASES + 1)],
        "builtin/okay_nabu",
    ],
)
def test_parse_falls_back_to_default_phrases_for_malformed_active_ids(
    active_model_ids: object,
) -> None:
    config = parse_voice_config(
        {"active_model_ids": active_model_ids, "model_sensitivities": {"builtin/hey_nabu": 0.7}}
    )

    assert config.phrases == (
        PhraseConfig("builtin/okay_nabu", 0.5),
        PhraseConfig("builtin/hey_nabu", 0.7),
    )


def test_parse_accepts_the_maximum_number_of_phrases_and_trims_ids() -> None:
    model_ids = [f" custom/{index} " for index in range(MAX_ACTIVE_PHRASES)]

    config = parse_voice_config({"active_model_ids": model_ids})

    assert config.active_model_ids == tuple(model_id.strip() for model_id in model_ids)


def test_parse_drops_each_malformed_field_on_its_own() -> None:
    config = parse_voice_config(
        {
            "enabled": "yes",
            "microphone": {"index": -1, "name": "Mic", "host_api": "MME"},
            "echo_cancellation": "off",
            "active_model_ids": ["builtin/okay_nabu"],
            "model_sensitivities": {
                "builtin/okay_nabu": True,
                "builtin/hey_nabu": 0.96,
                "": 0.4,
                "builtin/alexa": float("nan"),
            },
            "model_actions": {"builtin/okay_nabu": "live_voice"},
            "unknown": {"kept": "on write"},
        }
    )

    assert config == VoiceConfig(phrases=(PhraseConfig("builtin/okay_nabu", 0.5),))


def test_parse_keeps_valid_profile_fields_next_to_malformed_ones() -> None:
    config = parse_voice_config(
        {
            "server_profiles": {
                SERVER: {
                    "target_agent_id": " ",
                    "session_behavior": "sometimes",
                    "phrase_actions": {
                        "builtin/okay_nabu": {"type": "command", "session_behavior": "later"},
                        "builtin/hey_nabu": {"type": "live_voice", "mode": "hold"},
                        "builtin/alexa": {"type": "dance"},
                        "builtin/hey_jarvis": {"type": "live_voice"},
                        "custom/0": {"type": "command", "agent_id": 7},
                        "custom/1": {"type": "command"},
                        "custom/2": "live_voice",
                    },
                },
                "http://other:8421": ["not", "a", "profile"],
                "": {"target_agent_id": "main"},
            }
        }
    )

    assert dict(config.profiles) == {
        SERVER: ServerVoiceProfile(
            phrase_actions={
                "builtin/hey_jarvis": LiveVoiceAction("toggle"),
                "custom/1": CommandAction(),
            }
        )
    }


def test_parse_merges_loopback_aliases_and_prefers_the_canonical_spelling() -> None:
    config = parse_voice_config(
        {
            "server_profiles": {
                "http://localhost:8421/": {"target_agent_id": "alias"},
                SERVER: {"target_agent_id": "canonical"},
                "http://localhost:9000": {"target_agent_id": "alias-only"},
            }
        }
    )

    assert config.profile_for("http://localhost:8421").default_agent_id == "canonical"
    assert config.profile_for("http://127.0.0.1:9000/").default_agent_id == "alias-only"
    assert set(config.profiles) == {SERVER, "http://127.0.0.1:9000"}


@pytest.mark.parametrize(
    ("server_url", "expected"),
    [
        ("http://localhost:8421", "http://127.0.0.1:8421"),
        (" http://LOCALHOST:8421/ ", "http://127.0.0.1:8421"),
        ("https://localhost", "https://127.0.0.1"),
        ("http://127.0.0.1:8421/", "http://127.0.0.1:8421"),
        ("http://pi.lan:8421//", "http://pi.lan:8421"),
        ("http://localhost:not-a-port", "http://localhost:not-a-port"),
        ("", ""),
        ("   ", ""),
    ],
)
def test_canonical_profile_key(server_url: str, expected: str) -> None:
    assert canonical_profile_key(server_url) == expected


def test_config_snapshots_are_immutable() -> None:
    config = parse_voice_config(
        {"server_profiles": {SERVER: {"phrase_actions": {"custom/1": {"type": "command"}}}}}
    )

    with pytest.raises(TypeError):
        config.profiles["x"] = ServerVoiceProfile()  # type: ignore[index]
    with pytest.raises(TypeError):
        config.profiles[SERVER].phrase_actions["x"] = CommandAction()  # type: ignore[index]


# -- Effective actions ---------------------------------------------------------


def test_effective_action_falls_back_to_profile_defaults() -> None:
    config = parse_voice_config(
        {
            "server_profiles": {
                SERVER: {
                    "target_agent_id": "main",
                    "session_behavior": "new",
                    "phrase_actions": {
                        "builtin/hey_jarvis": {"type": "command", "agent_id": "coder"},
                        "builtin/alexa": {"type": "command", "session_behavior": "active"},
                        "builtin/hey_nabu": {"type": "live_voice", "mode": "start"},
                    },
                }
            }
        }
    )

    assert config.effective_action("builtin/okay_nabu", SERVER) == CommandAction("main", "new")
    assert config.effective_action("builtin/hey_jarvis", SERVER) == CommandAction("coder", "new")
    assert config.effective_action("builtin/alexa", SERVER) == CommandAction("main", "active")
    assert config.effective_action("builtin/hey_nabu", SERVER) == LiveVoiceAction("start")
    assert config.effective_action("builtin/hey_nabu", "http://localhost:8421") == (
        LiveVoiceAction("start")
    )


def test_effective_action_without_a_profile_is_an_untargeted_command() -> None:
    config = parse_voice_config({})

    assert config.effective_action("builtin/okay_nabu", SERVER) == CommandAction(None, "active")
    assert config.effective_action("builtin/okay_nabu", "") == CommandAction(None, "active")


def test_listener_settings_change_only_for_acoustic_fields() -> None:
    base = parse_voice_config({"server_profiles": {SERVER: {"target_agent_id": "main"}}})

    rerouted = parse_voice_config(
        {
            "enabled": True,
            "server_profiles": {
                SERVER: {
                    "target_agent_id": "coder",
                    "phrase_actions": {"builtin/okay_nabu": {"type": "live_voice"}},
                }
            },
        }
    )
    retuned = parse_voice_config({"model_sensitivities": {"builtin/okay_nabu": 0.6}})
    echo_off = parse_voice_config({"echo_cancellation": False})
    other_microphone = parse_voice_config(
        {"microphone": {"index": 1, "name": "USB", "host_api": "MME"}}
    )

    assert base.listener_settings_changed(rerouted) is False
    assert base.listener_settings_changed(retuned) is True
    assert base.listener_settings_changed(echo_off) is True
    assert base.listener_settings_changed(other_microphone) is True


# -- Strict changes -------------------------------------------------------------


def test_apply_changes_writes_global_and_profile_fields() -> None:
    raw = {"enabled": True, "unknown": {"kept": True}, "model_actions": {"x": "command"}}

    section = _apply(
        raw,
        {
            "microphone": {"index": 2, "name": "USB", "host_api": "MME"},
            "echo_cancellation": False,
            "active_model_ids": ["builtin/hey_jarvis", " builtin/alexa "],
            "model_sensitivities": {"builtin/alexa": 0.8},
            "default_agent_id": " main ",
            "default_session_behavior": "new",
            "phrase_actions": {
                "builtin/hey_jarvis": {"type": "command", "agent_id": "coder"},
                "builtin/alexa": {"type": "live_voice"},
            },
        },
    )

    assert section == {
        "enabled": True,
        "unknown": {"kept": True},
        "microphone": {"index": 2, "name": "USB", "host_api": "MME"},
        "echo_cancellation": False,
        "active_model_ids": ["builtin/hey_jarvis", "builtin/alexa"],
        "model_sensitivities": {"builtin/alexa": 0.8},
        "server_profiles": {
            SERVER: {
                "target_agent_id": "main",
                "session_behavior": "new",
                "phrase_actions": {
                    "builtin/hey_jarvis": {"type": "command", "agent_id": "coder"},
                    "builtin/alexa": {"type": "live_voice", "mode": "toggle"},
                },
            }
        },
    }
    assert raw == {"enabled": True, "unknown": {"kept": True}, "model_actions": {"x": "command"}}


def test_apply_changes_merges_sensitivities_and_phrase_actions() -> None:
    raw = {
        "model_sensitivities": {"builtin/okay_nabu": 0.3, "builtin/hey_nabu": 0.6},
        "server_profiles": {
            SERVER: {
                "target_agent_id": "main",
                "phrase_actions": {
                    "builtin/okay_nabu": {"type": "live_voice", "mode": "start"},
                    "custom/deleted": {"type": "command"},
                    "builtin/hey_nabu": {"type": "command", "agent_id": "coder"},
                },
            },
            "http://other:8421": {"target_agent_id": "elsewhere"},
        },
    }
    before = copy.deepcopy(raw)

    section = _apply(
        raw,
        {
            "model_sensitivities": {"builtin/hey_nabu": 0.45},
            "phrase_actions": {
                "custom/deleted": None,
                "builtin/okay_nabu": {"type": "command", "session_behavior": "new"},
            },
        },
    )

    assert section["model_sensitivities"] == {"builtin/okay_nabu": 0.3, "builtin/hey_nabu": 0.45}
    assert section["server_profiles"] == {
        SERVER: {
            "target_agent_id": "main",
            "phrase_actions": {
                "builtin/okay_nabu": {"type": "command", "session_behavior": "new"},
                "builtin/hey_nabu": {"type": "command", "agent_id": "coder"},
            },
        },
        "http://other:8421": {"target_agent_id": "elsewhere"},
    }
    assert raw == before


def test_apply_changes_clears_the_microphone_and_default_agent() -> None:
    raw = {
        "microphone": {"index": 2, "name": "USB", "host_api": "MME"},
        "server_profiles": {SERVER: {"target_agent_id": "main"}},
    }

    section = _apply(raw, {"microphone": None, "default_agent_id": None})

    assert section["microphone"] is None
    assert section["server_profiles"][SERVER]["target_agent_id"] is None
    assert parse_voice_config(section).profile_for(SERVER).default_agent_id is None


def test_apply_changes_moves_an_alias_profile_to_the_canonical_key() -> None:
    raw = {
        "server_profiles": {
            "http://localhost:8421/": {"target_agent_id": "main", "session_behavior": "new"}
        }
    }

    section = _apply(
        raw, {"default_session_behavior": "active"}, server_url="http://localhost:8421"
    )

    assert section["server_profiles"] == {
        SERVER: {"target_agent_id": "main", "session_behavior": "active"}
    }


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"enabled": True}, "enabled"),
        ({"model_actions": {}}, "model_actions"),
        ({"microphone": {"index": "1", "name": "Mic", "host_api": "MME"}}, "microphone"),
        ({"echo_cancellation": "false"}, "echo_cancellation"),
        ({"active_model_ids": []}, "active_model_ids"),
        (
            {"active_model_ids": [f"custom/{index}" for index in range(MAX_ACTIVE_PHRASES + 1)]},
            "active_model_ids",
        ),
        ({"active_model_ids": ["builtin/alexa", "builtin/alexa"]}, "active_model_ids"),
        ({"active_model_ids": ["builtin/unknown"]}, "active_model_ids"),
        ({"active_model_ids": "builtin/alexa"}, "active_model_ids"),
        ({"model_sensitivities": {"builtin/alexa": 0.99}}, "model_sensitivities"),
        ({"model_sensitivities": {"builtin/alexa": True}}, "model_sensitivities"),
        ({"model_sensitivities": {"builtin/unknown": 0.5}}, "model_sensitivities"),
        ({"model_sensitivities": [0.5]}, "model_sensitivities"),
        ({"default_agent_id": ""}, "default_agent_id"),
        ({"default_session_behavior": None}, "default_session_behavior"),
        ({"default_session_behavior": "later"}, "default_session_behavior"),
        ({"phrase_actions": []}, "phrase_actions"),
        ({"phrase_actions": {"builtin/unknown": {"type": "command"}}}, "phrase_actions"),
        ({"phrase_actions": {"builtin/alexa": {"type": "dance"}}}, "phrase_actions"),
        ({"phrase_actions": {"builtin/alexa": "command"}}, "phrase_actions"),
        (
            {"phrase_actions": {"builtin/alexa": {"type": "live_voice", "mode": "hold"}}},
            "phrase_actions",
        ),
        (
            {"phrase_actions": {"builtin/alexa": {"type": "command", "mode": "start"}}},
            "phrase_actions",
        ),
        (
            {"phrase_actions": {"builtin/alexa": {"type": "command", "session_behavior": "x"}}},
            "phrase_actions",
        ),
        ({"phrase_actions": {"": None}}, "phrase_actions"),
    ],
)
def test_apply_changes_rejects_invalid_input_with_the_offending_field(
    changes: dict[str, Any], field: str
) -> None:
    raw = {"enabled": True}

    with pytest.raises(VoiceConfigError) as raised:
        _apply(raw, changes)

    assert raised.value.field == field
    assert raised.value.error_code == "voice_config_invalid"
    assert raw == {"enabled": True}


def test_apply_changes_rejects_a_whole_change_when_one_part_is_invalid() -> None:
    raw: dict[str, Any] = {}

    with pytest.raises(VoiceConfigError) as raised:
        _apply(raw, {"echo_cancellation": False, "active_model_ids": ["builtin/unknown"]})

    assert raised.value.field == "active_model_ids"
    assert raw == {}


def test_apply_changes_rejects_a_non_object_change() -> None:
    with pytest.raises(VoiceConfigError):
        _apply({}, ["echo_cancellation"])


@pytest.mark.parametrize("key", ["default_agent_id", "default_session_behavior", "phrase_actions"])
def test_profile_changes_require_a_server(key: str) -> None:
    values = {
        "default_agent_id": "main",
        "default_session_behavior": "new",
        "phrase_actions": {"builtin/alexa": {"type": "live_voice"}},
    }

    with pytest.raises(VoiceConfigError) as raised:
        _apply({}, {key: values[key]}, server_url="  ")

    assert raised.value.error_code == "no_server"
    assert raised.value.field == key


def test_global_changes_do_not_need_a_server() -> None:
    section = _apply({}, {"echo_cancellation": False}, server_url="")

    assert section == {"echo_cancellation": False}


def test_set_enabled_is_strict_and_drops_the_retired_actions() -> None:
    raw = {"enabled": False, "model_actions": {"x": "command"}, "unknown": 1}

    assert set_enabled(raw, True) == {"enabled": True, "unknown": 1}
    with pytest.raises(VoiceConfigError) as raised:
        set_enabled(raw, "true")
    assert raised.value.field == "enabled"
    assert raw == {"enabled": False, "model_actions": {"x": "command"}, "unknown": 1}


def test_changes_compose_with_the_settings_section_transaction(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps(
            {
                "servers": [{"host": "127.0.0.1", "port": 8421}],
                "wakeword": {"enabled": True, "model_actions": {"builtin/okay_nabu": "command"}},
            }
        ),
        encoding="utf-8",
    )

    desktop_settings.update_section(
        "wakeword",
        lambda section: _apply(section, {"default_agent_id": "main"}),
        settings_file,
    )
    with pytest.raises(VoiceConfigError):
        desktop_settings.update_section(
            "wakeword",
            lambda section: _apply(section, {"default_session_behavior": "later"}),
            settings_file,
        )

    stored = json.loads(settings_file.read_text(encoding="utf-8"))
    assert stored == {
        "servers": [{"host": "127.0.0.1", "port": 8421}],
        "wakeword": {"enabled": True, "server_profiles": {SERVER: {"target_agent_id": "main"}}},
    }
    config = parse_voice_config(desktop_settings.read_section("wakeword", settings_file))
    assert config.effective_action("builtin/okay_nabu", SERVER) == CommandAction("main", "active")


# -- Status projection ------------------------------------------------------------


def test_config_status_projects_phrases_actions_and_limits() -> None:
    config = parse_voice_config(
        {
            "enabled": True,
            "microphone": {"index": 4, "name": "Studio mic", "host_api": "Windows WASAPI"},
            "active_model_ids": ["builtin/hey_nabu", "custom/0", "builtin/okay_nabu"],
            "model_sensitivities": {"builtin/hey_nabu": 0.55},
            "server_profiles": {
                SERVER: {
                    "target_agent_id": "main",
                    "phrase_actions": {
                        "custom/0": {"type": "command", "session_behavior": "new"},
                        "builtin/okay_nabu": {"type": "live_voice"},
                    },
                }
            },
        }
    )

    status = config_status(config, SERVER, {"builtin/hey_nabu": "Hey Nabu"})

    assert status == {
        "enabled": True,
        "microphone": {"index": 4, "name": "Studio mic", "host_api": "Windows WASAPI"},
        "echo_cancellation": {"enabled": True},
        "default_agent_id": "main",
        "default_session_behavior": "active",
        "phrases": [
            {
                "model_id": "builtin/hey_nabu",
                "label": "Hey Nabu",
                "sensitivity": 0.55,
                "action": {"type": "command", "agent_id": None, "session_behavior": None},
                "effective": {"type": "command", "agent_id": "main", "session_behavior": "active"},
            },
            {
                "model_id": "custom/0",
                "label": "custom/0",
                "sensitivity": 0.5,
                "action": {"type": "command", "agent_id": None, "session_behavior": "new"},
                "effective": {"type": "command", "agent_id": "main", "session_behavior": "new"},
            },
            {
                "model_id": "builtin/okay_nabu",
                "label": "builtin/okay_nabu",
                "sensitivity": 0.5,
                "action": {"type": "live_voice", "mode": "toggle"},
                "effective": {"type": "live_voice", "mode": "toggle"},
            },
        ],
        "limits": voice_limits(),
    }
    assert voice_limits() == {
        "max_active_phrases": MAX_ACTIVE_PHRASES,
        "min_sensitivity": 0.05,
        "max_sensitivity": 0.95,
    }
