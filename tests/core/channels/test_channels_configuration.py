"""Channels: configuration behavior."""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from core.channels import (
    ChannelConfig,
    ChannelConfigError,
    ChannelNotFoundError,
    ChannelStorage,
    managed_channel_token_env_var,
)
from core.channels.config import _normalize_channel_id
from tests.core.channels.channels_helpers import (
    make_config,
    make_service,
)

pytestmark = pytest.mark.usefixtures("current_format_data_directory")


def make_config_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": "tg-assistant",
        "platform": "telegram",
        "agent_id": "assistant",
        "dm_scope": "per_conversation",
        "allowed_chat_ids": [],
        "token_env_var": "TELEGRAM_BOT_TOKEN_TG_ASSISTANT",
    }
    payload.update(overrides)
    return payload


def test_channel_config_enabled_defaults_true() -> None:
    config = ChannelConfig.from_dict(make_config_payload())

    assert config.enabled is True


def test_channel_config_gating_defaults() -> None:
    config = ChannelConfig.from_dict(make_config_payload())

    assert config.response_mode == "mention"
    assert config.mention_patterns == []
    assert config.observe_unaddressed is False


def test_channel_config_rejects_unknown_response_mode() -> None:
    with pytest.raises(ChannelConfigError):
        ChannelConfig.from_dict(make_config_payload(response_mode="sometimes"))


def test_channel_config_rejects_invalid_mention_pattern_regex() -> None:
    with pytest.raises(ChannelConfigError):
        ChannelConfig.from_dict(make_config_payload(mention_patterns=["[unclosed"]))


def test_channel_config_rejects_empty_mention_pattern() -> None:
    with pytest.raises(ChannelConfigError):
        ChannelConfig.from_dict(make_config_payload(mention_patterns=["  "]))


def test_channel_config_rejects_non_boolean_observe_unaddressed() -> None:
    with pytest.raises(ChannelConfigError):
        ChannelConfig.from_dict(make_config_payload(observe_unaddressed="true"))


def test_channel_config_round_trips_gating_fields() -> None:
    config = ChannelConfig.from_dict(
        make_config_payload(
            response_mode="all",
            mention_patterns=["vbot", r"hey\s+bot"],
            observe_unaddressed=True,
        )
    )

    restored = ChannelConfig.from_dict(config.to_dict())

    assert restored.response_mode == "all"
    assert restored.mention_patterns == ["vbot", r"hey\s+bot"]
    assert "owner_user_ids" not in config.to_dict()
    assert restored.observe_unaddressed is True


def test_channel_config_normalizes_allowed_chat_ids_to_strings() -> None:
    config = ChannelConfig.from_dict(make_config_payload(allowed_chat_ids=[12345, " 67890 "]))

    assert config.allowed_chat_ids == ["12345", "67890"]


def test_channel_storage_crud_round_trip(tmp_path: Path) -> None:
    # Arrange
    storage = ChannelStorage(tmp_path)
    initial = make_config(allowed_chat_ids=[])
    updated = replace(initial, allowed_chat_ids=["12345"], enabled=False)

    # Act
    storage.save(initial)
    loaded = storage.get(initial.id)
    listed = storage.load_all()
    storage.save(updated)
    reloaded = storage.get(initial.id)
    storage.delete(initial.id)

    # Assert
    assert loaded.to_dict() == initial.to_dict()
    assert [item.id for item in listed] == [initial.id]
    assert reloaded.allowed_chat_ids == ["12345"]
    assert reloaded.enabled is False
    with pytest.raises(ChannelNotFoundError, match=initial.id):
        storage.get(initial.id)


@pytest.mark.asyncio
async def test_channel_access_mutations_do_not_change_channel_config(tmp_path: Path) -> None:
    ChannelStorage(tmp_path).save(make_config(allowed_chat_ids=["-100"]))
    config_path = tmp_path / "channels" / "tg-assistant" / "channel.json"
    original_config = config_path.read_bytes()
    service = make_service(tmp_path)
    try:
        await service._state.snapshot_participant_role("tg-assistant", "-100", "50", "Alice")
        await service.set_channel_self_user_id("tg-assistant", "50")
        await service.grant_channel_group_admin("tg-assistant", "-100", "51")
        await service.revoke_channel_group_admin("tg-assistant", "-100", "51")
        access = await service.channel_access("tg-assistant")
    finally:
        service.close()

    assert access["self_user_id"] == "50"
    assert config_path.read_bytes() == original_config
    assert not (config_path.parent / "access.json").exists()


