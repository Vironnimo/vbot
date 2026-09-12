"""Bridge: models behavior."""

from __future__ import annotations

import base64
import json
import threading
from pathlib import Path

import pytest

from desktop.wakeword import bridge as bridge_module
from desktop.wakeword import engine as engine_module
from desktop.wakeword.bridge import DesktopBridge
from desktop.wakeword.engine import (
    DEFAULT_WAKEWORD_MODEL_IDS,
    WakewordModelDescriptor,
    WakewordModelError,
)
from tests.desktop.bridge_helpers import (
    FakeWorker,
    _write_settings,
)


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
    with pytest.raises(WakewordModelError):
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

    with pytest.raises(WakewordModelError):
        bridge.importWakewordModel("bad.tflite", "%%%")
    monkeypatch.setattr(bridge_module, "_MAX_CUSTOM_WAKEWORD_MODEL_BASE64_CHARS", 8)
    with pytest.raises(WakewordModelError):
        bridge.importWakewordModel("large.tflite", "a" * 9)
    with pytest.raises(WakewordModelError):
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


def test_disable_during_import_is_applied_after_import_restart(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    _write_settings(
        settings_file,
        {
            "enabled": True,
            "active_model_ids": [DEFAULT_WAKEWORD_MODEL_IDS[0]],
        },
    )
    restart_entered = threading.Event()
    release_restart = threading.Event()
    workers: list[FakeWorker] = []

    class ImportCatalog:
        @staticmethod
        def import_model(_filename: str, _content: bytes) -> WakewordModelDescriptor:
            return WakewordModelDescriptor(
                id="custom/model",
                label="Model",
                source="imported",
                format="tflite",
                removable=True,
                target="model.tflite",
            )

    def worker_factory(_bridge: DesktopBridge) -> FakeWorker:
        worker = FakeWorker()
        workers.append(worker)
        return worker

    bridge = DesktopBridge(
        settings_path=settings_file,
        model_catalog=ImportCatalog(),
        worker_factory=worker_factory,
    )
    original_restart = bridge._restart_worker

    def blocking_restart(enabled: bool) -> None:
        restart_entered.set()
        assert release_restart.wait(timeout=1)
        original_restart(enabled)

    bridge._restart_worker = blocking_restart  # type: ignore[method-assign]
    import_thread = threading.Thread(
        target=bridge.importWakewordModel,
        args=("model.tflite", base64.b64encode(b"model").decode()),
    )
    disable_thread = threading.Thread(target=bridge.setWakewordEnabled, args=(False,))

    import_thread.start()
    assert restart_entered.wait(timeout=1)
    disable_thread.start()

    assert disable_thread.is_alive()
    release_restart.set()
    import_thread.join(timeout=1)
    disable_thread.join(timeout=1)

    assert len(workers) == 1
    assert workers[0].stopped is True
    assert bridge.getWakewordStatus()["enabled"] is False


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
