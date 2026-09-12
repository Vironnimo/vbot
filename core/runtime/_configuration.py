"""Runtime resource/version defaults and tolerant bootstrap setting inputs."""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _installed_package_version
from pathlib import Path
from typing import Any, cast

from core.runtime.interfaces import ConfigProtocol, LoggerProtocol
from core.skills.policy import SkillPolicyService
from core.storage.storage import StorageManager
from core.utils.config import VBOT_ROOT
from core.utils.errors import ConfigError, StorageError

_VBOT_ROOT = VBOT_ROOT


_DEFAULT_RESOURCES_DIR = _VBOT_ROOT / "resources"


_PACKAGE_NAME = "vbot"


_UNKNOWN_VBOT_VERSION = "0.0.0+unknown"


_SKILLS_DIRNAME = "skills"


def _detect_vbot_version() -> str:
    """Resolve the running vBot version from its single source of truth.

    The version lives once, in ``pyproject.toml`` → ``project.version``. Read
    that file directly when it sits next to the running code (the dev and
    clone-based deployments vBot actually ships as): it is the *live* value, so a
    version bump — or a ``vbot update`` git pull — flows through without a
    reinstall. Installed package metadata is only a fallback for a pure wheel
    install where the source tree is absent; it is a snapshot frozen at install
    time and would otherwise drift behind an edited ``pyproject.toml``.
    """
    try:
        with (_VBOT_ROOT / "pyproject.toml").open("rb") as handle:
            version = tomllib.load(handle)["project"]["version"]
        if isinstance(version, str) and version:
            return version
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        pass
    try:
        return _installed_package_version(_PACKAGE_NAME)
    except PackageNotFoundError:
        return _UNKNOWN_VBOT_VERSION


def _positive_size_setting(
    logger: LoggerProtocol | None, settings: dict[str, object], *, key: str, default: int
) -> int:
    raw_limit = settings.get(key, default)
    if isinstance(raw_limit, int) and not isinstance(raw_limit, bool) and raw_limit > 0:
        return raw_limit
    if logger is not None:
        logger.warning(
            "settings.%s must be a positive integer; using default %s",
            key,
            default,
        )
    return default


def _extra_extension_directories(
    logger: LoggerProtocol | None, settings: dict[str, object]
) -> list[Path]:
    raw_directories = settings.get("extension_directories", [])
    if not isinstance(raw_directories, list):
        if logger is not None:
            cast(Any, logger).warning(
                "settings.extension_directories must be a list; ignoring value"
            )
        return []

    directories: list[Path] = []
    for raw_directory in raw_directories:
        if not isinstance(raw_directory, str) or not raw_directory.strip():
            if logger is not None:
                cast(Any, logger).warning(
                    "Ignoring invalid extension directory setting: %r", raw_directory
                )
            continue
        directories.append(Path(raw_directory).expanduser())
    return directories


def _extension_load_options(
    logger: LoggerProtocol | None, settings: dict[str, object]
) -> tuple[set[str], dict[str, dict[str, object]]]:
    """Read the disabled set and per-extension config from ``settings.extensions``.

    Settings are validated before runtime reads them, so this defensive
    parse mirrors ``_extra_extension_directories`` and normalizes shape
    without re-validating: malformed pieces are ignored with a warning.
    """
    raw = settings.get("extensions")
    if raw is None:
        return set(), {}
    if not isinstance(raw, dict):
        if logger is not None:
            cast(Any, logger).warning("settings.extensions must be an object; ignoring value")
        return set(), {}

    disabled: set[str] = set()
    raw_disabled = raw.get("disabled", [])
    if isinstance(raw_disabled, list):
        for item in raw_disabled:
            if isinstance(item, str) and item.strip():
                disabled.add(item)
    elif logger is not None:
        cast(Any, logger).warning("settings.extensions.disabled must be a list; ignoring value")

    config: dict[str, dict[str, object]] = {}
    raw_config = raw.get("config", {})
    if isinstance(raw_config, dict):
        for name, value in raw_config.items():
            if isinstance(name, str) and isinstance(value, dict):
                config[name] = value
    elif logger is not None:
        cast(Any, logger).warning("settings.extensions.config must be an object; ignoring value")

    return disabled, config


def _global_agent_defaults(storage: StorageManager | None) -> dict[str, Any]:
    """Return the instance-wide ``defaults.agent`` map, or ``{}`` when unset.

    Read live from persisted ``defaults.agent`` so the resolver's chains
    (agent → project default → **global**) for model, temperature, and
    thinking effort always see the current values without a restart. Mirrors
    how ``AgentStore`` reads agent defaults; one read per resolve feeds all
    three chains.
    """
    if storage is None:
        return {}
    agent_defaults = storage.load_defaults().get("agent", {})
    return agent_defaults if isinstance(agent_defaults, dict) else {}


def _disabled_skill_names(policy: SkillPolicyService | None) -> frozenset[str]:
    """Return the policy's disabled names for registry-load exclusion.

    Called on every runtime-owned registry build; the manager-facing editor
    loads deliberately bypass this so disabled skills stay visible there.
    """
    if policy is None:
        return frozenset()
    return policy.load().disabled


def _provider_connection_enabled_overrides(
    storage: StorageManager | None, logger: LoggerProtocol | None
) -> Mapping[str, bool]:
    """Return live per-Connection enabled overrides used during credential checks."""
    if storage is None:
        return {}
    try:
        connections = storage.load_providers_settings()["connections"]
    except StorageError as error:
        if logger is not None:
            logger.warning(
                "Failed to load Provider Connection overrides: %s",
                error,
            )
        return {}
    return cast("Mapping[str, bool]", connections)


def _resolve_resources_path(config: ConfigProtocol) -> Path:
    resources_path_raw = config.get("RESOURCES_PATH")
    if resources_path_raw is not None:
        return Path(resources_path_raw)
    return _DEFAULT_RESOURCES_DIR


def _resolve_data_dir(config: ConfigProtocol) -> Path:
    data_dir_raw = config.get("DATA_DIR") or config.get("VBOT_DATA_DIR")
    if data_dir_raw:
        return Path(cast(str, data_dir_raw)).expanduser()
    if hasattr(config, "data_dir"):
        return Path(cast(Any, config).data_dir).expanduser()
    raise ConfigError("Runtime requires a data directory to initialize logging")
