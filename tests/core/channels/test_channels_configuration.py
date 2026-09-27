"""Channels: configuration values, validation and the persisted ``channel.json``."""

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
    validate_channel_data,
    validate_channel_document,
)
from tests.core.channels.channels_test_support import make_config, make_service

pytestmark = pytest.mark.usefixtures("current_format_data_directory")

_AGENT_ID_SLUG_ERROR = "must be 1-64 characters using only letters, numbers, hyphen, or underscore"


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


def _write_channel_json(data_dir: Path, payload: dict[str, Any] | str) -> Path:
    config_dir = data_dir / "channels" / "tg-assistant"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "channel.json"
    text = payload if isinstance(payload, str) else json.dumps(payload)
    config_path.write_text(text, encoding="utf-8")
    return config_path


def test_channel_config_defaults_and_round_trips_gating_fields() -> None:
    defaults = ChannelConfig.from_dict(make_config_payload())
    assert defaults.enabled is True
    assert (defaults.response_mode, defaults.mention_patterns, defaults.observe_unaddressed) == (
        "mention",
        [],
        False,
    )

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
    assert restored.observe_unaddressed is True
    assert "owner_user_ids" not in config.to_dict()


@pytest.mark.parametrize(
    ("overrides", "path", "message"),
    [
        (
            {"id": "../bad"},
            "$.id",
            "channel_id must contain only letters, numbers, underscore, and hyphen",
        ),
        ({"agent_id": "../bad"}, "$.agent_id", _AGENT_ID_SLUG_ERROR),
        ({"agent_id": "bad/name"}, "$.agent_id", _AGENT_ID_SLUG_ERROR),
        ({"allowed_chat_ids": [True]}, "$.allowed_chat_ids[0]", None),
        ({"allowed_chat_ids": "123"}, "$.allowed_chat_ids", None),
        ({"mention_patterns": ["[invalid"]}, "$.mention_patterns[0]", None),
        ({"mention_patterns": ["  "]}, "$.mention_patterns[0]", "must be a non-empty string"),
        ({"mention_patterns": None}, "$.mention_patterns", None),
        ({"enabled": "yes"}, "$.enabled", "must be a boolean"),
        ({"observe_unaddressed": "true"}, "$.observe_unaddressed", "must be a boolean"),
        ({"platform": "unknown"}, "$.platform", None),
        ({"response_mode": "sometimes"}, "$.response_mode", "must be one of: all, mention"),
        (
            {"platform": "whatsapp", "token_env_var": "", "allowed_chat_ids": ["someone"]},
            "$.allowed_chat_ids",
            "WhatsApp supports only the self chat: use ['self'] or []",
        ),
        (
            {"platform": "whatsapp"},
            "$",
            "WhatsApp uses linked-device pairing, not tokens or a server URL",
        ),
    ],
)
def test_channel_config_validation_agrees_across_all_entry_points(
    tmp_path: Path, overrides: dict[str, Any], path: str, message: str | None
) -> None:
    payload = make_config_payload(**overrides)

    diagnostics = [
        (item.severity, item.path, item.message) for item in validate_channel_data(payload)
    ]
    assert [(severity, where) for severity, where, _ in diagnostics] == [("error", path)]
    if message is not None:
        assert diagnostics[0][2] == message
    with pytest.raises(ChannelConfigError):
        ChannelConfig.from_dict(payload)
    storage = ChannelStorage(tmp_path)
    with pytest.raises(ChannelConfigError):
        storage.save(ChannelConfig(**payload))
    assert not (tmp_path / "channels").exists()
    # A hand-edited channel.json holding the same value fails on read.
    _write_channel_json(tmp_path, {"format_version": 1, **payload})
    with pytest.raises(ChannelConfigError):
        storage.get("tg-assistant")


def test_channel_config_validation_normalizes_accepted_values_consistently(tmp_path: Path) -> None:
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


def test_channel_storage_crud_round_trip(tmp_path: Path) -> None:
    storage = ChannelStorage(tmp_path)
    initial = make_config(allowed_chat_ids=[])
    updated = replace(initial, allowed_chat_ids=["12345"], enabled=False)
    assert storage.load_all() == []

    storage.save(initial)
    loaded = storage.get(initial.id)
    listed = storage.load_all()
    storage.save(updated)
    reloaded = storage.get(initial.id)
    storage.delete(initial.id)

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
    config_path = _write_channel_json(
        tmp_path,
        {
            "format_version": 1,
            **make_config_payload(allowed_chat_ids=["-100"], owner_user_ids=[50]),
        },
    )

    config = storage.get("tg-assistant")
    assert [loaded.id for loaded in storage.load_all()] == ["tg-assistant"]
    storage.save(replace(config, enabled=False))

    rewritten = json.loads(config_path.read_text(encoding="utf-8"))
    assert rewritten["format_version"] == 1
    assert rewritten["enabled"] is False
    assert rewritten["owner_user_ids"] == [50]
    assert not (config_path.parent / "access.json").exists()


@pytest.mark.parametrize("stored", ["{", '{"format_version": 2}'])
def test_channel_config_that_fails_to_load_is_never_overwritten(
    tmp_path: Path, stored: str
) -> None:
    config_path = _write_channel_json(tmp_path, stored)

    with pytest.raises(ChannelConfigError, match="Refusing to overwrite Channel config"):
        ChannelStorage(tmp_path).save(make_config())
    assert config_path.read_text(encoding="utf-8") == stored


def test_validate_channel_document_requires_the_current_format_version() -> None:
    payload = make_config_payload()
    assert [item.path for item in validate_channel_document(payload)] == ["$.format_version"]
    assert validate_channel_document({"format_version": 1, **payload}) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_id",
    ["../agents", "..", "foo/bar", "a\\b", "/etc", ".", "tg/../x", "tg-assistant/"],
)
async def test_channel_ids_with_path_components_never_reach_the_filesystem(
    tmp_path: Path, bad_id: str
) -> None:
    # A channel id is a storage path segment and delete removes its directory:
    # separators and traversal are refused before any filesystem access.
    storage = ChannelStorage(tmp_path)
    (tmp_path / "channels").mkdir()
    sibling = tmp_path / "agents"
    sibling.mkdir()
    sibling.joinpath("keep.txt").write_text("important", encoding="utf-8")
    service = make_service(tmp_path)
    try:
        with pytest.raises(ChannelConfigError):
            storage.get(bad_id)
        with pytest.raises(ChannelConfigError):
            storage.delete(bad_id)
        # The same guard holds one layer up, where channel.delete RPC enters.
        with pytest.raises(ChannelConfigError):
            await service.delete_channel(bad_id)
    finally:
        service.close()

    assert sibling.joinpath("keep.txt").read_text(encoding="utf-8") == "important"


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


def test_managed_channel_token_env_var_is_safe_and_collision_free() -> None:
    assert managed_channel_token_env_var("tg-main") == "VBOT_CHANNEL_TOKEN__74672D6D61696E"
    assert managed_channel_token_env_var("a-b") != managed_channel_token_env_var("a_b")
    assert managed_channel_token_env_var("main") != managed_channel_token_env_var("MAIN")
