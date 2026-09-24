"""The running vBot version from its single source of truth."""

from __future__ import annotations

import tomllib
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _installed_package_version
from pathlib import Path

PACKAGE_NAME = "vbot"
UNKNOWN_VBOT_VERSION = "0.0.0+unknown"
# The source checkout root; kept local so the helper imports nothing heavy.
_VBOT_ROOT = Path(__file__).resolve().parents[2]


def detect_vbot_version() -> str:
    """Resolve the running vBot version from its single source of truth.

    The version lives once, in ``pyproject.toml`` -> ``project.version``. Read
    that file directly when it sits next to the running code (the dev and
    clone-based deployments vBot actually ships as): it is the *live* value, so a
    version bump - or a ``vbot update`` git pull - flows through without a
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
        return _installed_package_version(PACKAGE_NAME)
    except PackageNotFoundError:
        return UNKNOWN_VBOT_VERSION
