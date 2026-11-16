"""Hints for commands PowerShell's guards refused or ended (``_powershell.py``, Guards)."""

from __future__ import annotations

import os
import re

from . import _shell_hint_common as _common
from ._shell_hint_common import _Failure

# PowerShell's strict-mode message, which the check before the run reports.
_UNASSIGNED = re.compile(
    r"The variable '\$([\w:?]+)' cannot be retrieved because it has not been set"
)
# vBot's own closing words of a refused Remove-Item or Move-Item.
_PROTECTED = ": vBot protects this folder."
# A directory change that failed as the last thing the output shows.
_FAILED_LOCATION = re.compile(r"^(Set-Location|Push-Location): .+\s*\Z", re.M)
_HANDLES_ERRORS = re.compile(r"-(?:ErrorAction|ea)\b|\$ErrorActionPreference|\bcatch\b", re.I)


def _hint_unassigned_variable(failure: _Failure) -> str | None:
    """Say how to give the variable a value; the command never assigned it.

    Agents reach this with bash's ``NAME=value``, with an environment variable
    written as ``$NAME``, and with a variable an earlier command assigned.
    """
    if "ParserError" not in failure.output:
        return None
    found = _UNASSIGNED.search(failure.output)
    if found is None:
        return None
    name = found[1]
    if re.search(rf"(?<![$\w:.-]){re.escape(name)}=", failure.command):
        return (
            f"Nothing ran. `{name}=value` assigns nothing in PowerShell: write `${name} = 'value'`."
        )
    if name in os.environ:
        return (
            f"Nothing ran. ${name} is not a PowerShell variable: the environment variable "
            f"is `$env:{name}`."
        )
    return (
        f"Nothing ran. The command reads ${name} but never assigns it, and variables of "
        f"earlier commands are gone. Assign it in this command. To test whether a variable "
        f"exists, use `Test-Path Variable:{name}`."
    )


def _hint_protected_folder(failure: _Failure) -> str | None:
    """Name what a refused Remove-Item or Move-Item may take instead."""
    if _PROTECTED not in failure.output:
        return None
    return (
        "The command ended at this statement. Remove-Item and Move-Item do not take a drive "
        "root, the user's profile folders, vBot's data directory or the root of a Project or "
        "Workspace, neither whole nor all of its content at once. Name the files and "
        "subfolders to remove. If the folder itself has to go, tell the user."
    )


def _hint_failed_location(failure: _Failure) -> str | None:
    """Say that the command ended at a directory change that failed.

    PowerShell alone goes on in the old directory; the guards end the command
    unless it handles the error itself.
    """
    if not _common._POWERSHELL or _HANDLES_ERRORS.search(failure.command):
        return None
    found = _FAILED_LOCATION.search(failure.output)
    if found is None:
        return None
    return f"The command ended at this failed {found[1]}: nothing after it ran."
