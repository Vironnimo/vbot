"""Tool names as the Model sees them on this host.

The registry name identifies a Tool everywhere inside vBot: policies, Session
history, settings, Run events and Extension hooks. A Model can see a different
name when that name tells it more; the shell Tool is offered as ``powershell`` on
Windows, because a Tool called ``bash`` makes Models write bash syntax for a
PowerShell host. Chat renames at the Provider boundary in both directions, and
Model-facing text uses ``model_tool_name``.
"""

from __future__ import annotations

import os

BASH_TOOL_NAME = "bash"
SHELL_MODEL_NAME = "powershell" if os.name == "nt" else BASH_TOOL_NAME

_MODEL_NAMES = {BASH_TOOL_NAME: SHELL_MODEL_NAME} if SHELL_MODEL_NAME != BASH_TOOL_NAME else {}
_REGISTRY_NAMES = {model: name for name, model in _MODEL_NAMES.items()}


def model_tool_name(name: str) -> str:
    """Return the name the Model sees for the registry Tool ``name``."""
    return _MODEL_NAMES.get(name, name)


def registry_tool_name(name: str) -> str:
    """Return the registry name for a Tool name the Model used."""
    return _REGISTRY_NAMES.get(name, name)


def reserved_model_name(name: str) -> bool:
    """Whether ``name`` is how the Model sees another Tool on this host."""
    return name in _REGISTRY_NAMES
