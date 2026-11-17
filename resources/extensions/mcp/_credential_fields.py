"""Recognize values that MCP setup and configuration route to Credentials."""

from __future__ import annotations

import re
from pathlib import PurePosixPath, PureWindowsPath

# Words of a variable or header name that mark its value as a secret.
_SECRET_WORDS = frozenset(
    {
        "TOKEN",
        "TOKENS",
        "SECRET",
        "SECRETS",
        "PASSWORD",
        "PASSWD",
        "PASS",
        "PASSPHRASE",
        "APIKEY",
        "KEY",
        "AUTH",
        "AUTHORIZATION",
        "CREDENTIAL",
        "CREDENTIALS",
        "COOKIE",
        "PAT",
        "PRIVATE",
        "SESSION",
        "SIGNATURE",
    }
)
# A last word that names something about a secret rather than the secret itself.
_SECRET_DESCRIPTORS = frozenset(
    {
        "PATH",
        "FILE",
        "DIR",
        "URL",
        "URI",
        "HOST",
        "ID",
        "NAME",
        "TYPE",
        "MODE",
        "REGION",
        "ENDPOINT",
        "PORT",
        "HEADER",
        "ENABLED",
        "EXPIRY",
    }
)
SECRET_VALUE = re.compile(
    r"""^(?:
        sk-[A-Za-z0-9_-]{16,}
      | (?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{16,}
      | gh[pousr]_[A-Za-z0-9]{20,}
      | github_pat_\w{20,}
      | glpat-[\w-]{20,}
      | xox[abposr]-[\w-]{10,}
      | AKIA[0-9A-Z]{16}
      | AIza[\w-]{30,}
      | ya29\.[\w-]{20,}
      | eyJ[\w-]{8,}\.eyJ[\w-]{8,}\.[\w-]{8,}
      | (?:Bearer|Basic|Token)\s+\S{8,}
    )$""",
    re.VERBOSE,
)


def _secret_name(name: str) -> bool:
    words = [word.upper() for word in re.split(r"[^A-Za-z0-9]+|(?<=[a-z])(?=[A-Z])", name) if word]
    if not words or words[-1] in _SECRET_DESCRIPTORS:
        return False
    joined = "_".join(words)
    return bool(_SECRET_WORDS & set(words)) or any(
        part in joined for part in ("API_KEY", "ACCESS_KEY", "PRIVATE_KEY")
    )


def is_plain_value(value: str) -> bool:
    """A value no secret looks like: a switch, a number or a path."""
    text = value.strip()
    if not text:
        return False
    return (
        text.lower() in {"true", "false", "yes", "no", "on", "off"}
        or bool(re.fullmatch(r"-?\d+(?:\.\d+)?", text))
        or is_absolute(text)
    )


def is_absolute(path: str) -> bool:
    return PurePosixPath(path).is_absolute() or PureWindowsPath(path).is_absolute()


def looks_like_credential(name: str, value: str) -> bool:
    """Whether a value belongs in Credentials instead of plain configuration."""
    return bool(SECRET_VALUE.match(value.strip())) or (
        _secret_name(name) and not is_plain_value(value)
    )
