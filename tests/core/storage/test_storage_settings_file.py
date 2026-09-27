"""The ``settings.json`` file: format, unknown fields, patches, invalid files and caching."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from core.settings import validate_settings_file
from core.settings.paths import SettingsPathError, parse_patch_operations
from core.storage import StorageError, StorageManager
from core.storage import storage as storage_module


def _write_raw(storage: StorageManager, content: str | bytes) -> None:
    storage.settings_path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        content = content.encode("utf-8")
    storage.settings_path.write_bytes(content)


def _on_disk(storage: StorageManager) -> dict[str, Any]:
    document: dict[str, Any] = json.loads(storage.settings_path.read_text(encoding="utf-8"))
    return document


def test_settings_round_trip_as_a_versioned_sorted_document(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)
    assert storage.load_settings() == {}
    settings = {"port": 8420, "keep_awake": True, "timezone": "Europe/Berlin"}

    storage.save_settings(settings)

    assert storage.load_settings() == settings
    text = storage.settings_path.read_text(encoding="utf-8")
    assert text.endswith("\n")
    assert list(json.loads(text)) == ["format_version", "keep_awake", "port", "timezone"]
    assert json.loads(text)["format_version"] == 1


def test_save_settings_rejects_unserializable_values(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)

    with pytest.raises(StorageError):
        storage.save_settings({"path": object()})

    assert "path" not in _on_disk(storage)


def test_settings_updates_keep_unknown_fields_on_disk_at_every_modeled_level(
    tmp_path: Path,
) -> None:
    storage = StorageManager(tmp_path)
    _write_raw(
        storage,
        json.dumps(
            {
                "format_version": 1,
                "future_top_level": {"kept": True},
                "appearance": {"language": "en", "future_appearance": 1},
                "providers": {
                    "custom": {
                        "local": {
                            "name": "Local",
                            "adapter": "openai_compatible",
                            "base_url": "http://127.0.0.1:1234/v1",
                            "auth": "none",
                            "future_provider_field": "x",
                            "models": {"m": {"name": "M", "future_model_field": 2}},
                        }
                    }
                },
            }
        ),
    )

    loaded = storage.load_settings()
    storage.update_settings_sections({"appearance": {"language": "en", "chat_width": "wide"}})
    storage.update_settings(lambda settings: settings.update({"keep_awake": True}))

    assert "future_top_level" not in loaded
    assert loaded["appearance"] == {"language": "en"}
    assert "future_provider_field" not in loaded["providers"]["custom"]["local"]
    on_disk = _on_disk(storage)
    assert on_disk["future_top_level"] == {"kept": True}
    assert on_disk["appearance"]["future_appearance"] == 1
    assert on_disk["appearance"]["chat_width"] == "wide"
    assert on_disk["keep_awake"] is True
    local = on_disk["providers"]["custom"]["local"]
    assert local["future_provider_field"] == "x"
    assert local["models"]["m"]["future_model_field"] == 2


@pytest.mark.parametrize("mutation", ["patch", "section"])
def test_reset_last_agent_default_keeps_unknown_siblings(tmp_path: Path, mutation: str) -> None:
    storage = StorageManager(tmp_path)
    storage.save_settings(
        {
            "defaults": {
                "agent": {"temperature": 0.2, "future_agent_option": {"enabled": True}},
                "future_defaults_option": "retained",
            }
        }
    )

    if mutation == "patch":
        operations = parse_patch_operations([{"op": "unset", "path": "defaults.agent.temperature"}])
        storage.patch_settings(operations)
    else:
        storage.update_settings_sections({"defaults": {"agent": {"temperature": None}}})

    assert storage.load_defaults() == {}
    assert _on_disk(storage)["defaults"] == {
        "agent": {"future_agent_option": {"enabled": True}},
        "future_defaults_option": "retained",
    }


@pytest.mark.parametrize("mutation", ["patch", "section"])
def test_remove_last_task_binding_keeps_unknown_siblings_only(
    tmp_path: Path, mutation: str
) -> None:
    storage = StorageManager(tmp_path)
    storage.save_settings(
        {
            "model_tasks": {
                "text_to_speech": {"target": "openai/tts-1", "future_option": True},
                "future_task": {"target": "future/target"},
            }
        }
    )

    if mutation == "patch":
        operations = parse_patch_operations(
            [{"op": "unset", "path": 'model_tasks["text_to_speech"].target'}]
        )
        storage.patch_settings(operations)
    else:
        storage.update_model_task_settings({"text_to_speech": {"target": ""}})

    assert storage.load_model_task_settings() == {}
    assert _on_disk(storage)["model_tasks"] == {"future_task": {"target": "future/target"}}


def test_reset_missing_settings_does_not_create_empty_containers(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)

    storage.update_settings_sections({"defaults": {"agent": {"temperature": None}}})
    storage.update_model_task_settings({"text_to_speech": {"target": ""}})

    assert storage.load_settings() == {}


@pytest.mark.parametrize("reset", ["last_field", "whole_policy"])
@pytest.mark.parametrize("unknown_field", [False, True])
def test_reset_model_routing_override_prunes_only_after_preserving_unknown_fields(
    tmp_path: Path, reset: str, unknown_field: bool
) -> None:
    storage = StorageManager(tmp_path)
    default_policy = {"mode": "allowed", "providers": ["approved"], "allow_fallbacks": False}
    storage.save_settings(
        {
            "providers": {
                "openrouter": {
                    "routing": {
                        "default": default_policy,
                        "models": {
                            "test/model": {
                                "mode": "automatic",
                                **({"future_option": "retained"} if unknown_field else {}),
                            },
                            "future/model": {"future_option": "retained"},
                        },
                        "future_routing": True,
                    },
                    "future_openrouter": True,
                },
                "future_provider": True,
            }
        }
    )
    path = 'providers.openrouter.routing.models["test/model"]'
    if reset == "last_field":
        path += ".mode"
    operations = parse_patch_operations([{"op": "unset", "path": path}])

    _previous, candidate, changed = storage.patch_settings(operations)
    assert changed == (path,)

    routing = storage.load_openrouter_routing_settings()
    keeps_policy = reset == "last_field" and unknown_field
    if keeps_policy:
        assert candidate["providers"]["openrouter"]["routing"]["models"]["test/model"] == {}
        assert routing["models"]["test/model"] == {
            "mode": "automatic",
            "providers": [],
            "blocked": [],
            "allow_fallbacks": True,
        }
    else:
        assert "test/model" not in routing["models"]
    assert routing["default"] == {**default_policy, "blocked": []}
    on_disk = _on_disk(storage)["providers"]
    assert on_disk["future_provider"] is True
    assert on_disk["openrouter"]["future_openrouter"] is True
    assert on_disk["openrouter"]["routing"]["future_routing"] is True
    expected = {"future/model": {"future_option": "retained"}}
    if keeps_policy:
        expected["test/model"] = {"future_option": "retained"}
    assert on_disk["openrouter"]["routing"]["models"] == expected


def test_empty_and_unknown_only_model_policies_keep_automatic_meaning(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)
    storage.save_settings(
        {
            "providers": {
                "openrouter": {
                    "routing": {
                        "default": {
                            "mode": "allowed",
                            "providers": ["approved"],
                            "allow_fallbacks": False,
                        },
                        "models": {
                            "empty/model": {},
                            "future/model": {"future_option": "retained"},
                            "explicit/model": {"mode": "automatic"},
                        },
                    }
                }
            }
        }
    )
    operations = parse_patch_operations(
        [{"op": "unset", "path": 'providers.openrouter.routing.models["future/model"].mode'}]
    )

    previous, candidate, changed = storage.patch_settings(operations)
    assert changed == ()
    assert candidate == previous
    storage.set_provider_connection_enabled("openrouter:api-key", True)

    routing = storage.load_openrouter_routing_settings()
    automatic = {"mode": "automatic", "providers": [], "blocked": [], "allow_fallbacks": True}
    assert routing["models"] == dict.fromkeys(
        ("empty/model", "future/model", "explicit/model"), automatic
    )
    assert _on_disk(storage)["providers"]["openrouter"]["routing"]["models"]["future/model"] == {
        **automatic,
        "future_option": "retained",
    }


def test_patch_runtime_validation_sees_only_known_fields_and_precedes_write(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)
    storage.save_settings({"web_search": {"provider": "searxng", "future_option": "retained"}})
    original = storage.settings_path.read_bytes()
    operations = parse_patch_operations([{"op": "unset", "path": "web_search.provider"}])

    def reject(previous: dict[str, Any], candidate: dict[str, Any]) -> None:
        assert previous == {"web_search": {"provider": "searxng"}}
        assert candidate == {"web_search": {}}
        raise ValueError("runtime validation failed")

    with pytest.raises(ValueError, match="runtime validation failed"):
        storage.patch_settings(operations, validate_candidate=reject)
    assert storage.settings_path.read_bytes() == original


def test_patch_still_rejects_new_unknown_fields(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)
    storage.save_settings({"providers": {"future_option": "retained"}})
    original = storage.settings_path.read_bytes()
    operations = parse_patch_operations(
        [{"op": "set", "path": "providers.openrouter.routing.default", "value": {"typo": True}}]
    )

    with pytest.raises(SettingsPathError, match="unsupported"):
        storage.patch_settings(operations)
    assert storage.settings_path.read_bytes() == original


def test_settings_written_by_a_newer_vbot_are_not_used_or_overwritten(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)
    original = '{"format_version": 2, "keep_awake": true}'
    _write_raw(storage, original)

    assert storage.load_settings() == {}
    with pytest.raises(StorageError, match="written by a newer vBot"):
        storage.update_settings(lambda settings: settings.update({"keep_awake": False}))
    with pytest.raises(StorageError, match="Refusing to overwrite Settings file"):
        storage.save_settings({"keep_awake": False})
    with pytest.raises(StorageError, match="written by a newer vBot"):
        storage.patch_settings(
            parse_patch_operations([{"op": "unset", "path": "server.keep_awake"}])
        )
    assert storage.settings_path.read_text(encoding="utf-8") == original


@pytest.mark.parametrize(
    ("content", "usable"),
    [
        ("[]", {}),
        ("{", {}),
        (b'{"appearance":{"language":"\xff"}}', {}),
        (
            '{"format_version": 1, "server_port": 8500, "compaction": {"enabled": "yes"}}',
            {"server_port": 8500},
        ),
        ('{"format_version": 1, "keep_awake": true, "appearance": {"chat_width": []}}', None),
        (
            '{"format_version": 1, "keep_awake": true, "compaction": {"trigger": {"threshold": '
            + "9" * 400
            + "}}}",
            None,
        ),
    ],
    ids=[
        "non-object",
        "invalid-json",
        "non-utf8",
        "invalid-field",
        "container-value",
        "float-overflow",
    ],
)
def test_invalid_settings_file_degrades_reads_and_refuses_updates(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    content: str | bytes,
    usable: dict[str, Any] | None,
) -> None:
    """Reads keep valid siblings and warn once; writes never replace the user's file."""
    storage = StorageManager(tmp_path)
    _write_raw(storage, content)
    original = storage.settings_path.read_bytes()

    with caplog.at_level("WARNING"):
        loaded = storage.load_settings()
        storage.load_settings()

    assert loaded == ({"keep_awake": True} if usable is None else usable)
    assert len(caplog.records) == 1
    with pytest.raises(StorageError):
        storage.update_settings(lambda settings: settings.update({"keep_awake": False}))
    assert storage.settings_path.read_bytes() == original