def test_unknown_config_fields_are_kept_when_the_channel_is_saved(tmp_path: Path) -> None:
    storage = ChannelStorage(tmp_path)
    config_dir = tmp_path / "channels" / "tg-assistant"
    config_dir.mkdir(parents=True)
    config_path = config_dir / "channel.json"
    payload = {
        "format_version": 1,
        **make_config_payload(allowed_chat_ids=["-100"], owner_user_ids=[50]),
    }
    config_path.write_text(json.dumps(payload), encoding="utf-8")

    config = storage.get("tg-assistant")
    assert [loaded.id for loaded in storage.load_all()] == ["tg-assistant"]
    storage.save(replace(config, enabled=False))

    rewritten = json.loads(config_path.read_text(encoding="utf-8"))
    assert rewritten["format_version"] == 1
    assert rewritten["enabled"] is False
    assert rewritten["owner_user_ids"] == [50]
    assert not (config_dir / "access.json").exists()


@pytest.mark.parametrize("stored", ["{", '{"format_version": 2}'])
def test_channel_config_that_fails_to_load_is_never_overwritten(
    tmp_path: Path, stored: str
) -> None:
    storage = ChannelStorage(tmp_path)
    config_dir = tmp_path / "channels" / "tg-assistant"
    config_dir.mkdir(parents=True)
    config_path = config_dir / "channel.json"
    config_path.write_text(stored, encoding="utf-8")

    with pytest.raises(ChannelConfigError, match="Refusing to overwrite Channel config"):
        storage.save(make_config())
    assert config_path.read_text(encoding="utf-8") == stored


def test_validate_channel_document_requires_the_current_format_version() -> None:
    from core.channels import validate_channel_document

    payload = make_config_payload()
    assert [item.path for item in validate_channel_document(payload)] == ["$.format_version"]
    assert validate_channel_document({"format_version": 1, **payload}) == []


@pytest.mark.parametrize(
    "bad_id",
    ["../agents", "..", "foo/bar", "a\\b", "/etc", ".", "tg/../x", "tg-assistant/"],
)
def test_normalize_channel_id_rejects_path_components(bad_id: str) -> None:
    # A channel id is a storage path segment; separators and traversal must be refused.
    with pytest.raises(ChannelConfigError):
        _normalize_channel_id(bad_id)


def test_normalize_channel_id_accepts_valid_slug() -> None:
    assert _normalize_channel_id("  tg-assistant  ") == "tg-assistant"


def test_channel_storage_delete_rejects_path_traversal_id(tmp_path: Path) -> None:
    # Arrange: a real channels dir plus a sibling that a traversal id would target.
    storage = ChannelStorage(tmp_path)
    (tmp_path / "channels").mkdir()
    sibling = tmp_path / "agents"
    sibling.mkdir()
    sibling.joinpath("keep.txt").write_text("important", encoding="utf-8")

    # Act / Assert: the traversal is refused before any filesystem deletion.
    with pytest.raises(ChannelConfigError):
        storage.delete("../agents")

    # The traversal target survives untouched.
    assert sibling.is_dir()
    assert sibling.joinpath("keep.txt").read_text(encoding="utf-8") == "important"


def test_channel_service_delete_rejects_path_traversal_id(tmp_path: Path) -> None:
    # The same guard holds one layer up, where channel.delete RPC enters.
    service = make_service(tmp_path)
    (tmp_path / "channels").mkdir()
    sibling = tmp_path / "agents"
    sibling.mkdir()
    sibling.joinpath("keep.txt").write_text("important", encoding="utf-8")

    with pytest.raises(ChannelConfigError):
        service.delete_channel("../agents")

    assert sibling.is_dir()
    assert sibling.joinpath("keep.txt").read_text(encoding="utf-8") == "important"


def test_channel_storage_load_all_missing_directory_returns_empty_list(tmp_path: Path) -> None:
    storage = ChannelStorage(tmp_path)

    assert storage.load_all() == []


