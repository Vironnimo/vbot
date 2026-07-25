"""Tests for DesktopBridge API shape and state management."""

from __future__ import annotations

import base64
import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from desktop.connection import PreparedConnection, ServerEntry
from desktop.main import DesktopProbeResult, DesktopTarget
from desktop.wakeword import bridge as bridge_module
from desktop.wakeword import engine as engine_module
from desktop.wakeword.bridge import DesktopBridge
from desktop.wakeword.engine import (
    DEFAULT_WAKEWORD_MODEL_IDS,
    WakewordModelDescriptor,
    WakewordModelError,
)


def _write_settings(path: Path, wakeword_config: dict | None = None) -> None:
    data = {"host": "127.0.0.1", "port": 8420}
    if wakeword_config is not None:
        data["wakeword"] = wakeword_config
    path.write_text(json.dumps(data), encoding="utf-8")


class FakeWorker:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True
        self.started = False

    def is_running(self) -> bool:
        return self.started


@dataclass
class FakeController:
    """Records the controller calls the bridge connection methods delegate to.

    Stands in for :class:`desktop.connection.ConnectionController` so the bridge
    can be tested without a pywebview window: it captures the host/port (and the
    Python types they arrive as) and returns canned results.
    """

    connect_status: str = "webui_available"
    prepare_calls: list[tuple[str, Any]] = field(default_factory=list)
    add_calls: list[tuple[str, Any, str | None]] = field(default_factory=list)
    remove_calls: list[tuple[str, Any]] = field(default_factory=list)
    servers: list[ServerEntry] = field(default_factory=list)
    remove_result: bool = True

    def prepare_connect(self, host: str, port: Any, label: str | None = None) -> PreparedConnection:
        self.prepare_calls.append((host, port))
        target = DesktopTarget(host=str(host), port=port if isinstance(port, int) else 0, url="")
        result = DesktopProbeResult(status=self.connect_status, target=target)
        if self.connect_status == "webui_available":
            return PreparedConnection(
                result=result,
                navigation_url="http://pi.lan:9000/?accessor=desktop",
            )
        return PreparedConnection(
            result=result,
            error_title="Server unreachable",
            error_body="Try again.",
        )

    def add_server(self, host: str, port: Any, label: str | None = None) -> ServerEntry:
        self.add_calls.append((host, port, label))
        return ServerEntry(host=host, port=port, label=label)

    def remove_server(self, host: str, port: Any) -> bool:
        self.remove_calls.append((host, port))
        return self.remove_result

    def list_servers(self) -> list[ServerEntry]:
        return list(self.servers)


def test_get_desktop_capabilities(tmp_path: Path) -> None:
    _write_settings(tmp_path / "settings.json")

    bridge = DesktopBridge(settings_path=tmp_path / "settings.json")

    capabilities = bridge.getDesktopCapabilities()

    assert capabilities == {"wakeword": True, "serverSelection": True}


def test_get_wakeword_status_shape(tmp_path: Path) -> None:
    _write_settings(
        tmp_path / "settings.json",
        {
            "enabled": True,
            "model_sensitivities": {DEFAULT_WAKEWORD_MODEL_IDS[0]: 0.7},
        },
    )

    bridge = DesktopBridge(settings_path=tmp_path / "settings.json")
    status = bridge.getWakewordStatus()

    assert status["enabled"] is True
    assert status["model_sensitivities"] == {
        DEFAULT_WAKEWORD_MODEL_IDS[0]: 0.7,
        DEFAULT_WAKEWORD_MODEL_IDS[1]: 0.5,
    }
    assert status["state"] == "off"
    assert "engine" in status
    assert "microphone" in status
    assert "target_agent_id" in status
    assert "session_behavior" in status
    assert status["active_model_ids"] == list(DEFAULT_WAKEWORD_MODEL_IDS)
    assert status["engine"] == "pyopen_wakeword"


def test_get_wakeword_status_includes_mock_flag(tmp_path: Path) -> None:
    _write_settings(tmp_path / "settings.json")

    real = DesktopBridge(settings_path=tmp_path / "settings.json")
    assert real.getWakewordStatus()["mock"] is False

    mock = DesktopBridge(settings_path=tmp_path / "settings.json", mock=True)
    assert mock.getWakewordStatus()["mock"] is True


def test_server_url_normalizes_trailing_slash(tmp_path: Path) -> None:
    _write_settings(tmp_path / "settings.json")

    bridge = DesktopBridge(
        settings_path=tmp_path / "settings.json",
        server_url="http://pi.lan:9000/",
    )

    assert bridge.server_url == "http://pi.lan:9000"