def test_settings_json_integer_limit_is_reported_without_overwriting(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)
    previous_limit = sys.get_int_max_str_digits()
    try:
        sys.set_int_max_str_digits(640)
        original = '{"unknown": ' + "9" * 641 + "}"
        _write_raw(storage, original)

        assert storage.load_settings() == {}
        assert not validate_settings_file(storage.settings_path).ok
        with pytest.raises(StorageError):
            storage.update_settings(lambda settings: settings.update({"keep_awake": False}))
        assert storage.settings_path.read_text(encoding="utf-8") == original
    finally:
        sys.set_int_max_str_digits(previous_limit)


def _age(path: Path, *, seconds: float = 60.0) -> None:
    """Move a file's mtime out of the racy window, as if written a while ago."""
    past = time.time_ns() - int(seconds * 1_000_000_000)
    os.utime(path, ns=(past, past))


def _count_settings_reads(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    reads: list[Path] = []
    load = storage_module.load_runtime_settings_json

    def counting(path: Path) -> Any:
        reads.append(path)
        return load(path)

    monkeypatch.setattr(storage_module, "load_runtime_settings_json", counting)
    return reads


def test_unchanged_settings_are_served_from_memory_as_independent_copies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = StorageManager(tmp_path)
    storage.save_settings({"defaults": {"agent": {"temperature": 0.5}}, "keep_awake": True})
    _age(storage.settings_path)
    reads = _count_settings_reads(monkeypatch)

    first = storage.load_settings()
    first["defaults"]["agent"]["temperature"] = 1.0
    first["keep_awake"] = False
    second = storage.load_settings()

    assert second == {"defaults": {"agent": {"temperature": 0.5}}, "keep_awake": True}
    assert len(reads) == 1


def test_settings_changes_are_read_after_every_kind_of_write(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)
    _write_raw(storage, '{"format_version": 1, "port": 8421}')
    _age(storage.settings_path)
    assert storage.load_settings() == {"port": 8421}

    # An external in-place edit of the same size keeps the file identity.
    storage.settings_path.write_text('{"format_version": 1, "port": 8422}', encoding="utf-8")
    assert storage.load_settings() == {"port": 8422}

    _age(storage.settings_path)
    storage.load_settings()
    storage.save_settings({"keep_awake": True, "port": 8426})
    assert storage.load_settings() == {"keep_awake": True, "port": 8426}

    storage.settings_path.unlink()
    assert storage.load_settings() == {}


def test_recently_written_settings_are_reread_even_with_an_identical_stamp(
    tmp_path: Path,
) -> None:
    """Two writes in one filesystem timestamp tick must not look unchanged."""
    storage = StorageManager(tmp_path)
    path = storage.settings_path
    _write_raw(storage, '{"format_version": 1, "port": 8421}')
    stamp = path.stat().st_mtime_ns
    assert storage.load_settings() == {"port": 8421}

    path.write_text('{"format_version": 1, "port": 8422}', encoding="utf-8")
    os.utime(path, ns=(stamp, stamp))

    assert storage.load_settings() == {"port": 8422}