def test_channel_storage_validates_channel_json_on_read(tmp_path: Path) -> None:
    storage = ChannelStorage(tmp_path)
    config_dir = tmp_path / "channels" / "tg-assistant"
    config_dir.mkdir(parents=True)
    config_dir.joinpath("channel.json").write_text(
        json.dumps({"format_version": 1, **make_config().to_dict(), "enabled": "true"}),
        encoding="utf-8",
    )

    with pytest.raises(ChannelConfigError):
        storage.get("tg-assistant")


def test_channel_storage_read_rejects_invalid_mention_pattern(tmp_path: Path) -> None:
    storage = ChannelStorage(tmp_path)
    config_dir = tmp_path / "channels" / "tg-assistant"
    config_dir.mkdir(parents=True)
    config_dir.joinpath("channel.json").write_text(
        json.dumps(
            {"format_version": 1, **make_config().to_dict(), "mention_patterns": ["[unclosed"]}
        ),
        encoding="utf-8",
    )

    with pytest.raises(ChannelConfigError):
        storage.get("tg-assistant")


def test_channel_storage_read_rejects_invalid_response_mode(tmp_path: Path) -> None:
    storage = ChannelStorage(tmp_path)
    config_dir = tmp_path / "channels" / "tg-assistant"
    config_dir.mkdir(parents=True)
    config_dir.joinpath("channel.json").write_text(
        json.dumps({"format_version": 1, **make_config().to_dict(), "response_mode": "sometimes"}),
        encoding="utf-8",
    )

    with pytest.raises(ChannelConfigError):
        storage.get("tg-assistant")


def test_channel_storage_load_all_skips_invalid_configs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    storage = ChannelStorage(tmp_path)
    storage.save(make_config("tg-valid"))
    broken_dir = tmp_path / "channels" / "tg-broken"
    broken_dir.mkdir(parents=True)
    broken_dir.joinpath("channel.json").write_text(
        json.dumps(
            {"format_version": 1, **make_config("tg-broken").to_dict(), "enabled": "not-a-bool"}
        ),
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING):
        loaded = storage.load_all()

    # One corrupt config is skipped (logged), not raised, so the rest stay loadable.
    assert [config.id for config in loaded] == ["tg-valid"]
    assert any("tg-broken" in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize(
    "overrides",
    [
        {"id": "../bad"},
        {"agent_id": "../bad"},
        {"agent_id": "bad/name"},
        {"allowed_chat_ids": [True]},
        {"allowed_chat_ids": "123"},
        {"mention_patterns": ["[invalid"]},
        {"mention_patterns": None},
        {"enabled": "yes"},
        {"observe_unaddressed": 1},
        {"platform": "unknown"},
    ],
)
def test_channel_config_validation_agrees_across_all_entry_points(
    tmp_path: Path, overrides: dict[str, Any]
) -> None:
    from core.channels import validate_channel_data

    payload = make_config_payload(**overrides)
    assert any(item.severity == "error" for item in validate_channel_data(payload))
    with pytest.raises(ChannelConfigError):
        ChannelConfig.from_dict(payload)
    with pytest.raises(ChannelConfigError):
        ChannelStorage(tmp_path).save(ChannelConfig(**payload))
    assert not (tmp_path / "channels").exists()


def test_channel_config_validation_normalizes_accepted_values_consistently(tmp_path: Path) -> None:
    from core.channels import validate_channel_data

    payload = make_config_payload(
        id=" tg-assistant ", agent_id=" assistant ", allowed_chat_ids=[123, " 456 "]
    )
    assert not any(item.severity == "error" for item in validate_channel_data(payload))
    config = ChannelConfig.from_dict(payload)
    storage = ChannelStorage(tmp_path)
    storage.save(ChannelConfig(**payload))
    assert storage.get("tg-assistant").to_dict() == config.to_dict()
    assert config.agent_id == "assistant"
    assert config.allowed_chat_ids == ["123", "456"]


def test_managed_channel_token_env_var_is_safe_and_collision_free() -> None:
    assert managed_channel_token_env_var("tg-main") == "VBOT_CHANNEL_TOKEN__74672D6D61696E"
    assert managed_channel_token_env_var("a-b") != managed_channel_token_env_var("a_b")
    assert managed_channel_token_env_var("main") != managed_channel_token_env_var("MAIN")
