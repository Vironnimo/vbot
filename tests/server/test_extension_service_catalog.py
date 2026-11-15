"""The Extension facade is a documented, audited entrance to canonical owners."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.calendar import CalendarService
from core.extensions import ExtensionRegistrationIdentity, ExtensionRegistry
from core.extensions.services import ExtensionServiceCatalog, ExtensionServiceError
from core.runtime.runtime import Runtime
from core.statistics import StatisticsIndex
from core.utils.config import Config
from server.events import ServerEventBus
from server.extension_services import (
    SERVICE_DEFINITIONS,
    SERVICE_EXCLUSIONS,
    audit_application_services,
    bind_application_services,
)
from server.rpc.methods import METHODS
from tests.core.automation.cron_test_support import make_service
from tests.core.extensions.extension_test_support import write_extension
from tests.server.rpc_test_support import StubAdapter, make_state, resource_changes


def _bind(state: Any) -> tuple[Any, Any]:
    close = bind_application_services(state, METHODS)
    services = state.runtime.extension_services.for_owner(
        ExtensionRegistrationIdentity("catalog-test", "one"), lambda: None
    )
    return services, close


def test_every_rpc_has_an_explicit_public_contract_or_a_discoverable_exclusion() -> None:
    audit_application_services(METHODS)
    with pytest.raises(ValueError):
        audit_application_services({**METHODS, "future.unreviewed": METHODS["agent.list"]})
    with pytest.raises(ValueError):
        audit_application_services(
            {name: handler for name, handler in METHODS.items() if name != "agent.list"}
        )
    state = SimpleNamespace(runtime=SimpleNamespace(extension_services=ExtensionServiceCatalog()))
    services, close = _bind(state)
    try:
        described = services.describe()
        assert {entry["name"] for entry in described["services"]} == {
            row[0] for row in SERVICE_DEFINITIONS
        }
        assert {
            entry["name"]: entry["reason"] for entry in described["excluded_services"]
        } == SERVICE_EXCLUSIONS
        assert all(entry["reason"] for entry in described["excluded_services"])
        assert all(
            entry["parameters"]["additionalProperties"] is False
            and entry["parameters"]["description"]
            for entry in described["services"]
        )
    finally:
        close()


@pytest.mark.asyncio
async def test_custom_resources_refresh_browser_pages_without_exporting_private_scopes(
    tmp_path: Path,
) -> None:
    write_extension(
        tmp_path,
        "provider",
        "def register(api):\n"
        "    api.register_resource('items')\n"
        "    api.register_service('read', 'query', {'type': 'object'}, "
        "lambda params: {'items': [1]}, resources=['items'])\n",
    )
    registry = await ExtensionRegistry.aload(tmp_path)
    catalog = ExtensionServiceCatalog()
    catalog.install_exports(registry)
    state = SimpleNamespace(
        runtime=SimpleNamespace(extension_services=catalog), event_bus=ServerEventBus()
    )
    consumer, close = _bind(state)
    publisher = catalog.for_owner(registry.registration_identity("provider"), lambda: None)
    try:
        # Installing the app binding preserves already registered Extension exports.
        assert await consumer.query("extension:provider:read") == {"items": [1]}
        publisher.publish("items", {"record": "private-record"})
        assert resource_changes(state) == [{"kind": "extensions"}]
    finally:
        close()
        catalog.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "arguments", "path"),
    [
        ("memory.add", {"agent_id": "coder", "scope": "agent"}, ["content"]),
        (
            "settings.patch",
            {
                "operations": [
                    {"op": "set", "path": "appearance.language", "value": "de", "typo": True}
                ]
            },
            ["operations", 0],
        ),
        (
            "settings.update",
            {"appearance": {"ui_layout": {"chat.composer": {"hidden": [4]}}}},
            ["appearance", "ui_layout"],
        ),
        (
            "provider.custom_save",
            {
                "provider": {
                    "id": "local-test",
                    "adapter": "openai_compatible",
                    "name": "Local",
                    "base_url": "http://localhost/v1",
                    "models": {"model": {"context_window": 0}},
                }
            },
            ["provider", "models", "model", "context_window"],
        ),
        ("statistics.report", {"sections": ["not-a-section"]}, ["sections"]),
        (
            "calendar.create",
            {"title": "Event", "start": "2026-11-15", "timeznoe": "UTC"},
            ["timeznoe"],
        ),
    ],
)
async def test_schema_errors_identify_fields_before_any_canonical_handler_runs(
    method: str, arguments: dict[str, Any], path: list[str | int]
) -> None:
    catalog = ExtensionServiceCatalog()
    state = SimpleNamespace(runtime=SimpleNamespace(extension_services=catalog))
    called: list[str] = []

    async def handler(_state: Any, _params: dict[str, Any]) -> dict[str, Any]:
        called.append("unexpected")
        return {}

    close = bind_application_services(
        state, dict.fromkeys((entry[0] for entry in SERVICE_DEFINITIONS), handler)
    )
    services = catalog.for_owner(ExtensionRegistrationIdentity("catalog-test", "one"), lambda: None)
    kind = next(row[1] for row in SERVICE_DEFINITIONS if row[0] == method)
    try:
        with pytest.raises(ExtensionServiceError) as error:
            await getattr(services, kind)(method, arguments)
        assert error.value.code == "invalid_request"
        assert error.value.data["path"][: len(path)] == path
        assert error.value.data["validator"]
        assert called == []
    finally:
        close()


@pytest.mark.asyncio
async def test_memory_services_preserve_canonical_content_and_validation(
    tmp_path: Path,
) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.agents.update("coder", workspace=str(tmp_path / "workspace"))
    services, close = _bind(state)
    try:
        added = await services.execute(
            "memory.add",
            {"agent_id": "coder", "scope": "agent", "content": "Remember the contract."},
        )
        assert added["entry"]["content"] == "Remember the contract."
        assert (await services.query("memory.list", {"agent_id": "coder"}))["scopes"]["agent"] == [
            added["entry"]
        ]
        assert resource_changes(state)[-1] == {"kind": "memories", "scope": {"agent_id": "coder"}}
        # Semantic content validation still comes from the owner after schema validation.
        with pytest.raises(ExtensionServiceError) as unknown:
            await services.execute(
                "memory.add", {"agent_id": "coder", "scope": "agent", "content": "   "}
            )
        assert unknown.value.code == "domain_error"

    finally:
        close()


@pytest.mark.asyncio
async def test_calendar_and_cron_services_share_real_scheduling_and_event_owners(
    tmp_path: Path,
) -> None:
    state = make_state(tmp_path, StubAdapter())
    calendar = CalendarService(tmp_path, tz="UTC")
    cron, _trigger = make_service(
        tmp_path,
        agent_resolver=state.runtime.agent_resolver,
        sessions=state.runtime.chat_sessions,
        calendar=calendar,
        tz="UTC",
    )
    state.runtime.calendar_service = calendar
    state.runtime.cron_service = cron
    services, close = _bind(state)
    try:
        event = (
            await services.execute(
                "calendar.create", {"title": "Release", "start": "2099-11-15T12:00:00+00:00"}
            )
        )["event"]
        created = await services.execute(
            "cron.create",
            {
                "agent_id": "coder",
                "prompt": "Review release",
                "schedule_type": "event",
                "event_id": event["id"],
                "event_time": "start",
            },
        )
        job_id = created["id"]
        window = await services.query("calendar.window", {"from": "2099-11-15", "to": "2099-11-16"})
        assert [item["event_id"] for item in window["occurrences"]] == [event["id"]]
        assert [item["job_id"] for item in window["cron"]] == [job_id]
        deleted = await services.execute("calendar.delete", {"id": event["id"]})
        assert [item["id"] for item in deleted["cron_jobs"]] == [job_id]
        assert (await services.query("cron.list"))["jobs"] == []
    finally:
        close()
        cron.stop()


@pytest.mark.asyncio
async def test_service_settings_provider_and_channel_changes_keep_live_conflict_and_secret_rules(
    tmp_path: Path,
) -> None:
    runtime = Runtime(Config(data_dir=tmp_path / "data"), safe_startup_mode="test")
    runtime.start()
    state = SimpleNamespace(
        runtime=runtime,
        event_bus=ServerEventBus(),
        agent_delete_lock=asyncio.Lock(),
        server_bind={"listen_host": "127.0.0.1", "listen_port": 8420, "port_source": "default"},
    )
    services, close = _bind(state)
    try:
        provider = {
            "id": "service-local",
            "name": "Service local",
            "adapter": "openai_compatible",
            "base_url": "http://127.0.0.1:9999/v1",
            "auth": "none",
            "models": {"tiny": {"context_window": 4096}},
        }
        first = (await services.execute("provider.custom_save", {"provider": provider}))["provider"]
        updated = {**provider, "name": "Updated", "wire": None}
        await services.execute(
            "provider.custom_save", {"provider": updated, "expected_revision": first["revision"]}
        )
        with pytest.raises(ExtensionServiceError) as conflict:
            await services.execute(
                "provider.custom_save",
                {"provider": provider, "expected_revision": first["revision"]},
            )
        assert conflict.value.code == "settings_conflict"
        assert (await services.query("provider.custom_list"))["providers"][0]["name"] == "Updated"
        with pytest.raises(ExtensionServiceError) as secret:
            await services.execute(
                "provider.custom_save", {"provider": provider, "api_key": "must-not-write"}
            )
        assert secret.value.code == "invalid_request"

        layout = {
            "chat.composer": {
                "hidden": ["builtin:composer.context"],
                "replacements": {"context": ""},
            }
        }
        await services.execute(
            "settings.update",
            {"appearance": {"ui_layout": layout}, "base": {"appearance": {"ui_layout": None}}},
        )
        with pytest.raises(ExtensionServiceError) as stale_layout:
            await services.execute(
                "settings.update",
                {"appearance": {"ui_layout": {}}, "base": {"appearance": {"ui_layout": None}}},
            )
        assert stale_layout.value.code == "settings_conflict"
        assert (await services.query("settings.get_path", {"path": "appearance.ui_layout"}))[
            "setting"
        ]["value"] == layout

        runtime.agents.create("service-agent", "Service Agent")
        session = runtime.chat_sessions.create("service-agent", session_id="restorable")
        await runtime.archive.archive_session(session.address)
        [entry] = (await services.query("archive.list", {"kind": "session"}))["entries"]
        restored = await services.execute("archive.restore", {"entry_id": entry["entry_id"]})
        assert restored["restored"]["session_id"] == "restorable"
        assert runtime.chat_sessions.summary(session.address) is not None
        channel = await services.execute(
            "channel.create",
            {
                "id": "service-channel",
                "platform": "telegram",
                "agent_id": "service-agent",
                "enabled": False,
                "token_env_var": "SERVICE_TEST_TOKEN",
            },
        )
        assert channel["id"] == "service-channel"
        await services.execute("channel.update", {"id": "service-channel", "response_mode": "all"})
        assert (await runtime.channel_service.get_channel("service-channel")).response_mode == "all"
        await services.execute("channel.delete", {"id": "service-channel"})
        assert await runtime.channel_service.list_channels_async() == []
    finally:
        close()
        await runtime.aclose()


@pytest.mark.asyncio
async def test_permission_preview_and_statistics_period_use_canonical_services(
    tmp_path: Path,
) -> None:
    state = make_state(tmp_path, StubAdapter())
    index = StatisticsIndex(tmp_path)
    state.runtime.statistics_index = index
    services, close = _bind(state)
    try:
        checked = await services.query(
            "permissions.validate", {"permission_rules": {"read": "invalid-rule"}}
        )
        assert checked["valid"] is False
        assert checked["errors"]
        checked = await services.query("permissions.validate", {"permission_rules": None})
        assert checked["valid"] is True
        started = await services.execute("statistics.period.start")
        assert await services.query("statistics.period") == started
        cleared = await services.execute("statistics.period.clear")
        assert cleared != started
        assert resource_changes(state)[-1] == {"kind": "statistics"}
    finally:
        close()
        index.close()
