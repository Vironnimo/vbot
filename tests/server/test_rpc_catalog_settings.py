"""Server RPC catalog and settings handlers."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.projects.projects import PROJECT_DEFAULT_ALLOWED_TOOLS
from core.storage import StorageError
from server.rpc.methods import dispatch_rpc
from tests.server.rpc_test_support import (
    JsonObject,
    StubAdapter,
    _no_models_dev_fetch,
    make_state,
)

__all__ = ["_no_models_dev_fetch"]


@pytest.mark.asyncio
async def test_tool_list_returns_all_registered_tools_with_name_and_description(
    tmp_path: Path,
) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.tools.register(
        "z_tool",
        "Last tool alphabetically",
        {"type": "object", "properties": {}, "additionalProperties": False},
        lambda _context, _arguments: {"ok": True, "error": None, "data": {}, "artifacts": []},
    )
    state.runtime.tools.register(
        "a_tool",
        "First tool alphabetically",
        {"type": "object", "properties": {}, "additionalProperties": False},
        lambda _context, _arguments: {"ok": True, "error": None, "data": {}, "artifacts": []},
    )

    response = await dispatch_rpc(state, {"method": "tool.list", "params": {}})
    a_fingerprint = state.runtime.tools.schema_fingerprint("a_tool")
    z_fingerprint = state.runtime.tools.schema_fingerprint("z_tool")

    assert response == {
        "ok": True,
        "result": {
            "tools": [
                {
                    "name": "a_tool",
                    "description": "First tool alphabetically",
                    "ready": True,
                    "readiness_hint": None,
                    "family": None,
                    "family_label": None,
                    "activation": "configurable",
                    "activation_source": None,
                    "constraints": [],
                    "session_scoped": False,
                    "extension": None,
                    "schema_fingerprint": a_fingerprint,
                    "parallel_safe": True,
                    "project_configurable": True,
                    "project_configurability_reason": None,
                },
                {
                    "name": "z_tool",
                    "description": "Last tool alphabetically",
                    "ready": True,
                    "readiness_hint": None,
                    "family": None,
                    "family_label": None,
                    "activation": "configurable",
                    "activation_source": None,
                    "constraints": [],
                    "session_scoped": False,
                    "extension": None,
                    "schema_fingerprint": z_fingerprint,
                    "parallel_safe": True,
                    "project_configurable": True,
                    "project_configurability_reason": None,
                },
            ],
            "default_project_tools": list(PROJECT_DEFAULT_ALLOWED_TOOLS),
        },
    }


@pytest.mark.asyncio
async def test_tool_list_omits_internal_skill_tool(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.tools.register(
        "skill",
        "Load skills",
        {"type": "object", "properties": {}, "additionalProperties": False},
        lambda _context, _arguments: {"ok": True, "error": None, "data": {}, "artifacts": []},
        internal=True,
    )

    response = await dispatch_rpc(state, {"method": "tool.list", "params": {}})

    assert response == {
        "ok": True,
        "result": {
            "tools": [],
            "default_project_tools": list(PROJECT_DEFAULT_ALLOWED_TOOLS),
        },
    }


@pytest.mark.asyncio
async def test_skill_catalog_returns_loadable_and_invalid_diagnostics(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(state, {"method": "skill.list", "params": {}})

    assert response == {
        "ok": True,
        "result": {
            "skills": [
                {
                    "name": "debugging",
                    "description": "Debug failures.",
                    "origin": None,
                    "valid": True,
                    "warnings": [],
                    "state": "available",
                    "requirements": {"missing": [], "optional_missing": []},
                },
                {
                    "name": "warned",
                    "description": "Loads with warnings.",
                    "origin": None,
                    "valid": False,
                    "warnings": ["Name does not match directory."],
                    "state": "available",
                    "requirements": {"missing": [], "optional_missing": []},
                },
            ],
            "invalid_skills": [
                {
                    "name": "broken",
                    "path": str(Path("/skills/broken/SKILL.md")),
                    "valid": False,
                    "warnings": ["missing description"],
                }
            ],
        },
    }


@pytest.mark.asyncio
async def test_settings_update_persists_supported_language_and_returns_full_payload(
    tmp_path: Path,
) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.server_bind = {
        "listen_host": "127.0.0.1",
        "listen_port": 8500,
        "port_source": "VBOT_SERVER_PORT",
    }

    response = await dispatch_rpc(
        state,
        {"method": "settings.update", "params": {"appearance": {"language": "en"}}},
    )

    assert response["ok"] is True
    assert state.runtime.storage.load_appearance_settings() == {
        "language": "en",
        "chat_width": "comfortable",
        "chat_working_mode": "normal",
    }
    assert response["result"]["appearance"] == {
        "language": "en",
        "available_languages": ["en"],
        "chat_width": "comfortable",
        "chat_working_mode": "normal",
    }
    assert response["result"]["general"]["server"] == {
        "listen_host": "127.0.0.1",
        "listen_port": 8500,
        "port_source": "VBOT_SERVER_PORT",
    }


@pytest.mark.asyncio
async def test_settings_update_persists_skill_directories_and_returns_full_payload(
    tmp_path: Path,
) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(
        state,
        {
            "method": "settings.update",
            "params": {"skills": {"directories": ["~/skills", " C:/skills/team "]}},
        },
    )

    assert response["ok"] is True, response
    assert state.runtime.storage.load_skill_directory_settings() == [
        "~/skills",
        " C:/skills/team ",
    ]
    assert response["result"]["skills"] == {
        "default_directory": str(tmp_path / "skills"),
        "directories": ["~/skills", " C:/skills/team "],
    }


@pytest.mark.asyncio
async def test_settings_update_persists_subagent_settings_and_returns_full_payload(
    tmp_path: Path,
) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(
        state,
        {
            "method": "settings.update",
            "params": {
                "subagents": {
                    "max_subagent_depth": 6,
                    "max_subagents_per_turn": 12,
                    "subagent_timeout_minutes": 90,
                }
            },
        },
    )

    assert response["ok"] is True, response
    assert state.runtime.storage.load_settings() == {
        "max_subagent_depth": 6,
        "max_subagents_per_turn": 12,
        "subagent_timeout_minutes": 90,
    }
    assert response["result"]["subagents"] == {
        "max_subagent_depth": 6,
        "max_subagents_per_turn": 12,
        "subagent_timeout_minutes": 90,
    }


@pytest.mark.asyncio
async def test_settings_update_persists_reflection_settings_and_returns_full_payload(
    tmp_path: Path,
) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(
        state,
        {
            "method": "settings.update",
            "params": {"reflection": {"enabled": True, "memory_turn_interval": 5}},
        },
    )

    assert response["ok"] is True, response
    # Partial update merges with defaults; the full payload echoes the section.
    assert response["result"]["reflection"] == {
        "enabled": True,
        "memory_turn_interval": 5,
        "skill_model_step_interval": 10,
    }


@pytest.mark.asyncio
async def test_settings_update_rejects_invalid_reflection_section(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(
        state,
        {
            "method": "settings.update",
            "params": {"reflection": {"memory_turn_interval": 0}},
        },
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_settings_update_persists_compaction_settings_and_returns_full_payload(
    tmp_path: Path,
) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(
        state,
        {
            "method": "settings.update",
            "params": {
                "compaction": {
                    "enabled": False,
                    "trigger": {"type": "context_ratio", "threshold": 0.9},
                    "strategy": {
                        "type": "summary_tail",
                        "tail_tokens": 12000,
                        "summary_model": "openai/gpt-5.2",
                    },
                }
            },
        },
    )

    assert response["ok"] is True, response
    assert state.runtime.storage.load_compaction_settings() == {
        "enabled": False,
        "trigger": {"type": "context_ratio", "threshold": 0.9},
        "strategy": {
            "type": "summary_tail",
            "tail_tokens": 12000,
            "summary_model": "openai/gpt-5.2",
        },
    }
    assert response["result"]["compaction"] == {
        "enabled": False,
        "trigger": {"type": "context_ratio", "threshold": 0.9},
        "strategy": {
            "type": "summary_tail",
            "tail_tokens": 12000,
            "summary_model": "openai/gpt-5.2",
        },
    }


@pytest.mark.asyncio
async def test_settings_update_persists_recall_backend_and_reloads_runtime(
    tmp_path: Path,
) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(
        state,
        {
            "method": "settings.update",
            "params": {
                "recall": {
                    "backend": "sqlite_fts",
                }
            },
        },
    )

    assert response["ok"] is True, response
    assert state.runtime.storage.load_recall_settings() == {"backend": "sqlite_fts"}
    assert state.runtime.recall_reload_count == 1
    assert response["result"]["recall"] == {
        "backend": "sqlite_fts",
        "available_backends": ["canonical_scan", "hybrid", "sqlite_fts", "vector"],
    }


@pytest.mark.asyncio
async def test_settings_update_accepts_vector_recall_backend(
    tmp_path: Path,
) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(
        state,
        {
            "method": "settings.update",
            "params": {
                "recall": {
                    "backend": "vector",
                }
            },
        },
    )

    assert response["ok"] is True, response
    assert state.runtime.storage.load_recall_settings() == {"backend": "vector"}
    assert state.runtime.recall_reload_count == 1
    assert response["result"]["recall"]["backend"] == "vector"
    assert "vector" in response["result"]["recall"]["available_backends"]


@pytest.mark.asyncio
async def test_settings_get_lists_extension_recall_backend(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.available_recall_backends = lambda: ["canonical_scan", "my_ext_backend"]

    response = await dispatch_rpc(state, {"method": "settings.get"})

    assert response["ok"] is True, response
    assert response["result"]["recall"]["available_backends"] == [
        "canonical_scan",
        "my_ext_backend",
    ]


@pytest.mark.asyncio
async def test_settings_update_accepts_extension_recall_backend(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.available_recall_backends = lambda: ["canonical_scan", "my_ext_backend"]

    response = await dispatch_rpc(
        state,
        {"method": "settings.update", "params": {"recall": {"backend": "my_ext_backend"}}},
    )

    assert response["ok"] is True, response
    assert state.runtime.storage.load_recall_settings() == {"backend": "my_ext_backend"}
    assert state.runtime.recall_reload_count == 1
    assert "my_ext_backend" in response["result"]["recall"]["available_backends"]


@pytest.mark.asyncio
async def test_settings_update_persists_web_search_provider(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(
        state,
        {
            "method": "settings.update",
            "params": {
                "web_search": {
                    "provider": "searxng",
                    "searxng": {"base_url": "http://localhost:9999"},
                }
            },
        },
    )

    assert response["ok"] is True, response
    assert state.runtime.storage.load_web_search_settings() == {
        "provider": "searxng",
        "default_count": 12,
        "searxng": {"base_url": "http://localhost:9999"},
    }
    assert response["result"]["web_search"] == {
        "provider": "searxng",
        "available_providers": ["brave", "searxng"],
        "default_count": 12,
        "searxng": {"base_url": "http://localhost:9999"},
    }


@pytest.mark.asyncio
async def test_settings_update_persists_agent_default_model_and_returns_defaults(
    tmp_path: Path,
) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(
        state,
        {
            "method": "settings.update",
            "params": {
                "defaults": {
                    "agent": {
                        "model": "openai/gpt-4.1-mini",
                    }
                }
            },
        },
    )

    assert response["ok"] is True, response
    assert state.runtime.storage.load_defaults() == {"agent": {"model": "openai/gpt-4.1-mini"}}
    assert response["result"]["defaults"] == {"agent": {"model": "openai/gpt-4.1-mini"}}


@pytest.mark.asyncio
async def test_settings_update_removes_agent_default_temperature_on_null(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.storage.update_settings_sections(
        {
            "defaults": {
                "agent": {
                    "model": "openai/gpt-4.1-mini",
                    "temperature": 0.6,
                }
            }
        }
    )

    response = await dispatch_rpc(
        state,
        {
            "method": "settings.update",
            "params": {
                "defaults": {
                    "agent": {
                        "temperature": None,
                    }
                }
            },
        },
    )

    assert response["ok"] is True, response
    assert state.runtime.storage.load_defaults() == {"agent": {"model": "openai/gpt-4.1-mini"}}
    assert response["result"]["defaults"] == {"agent": {"model": "openai/gpt-4.1-mini"}}


@pytest.mark.asyncio
async def test_settings_update_rejects_unknown_defaults_agent_field(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(
        state,
        {
            "method": "settings.update",
            "params": {
                "defaults": {
                    "agent": {
                        "unknown_field": True,
                    }
                }
            },
        },
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"
    assert "unknown_field" in response["error"]["message"]


@pytest.mark.asyncio
async def test_settings_update_reloads_runtime_skills_for_immediate_catalog(
    tmp_path: Path,
) -> None:
    state = make_state(tmp_path, StubAdapter())

    update_response = await dispatch_rpc(
        state,
        {
            "method": "settings.update",
            "params": {"skills": {"directories": ["debugging"]}},
        },
    )
    list_response = await dispatch_rpc(state, {"method": "skill.list", "params": {}})

    assert update_response["ok"] is True, update_response
    assert list_response == {
        "ok": True,
        "result": {
            "skills": [
                {
                    "name": "debugging",
                    "description": "debugging skill.",
                    "origin": None,
                    "valid": True,
                    "warnings": [],
                    "state": "available",
                    "requirements": {"missing": [], "optional_missing": []},
                }
            ],
            "invalid_skills": [],
        },
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [
        {},
        {"general": {}},
        {"appearance": []},
        {"appearance": {}},
        {"appearance": {"show_token_counts": False}},
        {"appearance": {"language": ""}},
        {"skills": []},
        {"skills": {}},
        {"skills": {"extra": []}},
        {"skills": {"directories": "~/skills"}},
        {"skills": {"directories": [1]}},
        {"subagents": []},
        {"subagents": {}},
        {"subagents": {"extra": 1}},
        {
            "subagents": {
                "max_subagent_depth": 0,
                "max_subagents_per_turn": 8,
                "subagent_timeout_minutes": 60,
            }
        },
        {
            "subagents": {
                "max_subagent_depth": True,
                "max_subagents_per_turn": 8,
                "subagent_timeout_minutes": 60,
            }
        },
        {
            "subagents": {
                "max_subagent_depth": 4,
                "max_subagents_per_turn": "8",
                "subagent_timeout_minutes": 60,
            }
        },
        {
            "subagents": {
                "max_subagent_depth": 4,
                "max_subagents_per_turn": 8,
            }
        },
        {"recall": []},
        {"recall": {"extra": True}},
        {"recall": {"backend": "unknown_backend"}},
        {"web_search": []},
        {"web_search": {"provider": "unknown"}},
        {"web_search": {"provider": "searxng", "searxng": {"base_url": ""}}},
    ],
)
async def test_settings_update_rejects_unsupported_sections_and_fields(
    tmp_path: Path,
    params: JsonObject,
) -> None:
    state = make_state(tmp_path, StubAdapter())
    original_appearance = state.runtime.storage.load_appearance_settings()

    response = await dispatch_rpc(state, {"method": "settings.update", "params": params})

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"
    assert state.runtime.storage.load_appearance_settings() == original_appearance


@pytest.mark.asyncio
async def test_settings_update_maps_storage_validation_errors_to_domain_error(
    tmp_path: Path,
) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(
        state,
        {"method": "settings.update", "params": {"appearance": {"language": "fr"}}},
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "domain_error"


@pytest.mark.asyncio
async def test_settings_update_rejects_compaction_threshold_out_of_range(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(
        state,
        {
            "method": "settings.update",
            "params": {
                "compaction": {
                    "enabled": True,
                    "trigger": {"type": "context_ratio", "threshold": 1.5},
                    "strategy": {
                        "type": "summary_tail",
                        "tail_tokens": 15000,
                        "summary_model": None,
                    },
                }
            },
        },
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"
    assert "params.compaction.trigger.threshold" in response["error"]["message"]


@pytest.mark.asyncio
async def test_settings_update_maps_storage_section_error_without_partial_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = make_state(tmp_path, StubAdapter())
    original_settings = {
        "appearance": {"language": "en", "theme": "legacy"},
        "server_port": 8500,
    }
    state.runtime.storage.save_settings(original_settings)

    def fail_settings_update(_settings_update: object) -> JsonObject:
        raise StorageError("compaction write failed")

    monkeypatch.setattr(
        state.runtime.storage,
        "update_settings_sections",
        fail_settings_update,
    )

    response = await dispatch_rpc(
        state,
        {
            "method": "settings.update",
            "params": {
                "appearance": {"language": "en"},
                "compaction": {
                    "enabled": False,
                    "trigger": {"type": "context_ratio", "threshold": 0.9},
                    "strategy": {
                        "type": "summary_tail",
                        "tail_tokens": 12000,
                        "summary_model": None,
                    },
                },
            },
        },
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "domain_error"
    assert state.runtime.storage.load_settings() == original_settings
