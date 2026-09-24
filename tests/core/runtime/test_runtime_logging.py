"""Tests for runtime logging."""

import json
import logging
import re
from pathlib import Path

import pytest

from core.providers.providers import ProviderRegistry
from core.runtime.runtime import Runtime
from core.utils.config import Config
from tests.core.runtime.runtime_test_support import (
    _authorize_session_store,
)
from tests.core.runtime.runtime_test_support import (
    config as config,
)


def _clear_provider_credential_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    resources_path = Path(__file__).resolve().parents[3] / "resources"
    provider_registry = ProviderRegistry.load(resources_path)
    seeded_credential_key: str | None = None

    for provider_id in provider_registry.list_ids():
        for connection in provider_registry.get(provider_id).connections:
            credential_key = connection.auth.credential_key
            if not credential_key:
                continue
            monkeypatch.delenv(credential_key, raising=False)
            if seeded_credential_key is None and connection.type == "api_key":
                seeded_credential_key = credential_key

    if seeded_credential_key is not None:
        monkeypatch.setenv(seeded_credential_key, "test-startup-credential")


def _expected_startup_inventory_message(runtime: Runtime) -> str:
    provider_ids = runtime.providers.list_ids()
    usable_provider_count = 0
    total_connection_count = 0
    usable_connection_count = 0

    for provider_id in provider_ids:
        provider_config = runtime.providers.get(provider_id)
        provider_is_usable = False

        for connection in provider_config.connections:
            total_connection_count += 1
            connection_id = f"{provider_id}:{connection.id}"
            if runtime.provider_credentials.is_usable(provider_id, connection_id):
                usable_connection_count += 1
                provider_is_usable = True

        if provider_is_usable:
            usable_provider_count += 1

    return (
        "Runtime inventory: "
        f"{len(runtime.tools.list_tools())} tools, "
        f"{len(runtime.skills.list_all())} skills, "
        f"{usable_provider_count}/{len(provider_ids)} usable providers, "
        f"{usable_connection_count}/{total_connection_count} usable connections"
    )


def test_runtime_logger_exists_after_start(tmp_path: Path):
    """After start(), runtime.logger is a valid logger object."""
    # Arrange
    logging.getLogger("vbot").handlers = []
    config = Config(data_dir=tmp_path / "data")
    runtime = Runtime(config)

    # Act
    runtime.start()

    # Assert
    logger = runtime.logger
    assert logger is not None
    assert hasattr(logger, "info")
    assert hasattr(logger, "error")
    assert hasattr(logger, "debug")
    # Verify it is a logging.Logger (the concrete implementation)
    assert isinstance(logger, logging.Logger)


def test_runtime_start_creates_date_named_log_file(config: Config) -> None:
    """Runtime logging writes to the active daily log file under the data dir."""
    runtime = Runtime(config)

    runtime.start()
    runtime.stop()

    log_files = list((config.data_dir / "logs").iterdir())
    assert len(log_files) == 1
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}\.log", log_files[0].name)


def test_runtime_start_logs_startup_and_shutdown_with_required_format(config: Config) -> None:
    """Runtime lifecycle logs use the required shared log format."""
    runtime = Runtime(config)

    runtime.start()
    runtime.stop()

    log_file = next((config.data_dir / "logs").iterdir())
    lines = log_file.read_text(encoding="utf-8").strip().splitlines()

    assert any(line.endswith("[INFO] vbot.core - Runtime startup initiated") for line in lines)
    assert any(line.endswith("[INFO] vbot.core - Runtime started") for line in lines)
    assert any(line.endswith("[INFO] vbot.core - Runtime stopped") for line in lines)


def test_runtime_start_logs_inventory_counts(
    config: Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runtime startup logs loaded tool, skill, and provider inventory counts."""
    _clear_provider_credential_environment(monkeypatch)
    runtime = Runtime(config)

    runtime.start()
    expected_message = _expected_startup_inventory_message(runtime)
    runtime.stop()

    log_file = next((config.data_dir / "logs").iterdir())
    contents = log_file.read_text(encoding="utf-8")

    assert f"[INFO] vbot.core - {expected_message}" in contents


def test_runtime_warning_logs_use_shared_manager_format(config: Config) -> None:
    """Runtime warnings emitted during startup use the managed logger contract."""
    config.data_dir.mkdir(parents=True, exist_ok=True)
    _authorize_session_store(config.data_dir)
    extra_skills_dir = config.data_dir / "extra-skills"
    broken_skill_dir = extra_skills_dir / "broken"
    broken_skill_dir.mkdir(parents=True)
    broken_skill_dir.joinpath("SKILL.md").write_text(
        """---
name: broken
description: Has unsupported vBot requirements.
metadata:
  vbot:
    requirements:
      provider: missing
---

# Broken
""",
        encoding="utf-8",
    )
    config.data_dir.joinpath("settings.json").write_text(
        json.dumps({"format_version": 1, "skill_directories": [str(extra_skills_dir)]}),
        encoding="utf-8",
    )
    runtime = Runtime(config)

    runtime.start()
    runtime.stop()

    log_file = next((config.data_dir / "logs").iterdir())
    contents = log_file.read_text(encoding="utf-8")

    assert "[WARN] vbot.core - Loaded skills with " in contents
    assert " invalid skill directories; see vbot.skills warnings for details" in contents


@pytest.mark.parametrize(
    ("owner", "method"),
    [
        # Before ``runtime.logger`` exists, on an existing data root.
        ("StorageManager", "ensure_directories"),
        # After the Runtime logger is in use.
        ("Runtime", "_start_terminal_manager"),
    ],
)
def test_runtime_failed_start_logs_one_error_with_traceback(
    config: Config, monkeypatch: pytest.MonkeyPatch, owner: str, method: str
) -> None:
    import core.runtime._bootstrap as bootstrap_module

    def fail(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("startup sentinel")

    target = Runtime if owner == "Runtime" else getattr(bootstrap_module, owner)
    monkeypatch.setattr(target, method, fail)
    runtime = Runtime(config)

    with pytest.raises(RuntimeError, match="startup sentinel"):
        runtime.start()

    contents = next((config.data_dir / "logs").iterdir()).read_text(encoding="utf-8")
    assert contents.count("[ERROR] vbot.core - Runtime startup failed") == 1
    assert contents.count("Traceback (most recent call last):") == 1
    assert contents.count("RuntimeError: startup sentinel") == 1
    # Cleanup closed the managed handlers after the failure was recorded.
    assert not runtime._log_manager._handlers


def test_runtime_failed_start_never_creates_a_missing_data_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import core.runtime._bootstrap as bootstrap_module

    config = Config(data_dir=tmp_path / "fresh")

    def fail(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("startup sentinel")

    monkeypatch.setattr(bootstrap_module.StorageManager, "ensure_directories", fail)

    with pytest.raises(RuntimeError, match="startup sentinel"):
        Runtime(config).start()

    # A root created only for the log would lack the Session bootstrap marker.
    assert not config.data_dir.exists()
