"""Configuration loader for vBot.

Loads settings from ``.env`` files and ``settings.json``, merges them
together, and exposes a simple ``get()`` accessor.  Operating-system
environment variables take highest priority and are always available.
"""

import json
import os
from pathlib import Path
from typing import Any

from core.utils.errors import ConfigError


class Config:
    """Loads and merges configuration from multiple sources.

    Sources are loaded in this order (later sources override earlier ones):

    1. ``settings.json`` — base configuration (may contain nested objects).
    2. ``.env``  — environment-specific ``KEY=VALUE`` overrides.
    3. ``os.environ`` — actual process environment (highest priority).

    All three sources are optional — missing ``.env`` or ``settings.json``
    is **not** an error.  A malformed file *is* an error.

    Usage::

        config = Config()
        port = config.get("PORT", 8080)
        data = config.data_dir  # ~/.vbot
    """

    def __init__(
        self,
        env_path: str | Path | None = None,
        settings_path: str | Path | None = None,
        data_dir: str | Path | None = None,
    ) -> None:
        """Initialise the configuration.

        Args:
            env_path: Path to a ``.env`` file.  Defaults to ``./.env``
                      relative to the current working directory.
            settings_path: Path to a ``settings.json`` file.  Defaults
                           to ``./settings.json`` relative to CWD.
            data_dir: vBot data directory.  Defaults to ``~/.vbot``.
        """
        self._data: dict[str, Any] = {}
        root = Path.cwd()

        self._env_path = Path(env_path) if env_path else root / ".env"
        self._settings_path = Path(settings_path) if settings_path else root / "settings.json"
        self._data_dir = Path(data_dir) if data_dir else Path.home() / ".vbot"

        self._load()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self, key: str, default: Any = None) -> Any:
        """Return the value for *key*, or *default* if not found.

        Args:
            key: Configuration key (case-sensitive).
            default: Fallback value when the key is absent.

        Returns:
            The stored value (any JSON-compatible type, or a string for
            ``.env`` / environment-variable values).
        """
        return self._data.get(key, default)

    @property
    def data_dir(self) -> Path:
        """The vBot data directory.

        Defaults to ``~/.vbot`` unless overridden via constructor.

        Returns:
            An absolute path.  The directory is **not** created automatically.
        """
        return self._data_dir

    # ------------------------------------------------------------------
    # Loading helpers
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """Load all sources in priority order."""
        self._load_settings_json()
        self._load_env_file()
        self._load_os_environ()

    def _load_settings_json(self) -> None:
        """Load key-value pairs from ``settings.json``."""
        if not self._settings_path.exists():
            return

        try:
            data = json.loads(self._settings_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"Invalid JSON in {self._settings_path}: {exc}") from exc
        except OSError as exc:
            raise ConfigError(f"Cannot read {self._settings_path}: {exc}") from exc

        if not isinstance(data, dict):
            raise ConfigError(
                f"Expected a JSON object at {self._settings_path}, got {type(data).__name__}"
            )

        self._data.update(data)

    def _load_env_file(self) -> None:
        """Parse ``KEY=VALUE`` pairs from a ``.env`` file."""
        if not self._env_path.exists():
            return

        try:
            raw = self._env_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"Cannot read {self._env_path}: {exc}") from exc

        for _line_no, line in enumerate(raw.splitlines(), start=1):
            stripped = line.strip()

            # Skip blank lines and comments
            if not stripped or stripped.startswith("#"):
                continue

            # Split on *first* '=' only (values may contain '=')
            if "=" not in stripped:
                continue

            key, _, value = stripped.partition("=")
            key = key.strip()
            value = value.strip()

            # Strip surrounding quotes (single or double)
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]

            if key:
                self._data[key] = self._coerce_value(value)

    def _load_os_environ(self) -> None:
        """Overlay process environment variables (highest priority).

        Values are coerced through :meth:`_coerce_value` for consistency
        with ``.env``-file handling — e.g. ``LOG_LEVEL=20`` becomes
        ``int(20)`` from either source.
        """
        for key, value in os.environ.items():
            self._data[key] = self._coerce_value(value)

    @staticmethod
    def _coerce_value(value: str) -> Any:
        """Convert a ``.env`` string value to the most specific Python type.

        Coercion rules (tried in order):
        1. ``"true"`` / ``"yes"`` / ``"1"`` → ``True``
        2. ``"false"`` / ``"no"`` / ``"0"`` → ``False``
        3. Integer
        4. Float
        5. Original string (fallback)

        Returns:
            The coerced value.
        """
        lower = value.lower()
        if lower in ("true", "yes", "1"):
            return True
        if lower in ("false", "no", "0"):
            return False

        for parser in (int, float):
            try:
                return parser(value)
            except ValueError:
                continue

        return value


def load_config() -> Config:
    """Convenience helper — create a ``Config`` with default paths.

    Returns:
        A fully loaded :class:`Config` instance.
    """
    return Config()