def test_set_server_url_rebuilds_running_worker(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    _write_settings(settings_file, {"enabled": True})
    workers: list[FakeWorker] = []

    def worker_factory(bridge: DesktopBridge) -> FakeWorker:
        worker = FakeWorker()
        # Record the URL the factory saw so we can prove it reads the current one.
        worker.server_url = bridge.server_url  # type: ignore[attr-defined]
        workers.append(worker)
        return worker

    bridge = DesktopBridge(
        settings_path=settings_file,
        worker_factory=worker_factory,
        server_url="http://a.lan:8420/",
    )
    bridge.setWakewordEnabled(True)
    assert workers[0].server_url == "http://a.lan:8420"  # type: ignore[attr-defined]

    bridge.set_server_url("http://b.lan:9000/")

    # The running worker is rebuilt against the new server.
    assert len(workers) == 2
    assert workers[0].stopped is True
    assert workers[1].started is True
    assert workers[1].server_url == "http://b.lan:9000"  # type: ignore[attr-defined]


def test_set_server_url_noop_when_unchanged(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    _write_settings(settings_file, {"enabled": True})
    workers: list[FakeWorker] = []

    def worker_factory(_bridge: DesktopBridge) -> FakeWorker:
        worker = FakeWorker()
        workers.append(worker)
        return worker

    bridge = DesktopBridge(
        settings_path=settings_file,
        worker_factory=worker_factory,
        server_url="http://a.lan:8420/",
    )
    bridge.setWakewordEnabled(True)

    # Same target (trailing slash normalized away) → no needless rebuild, so the
    # launch auto-connect to the already-open server does not restart the worker.
    bridge.set_server_url("http://a.lan:8420")

    assert len(workers) == 1


def test_set_server_url_stores_for_next_start_when_no_worker_running(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    _write_settings(settings_file)  # wakeword disabled → nothing running
    workers: list[FakeWorker] = []

    def worker_factory(bridge: DesktopBridge) -> FakeWorker:
        worker = FakeWorker()
        worker.server_url = bridge.server_url  # type: ignore[attr-defined]
        workers.append(worker)
        return worker

    bridge = DesktopBridge(
        settings_path=settings_file,
        worker_factory=worker_factory,
        server_url="",
    )

    # First-run shape: connect happens before voice is enabled. Nothing is built
    # now, but the URL is stored so the next start targets the right server.
    bridge.set_server_url("http://pi.lan:9000/")
    assert workers == []
    assert bridge.server_url == "http://pi.lan:9000"

    bridge.setWakewordEnabled(True)
    assert workers[0].server_url == "http://pi.lan:9000"  # type: ignore[attr-defined]


def test_set_wakeword_enabled_toggles_and_persists(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    _write_settings(settings_file)

    bridge = DesktopBridge(
        settings_path=settings_file,
        server_url="http://127.0.0.1:8420",
    )

    bridge.setWakewordEnabled(True)
    status = bridge.getWakewordStatus()
    assert status["enabled"] is True

    bridge.setWakewordEnabled(False)
    status = bridge.getWakewordStatus()
    assert status["enabled"] is False


def test_set_wakeword_enabled_rejects_missing_speech_to_text_before_persisting(
    tmp_path: Path,
) -> None:
    settings_file = tmp_path / "settings.json"
    _write_settings(settings_file)
    worker = FakeWorker()
    bridge = DesktopBridge(
        settings_path=settings_file,
        worker=worker,
        server_url="http://127.0.0.1:8420",
        speech_readiness_checker=lambda _server_url: "speech_to_text_unconfigured",
    )

    result = bridge.setWakewordEnabled(True)

    assert result == {
        "enabled": False,
        "error_code": "speech_to_text_unconfigured",
    }
    assert bridge.getWakewordStatus()["enabled"] is False
    assert bridge.getWakewordStatus()["state"] == "error"
    assert bridge.getWakewordStatus()["error_code"] == "speech_to_text_unconfigured"
    assert worker.started is False


def test_set_wakeword_enabled_uses_worker_factory(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    _write_settings(settings_file)
    workers: list[FakeWorker] = []

    def worker_factory(_bridge: DesktopBridge) -> FakeWorker:
        worker = FakeWorker()
        workers.append(worker)
        return worker

    bridge = DesktopBridge(settings_path=settings_file, worker_factory=worker_factory)

    bridge.setWakewordEnabled(True)

    assert len(workers) == 1
    assert workers[0].started is True
    assert bridge.getWakewordStatus()["enabled"] is True


def test_set_wakeword_config_recreates_running_worker(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    _write_settings(settings_file, {"enabled": True})
    workers: list[FakeWorker] = []

    def worker_factory(_bridge: DesktopBridge) -> FakeWorker:
        worker = FakeWorker()
        workers.append(worker)
        return worker

    bridge = DesktopBridge(settings_path=settings_file, worker_factory=worker_factory)
    bridge.setWakewordEnabled(True)

    bridge.setWakewordConfig({"model_sensitivities": {DEFAULT_WAKEWORD_MODEL_IDS[0]: 0.9}})

    assert len(workers) == 2
    assert workers[0].stopped is True
    assert workers[1].started is True
    assert bridge.getWakewordStatus()["model_sensitivities"][DEFAULT_WAKEWORD_MODEL_IDS[0]] == 0.9


def test_set_wakeword_config_partial_update(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    _write_settings(settings_file)

    bridge = DesktopBridge(
        settings_path=settings_file,
        server_url="http://127.0.0.1:8420",
    )
    bridge.setWakewordConfig(
        {
            "model_sensitivities": {DEFAULT_WAKEWORD_MODEL_IDS[0]: 0.9},
            "target_agent_id": "agent-1",
        }
    )

    status = bridge.getWakewordStatus()
    assert status["model_sensitivities"][DEFAULT_WAKEWORD_MODEL_IDS[0]] == 0.9
    assert status["target_agent_id"] == "agent-1"


def test_bridge_imports_selects_and_deletes_a_custom_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_file = tmp_path / "settings.json"
    _write_settings(settings_file)
    monkeypatch.setattr(engine_module, "_validate_custom_model", lambda _path: None)
    bridge = DesktopBridge(settings_path=settings_file)
    bridge.setWakewordConfig({"active_model_ids": [DEFAULT_WAKEWORD_MODEL_IDS[0]]})

    imported = bridge.importWakewordModel(
        "hey_computer.tflite",
        base64.b64encode(b"tflite-model").decode("ascii"),
    )
    bridge.setWakewordConfig({"model_sensitivities": {imported["id"]: 0.75}})

    assert imported["label"] == "hey computer"
    assert imported["removable"] is True
    assert imported["activated"] is True
    assert any(model["id"] == imported["id"] for model in bridge.listWakewordModels())
    assert bridge.getWakewordStatus()["active_model_ids"] == [
        DEFAULT_WAKEWORD_MODEL_IDS[0],
        imported["id"],
    ]
    assert bridge.getWakewordStatus()["model_sensitivities"][imported["id"]] == 0.75
    engine = bridge._create_wakeword_engine()
    assert isinstance(engine, engine_module.MultiWakewordEngine)
    assert engine.active_model_ids == (DEFAULT_WAKEWORD_MODEL_IDS[0], imported["id"])
    with pytest.raises(WakewordModelError, match="active"):
        bridge.deleteWakewordModel(imported["id"])

    bridge.setWakewordConfig({"active_model_ids": list(DEFAULT_WAKEWORD_MODEL_IDS)})
    assert bridge.deleteWakewordModel(imported["id"]) == {"deleted": True}
    assert imported not in bridge.listWakewordModels()
    stored = json.loads(settings_file.read_text(encoding="utf-8"))["wakeword"]
    assert imported["id"] not in stored["model_sensitivities"]


def test_bridge_rejects_invalid_model_content_and_unknown_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_file = tmp_path / "settings.json"
    _write_settings(settings_file)
    bridge = DesktopBridge(settings_path=settings_file)

    with pytest.raises(WakewordModelError, match="base64"):
        bridge.importWakewordModel("bad.tflite", "%%%")
    monkeypatch.setattr(bridge_module, "_MAX_CUSTOM_WAKEWORD_MODEL_BASE64_CHARS", 8)
    with pytest.raises(WakewordModelError, match="size limit"):
        bridge.importWakewordModel("large.tflite", "a" * 9)
    with pytest.raises(WakewordModelError, match="not available"):
        bridge.setWakewordConfig({"active_model_ids": ["custom/missing"]})


@pytest.mark.parametrize(
    "active_model_ids",
    [[], ["builtin/okay_nabu", "builtin/okay_nabu"], ["a", "b", "c"]],
)
def test_bridge_rejects_invalid_active_model_sets(
    tmp_path: Path, active_model_ids: list[str]
) -> None:
    bridge = DesktopBridge(settings_path=tmp_path / "settings.json")

    with pytest.raises(WakewordModelError):
        bridge.setWakewordConfig({"active_model_ids": active_model_ids})


def test_import_leaves_model_inactive_when_both_slots_are_occupied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_file = tmp_path / "settings.json"
    _write_settings(settings_file)
    monkeypatch.setattr(engine_module, "_validate_custom_model", lambda _path: None)
    bridge = DesktopBridge(settings_path=settings_file)

    imported = bridge.importWakewordModel(
        "computer.tflite", base64.b64encode(b"model").decode("ascii")
    )

    assert imported["activated"] is False
    assert bridge.getWakewordStatus()["active_model_ids"] == list(DEFAULT_WAKEWORD_MODEL_IDS)


def test_model_import_validation_does_not_block_status_polling(tmp_path: Path) -> None:
    import_started = threading.Event()
    release_import = threading.Event()
    errors: list[Exception] = []

    class BlockingCatalog:
        def import_model(self, _filename: str, _content: bytes) -> WakewordModelDescriptor:
            import_started.set()
            release_import.wait(timeout=2)
            return WakewordModelDescriptor(
                id="custom/model",
                label="Model",
                source="imported",
                format="tflite",
                removable=True,
                target="model.tflite",
            )

    bridge = DesktopBridge(
        settings_path=tmp_path / "settings.json",
        model_catalog=BlockingCatalog(),
    )

    def import_model() -> None:
        try:
            bridge.importWakewordModel("model.tflite", base64.b64encode(b"model").decode())
        except Exception as exc:  # pragma: no cover - asserted through the shared list
            errors.append(exc)

    import_thread = threading.Thread(target=import_model)
    import_thread.start()
    assert import_started.wait(timeout=1)

    status_thread = threading.Thread(target=bridge.getWakewordStatus)
    status_thread.start()
    status_thread.join(timeout=0.2)
    release_import.set()
    import_thread.join(timeout=1)
    status_thread.join(timeout=1)

    assert not status_thread.is_alive()
    assert not import_thread.is_alive()
    assert errors == []


def test_sensitivity_is_preserved_per_wakeword_model(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    _write_settings(settings_file)
    bridge = DesktopBridge(settings_path=settings_file)

    bridge.setWakewordConfig(
        {
            "model_sensitivities": {
                DEFAULT_WAKEWORD_MODEL_IDS[0]: 0.8,
                DEFAULT_WAKEWORD_MODEL_IDS[1]: 0.35,
            }
        }
    )

    assert bridge.getWakewordStatus()["model_sensitivities"] == {
        DEFAULT_WAKEWORD_MODEL_IDS[0]: 0.8,
        DEFAULT_WAKEWORD_MODEL_IDS[1]: 0.35,
    }


def test_worker_factory_model_error_becomes_actionable_status(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    _write_settings(settings_file)

    def worker_factory(_bridge: DesktopBridge) -> FakeWorker:
        raise WakewordModelError(
            "missing model",
            error_code="wakeword_model_unavailable",
        )

    bridge = DesktopBridge(settings_path=settings_file, worker_factory=worker_factory)

    bridge.setWakewordEnabled(True)

    status = bridge.getWakewordStatus()
    assert status["state"] == "error"
    assert status["error_code"] == "wakeword_model_unavailable"


def test_voice_target_profile_is_isolated_per_server(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    _write_settings(settings_file)
    bridge = DesktopBridge(
        settings_path=settings_file,
        server_url="http://a.lan:8420",
    )

    bridge.setWakewordConfig({"target_agent_id": "home", "session_behavior": "active"})
    bridge.set_server_url("http://b.lan:8420")
    assert bridge.getWakewordStatus()["target_agent_id"] is None

    bridge.setWakewordConfig({"target_agent_id": "office", "session_behavior": "new"})
    bridge.set_server_url("http://a.lan:8420")
    status = bridge.getWakewordStatus()

    assert status["target_agent_id"] == "home"
    assert status["session_behavior"] == "active"


def test_status_exposes_actionable_error_and_event_history(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    _write_settings(settings_file)
    bridge = DesktopBridge(settings_path=settings_file)

    bridge.publish_state("starting")
    bridge.publish_state("error", "microphone_unavailable")

    status = bridge.getWakewordStatus()
    assert status["state"] == "error"
    assert status["error_code"] == "microphone_unavailable"
    assert status["events"][-2:] == [
        {"sequence": 1, "state": "starting", "error_code": None},
        {
            "sequence": 2,
            "state": "error",
            "error_code": "microphone_unavailable",
        },
    ]


def test_set_wakeword_config_rejects_non_dict(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    _write_settings(settings_file)

    bridge = DesktopBridge(settings_path=settings_file)
    # Non-dict input should be silently ignored
    bridge.setWakewordConfig({"not": "applicable"})

    status = bridge.getWakewordStatus()
    assert status["model_sensitivities"] == {
        DEFAULT_WAKEWORD_MODEL_IDS[0]: 0.5,
        DEFAULT_WAKEWORD_MODEL_IDS[1]: 0.5,
    }


def test_publish_state_updates_state_and_logs_transitions(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _write_settings(tmp_path / "settings.json")

    bridge = DesktopBridge(settings_path=tmp_path / "settings.json")

    with caplog.at_level("INFO", logger="vbot.desktop.wakeword.bridge"):
        bridge.publish_state("listening")
        assert bridge.getWakewordStatus()["state"] == "listening"

        bridge.publish_state("recording")
        assert bridge.getWakewordStatus()["state"] == "recording"

        bridge.publish_state("error", "microphone_unavailable")
        assert bridge.getWakewordStatus()["state"] == "error"

    assert "Voice state changed (sequence=1, from=off, to=listening)" in caplog.text
    assert "Voice state changed (sequence=2, from=listening, to=recording)" in caplog.text
    assert (
        "Voice state changed (sequence=3, from=recording, to=error, "
        "error_code=microphone_unavailable)"
    ) in caplog.text


def test_publish_state_does_not_log_unchanged_state(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _write_settings(tmp_path / "settings.json")
    bridge = DesktopBridge(settings_path=tmp_path / "settings.json")

    with caplog.at_level("INFO", logger="vbot.desktop.wakeword.bridge"):
        bridge.publish_state("listening")
        caplog.clear()
        bridge.publish_state("listening")

    assert "Voice state changed" not in caplog.text


def test_publish_state_rejects_invalid_state(tmp_path: Path) -> None:
    _write_settings(tmp_path / "settings.json")

    bridge = DesktopBridge(settings_path=tmp_path / "settings.json")

    with pytest.raises(ValueError, match="Invalid wakeword state"):
        bridge.publish_state("nonexistent")


def test_bridge_thread_safety_concurrent_access(tmp_path: Path) -> None:
    _write_settings(tmp_path / "settings.json")

    bridge = DesktopBridge(settings_path=tmp_path / "settings.json")
    errors: list[Exception] = []

    def reader() -> None:
        for _ in range(50):
            try:
                bridge.getWakewordStatus()
            except Exception as exc:
                errors.append(exc)

    def writer() -> None:
        for i in range(50):
            try:
                bridge.publish_state("listening" if i % 2 == 0 else "recording")
                bridge.setWakewordConfig(
                    {"model_sensitivities": {DEFAULT_WAKEWORD_MODEL_IDS[0]: 0.5 + (i % 9) * 0.05}}
                )
            except Exception as exc:
                errors.append(exc)

    threads = [threading.Thread(target=reader) for _ in range(3)] + [
        threading.Thread(target=writer) for _ in range(2)
    ]

    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5.0)

    assert len(errors) == 0


# -- Connection methods (server selection delegated to the controller) -------


def test_connect_prepares_controller_result_for_javascript_navigation(tmp_path: Path) -> None:
    _write_settings(tmp_path / "settings.json")
    controller = FakeController(connect_status="webui_available")
    bridge = DesktopBridge(settings_path=tmp_path / "settings.json", connection=controller)

    result = bridge.connect("pi.lan", 9000)

    assert controller.prepare_calls == [("pi.lan", 9000)]
    assert result == {
        "status": "webui_available",
        "url": "http://pi.lan:9000/?accessor=desktop",
    }


def test_connect_coerces_string_port_to_int(tmp_path: Path) -> None:
    _write_settings(tmp_path / "settings.json")
    controller = FakeController()
    bridge = DesktopBridge(settings_path=tmp_path / "settings.json", connection=controller)

    bridge.connect("pi.lan", "9000")

    # The screen may hand a string through; the controller receives a real int.
    assert controller.prepare_calls == [("pi.lan", 9000)]


def test_connect_passes_non_numeric_port_through_for_controller_validation(tmp_path: Path) -> None:
    _write_settings(tmp_path / "settings.json")
    controller = FakeController()
    bridge = DesktopBridge(settings_path=tmp_path / "settings.json", connection=controller)

    bridge.connect("pi.lan", "not-a-port")

    # Non-numeric input is left for the controller to reject with a clear message.
    assert controller.prepare_calls == [("pi.lan", "not-a-port")]


def test_connect_reports_failure_status(tmp_path: Path) -> None:
    _write_settings(tmp_path / "settings.json")
    controller = FakeController(connect_status="server_unreachable")
    bridge = DesktopBridge(settings_path=tmp_path / "settings.json", connection=controller)

    result = bridge.connect("pi.lan", 9000)

    assert result == {
        "status": "server_unreachable",
        "error_title": "Server unreachable",
        "error_body": "Try again.",
    }


def test_list_servers_returns_plain_payloads(tmp_path: Path) -> None:
    _write_settings(tmp_path / "settings.json")
    controller = FakeController(
        servers=[ServerEntry("pi.lan", 9000, "Pi"), ServerEntry("10.0.0.5", 8500)]
    )
    bridge = DesktopBridge(
        settings_path=tmp_path / "settings.json",
        connection=controller,
        server_url="http://pi.lan:9000/",
    )

    assert bridge.listServers() == [
        {"host": "pi.lan", "port": 9000, "label": "Pi", "active": True},
        {"host": "10.0.0.5", "port": 8500, "active": False},
    ]


def test_add_server_delegates_and_returns_entry(tmp_path: Path) -> None:
    _write_settings(tmp_path / "settings.json")
    controller = FakeController()
    bridge = DesktopBridge(settings_path=tmp_path / "settings.json", connection=controller)

    payload = bridge.addServer("pi.lan", "9000", "Pi")

    assert controller.add_calls == [("pi.lan", 9000, "Pi")]
    assert payload == {"host": "pi.lan", "port": 9000, "label": "Pi"}


def test_add_server_normalizes_empty_label_to_none(tmp_path: Path) -> None:
    _write_settings(tmp_path / "settings.json")
    controller = FakeController()
    bridge = DesktopBridge(settings_path=tmp_path / "settings.json", connection=controller)

    bridge.addServer("pi.lan", 9000, "")

    assert controller.add_calls == [("pi.lan", 9000, None)]


def test_remove_server_reports_outcome(tmp_path: Path) -> None:
    _write_settings(tmp_path / "settings.json")
    controller = FakeController(remove_result=True)
    bridge = DesktopBridge(settings_path=tmp_path / "settings.json", connection=controller)

    assert bridge.removeServer("pi.lan", 9000) == {"removed": True}
    assert controller.remove_calls == [("pi.lan", 9000)]


def test_select_server_prepares_bridge_safe_navigation(tmp_path: Path) -> None:
    _write_settings(tmp_path / "settings.json")
    controller = FakeController(connect_status="webui_available")
    bridge = DesktopBridge(settings_path=tmp_path / "settings.json", connection=controller)

    result = bridge.selectServer("pi.lan", 9000)

    assert controller.prepare_calls == [("pi.lan", 9000)]
    assert result == {
        "status": "webui_available",
        "url": "http://pi.lan:9000/?accessor=desktop",
    }


def test_connection_methods_raise_without_controller(tmp_path: Path) -> None:
    _write_settings(tmp_path / "settings.json")
    bridge = DesktopBridge(settings_path=tmp_path / "settings.json")

    with pytest.raises(RuntimeError, match="no connection controller"):
        bridge.connect("pi.lan", 9000)
    with pytest.raises(RuntimeError, match="no connection controller"):
        bridge.listServers()


def test_wakeword_and_connection_methods_share_one_bridge(tmp_path: Path) -> None:
    _write_settings(tmp_path / "settings.json", {"enabled": False})
    controller = FakeController()
    bridge = DesktopBridge(settings_path=tmp_path / "settings.json", connection=controller)

    # The same bridge object serves both surfaces (it is the window's single
    # js_api across load_url navigation): wakeword status and server connect.
    assert bridge.getWakewordStatus()["state"] == "off"
    assert bridge.getDesktopCapabilities() == {
        "wakeword": True,
        "serverSelection": True,
    }
    bridge.connect("pi.lan", 9000)
    assert controller.prepare_calls == [("pi.lan", 9000)]
