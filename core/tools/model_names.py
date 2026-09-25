"""Tool names as the Model sees them on this host, and names Models use instead.

The registry name identifies a Tool everywhere inside vBot: policies, Session
history, settings, Run events and Extension hooks. A Model can see a different
name when that name tells it more; the shell Tool is offered as ``powershell`` on
Windows, because a Tool called ``bash`` makes Models write bash syntax for a
PowerShell host. Chat renames at the Provider boundary in both directions, and
Model-facing text uses ``model_tool_name``.

Models also call Tools by names they were trained on elsewhere (``Read``,
``functions.bash``, ``Grep``, ``Task``). ``called_tool_name`` maps such a name to
the offered Tool it clearly means, so the call runs instead of failing.
"""

from __future__ import annotations

import os
import re
from collections.abc import Collection, Container

BASH_TOOL_NAME = "bash"
SHELL_MODEL_NAME = "powershell" if os.name == "nt" else BASH_TOOL_NAME

_MODEL_NAMES = {BASH_TOOL_NAME: SHELL_MODEL_NAME} if SHELL_MODEL_NAME != BASH_TOOL_NAME else {}
_REGISTRY_NAMES = {model: name for name, model in _MODEL_NAMES.items()}

# Namespaces some Models put in front of a function name.
_WRAPPER_PREFIXES = ("functions.", "function.", "default_api.", "default_api:", "tools.", "tool.")
_SEPARATORS = re.compile(r"[\s_.:/-]+")

# Names other agent harnesses give the same capability, keyed by their spelling
# without case and separators. A name maps only to a Tool offered in the Model
# request; the target's own argument handling then decides whether the call runs.
_HARNESS_NAMES = {
    **dict.fromkeys(
        (
            "shell",
            "sh",
            "zsh",
            "cmd",
            "pwsh",
            "powershell",
            "terminal",
            "exec",
            "execute",
            "executecommand",
            "execcommand",
            "runcommand",
            "runshellcommand",
            "shellcommand",
            "runterminalcmd",
            "runterminalcommand",
            "localshell",
            "containerexec",
        ),
        BASH_TOOL_NAME,
    ),
    **dict.fromkeys(
        ("readfile", "viewfile", "view", "cat", "openfile", "fileread", "readtextfile"),
        "read",
    ),
    **dict.fromkeys(
        (
            "edit",
            "editfile",
            "fileedit",
            "multiedit",
            "strreplace",
            "replace",
            "replaceinfile",
            "searchreplace",
            "searchandreplace",
            "write",
            "writefile",
            "filewrite",
            "createfile",
            "writetofile",
            "patch",
            "applydiff",
        ),
        "apply_patch",
    ),
    **dict.fromkeys(
        (
            "grep",
            "glob",
            "rg",
            "ripgrep",
            "find",
            "findfiles",
            "filesearch",
            "searchfilecontent",
            "grepsearch",
            "listfiles",
            "listdir",
            "listdirectory",
            "ls",
        ),
        "search_files",
    ),
    **dict.fromkeys(
        (
            "fetch",
            "fetchurl",
            "urlfetch",
            "readurl",
            "openurl",
            "browse",
            "browseurl",
            "fetchwebpage",
            "webextract",
            "tavilyextract",
        ),
        "web_fetch",
    ),
    **dict.fromkeys(
        (
            "searchweb",
            "googlesearch",
            "googlewebsearch",
            "internetsearch",
            "bingsearch",
            "bravesearch",
            "tavilysearch",
            "websearchexa",
        ),
        "web_search",
    ),
    **dict.fromkeys(
        ("task", "agent", "delegate", "delegatetask", "spawnagent", "spawnsubagent"),
        "subagent",
    ),
    **dict.fromkeys(("loadskill", "useskill", "skillview", "readskill"), "skill"),
    **dict.fromkeys(
        (
            "viewimage",
            "readimage",
            "describeimage",
            "imageanalysis",
            "vision",
            "visionanalyze",
        ),
        "analyze_image",
    ),
    **dict.fromkeys(("generateimage", "createimage", "imagegen"), "image_generation"),
    **dict.fromkeys(("tts", "speak"), "text_to_speech"),
}


def model_tool_name(name: str) -> str:
    """Return the name the Model sees for the registry Tool ``name``."""
    return _MODEL_NAMES.get(name, name)


def registry_tool_name(name: str) -> str:
    """Return the registry name for a Tool name the Model used."""
    return _REGISTRY_NAMES.get(name, name)


def reserved_model_name(name: str) -> bool:
    """Whether ``name`` is how the Model sees another Tool on this host."""
    return name in _REGISTRY_NAMES


def called_tool_name(
    name: str, offered: Collection[str], *, registered: Container[str] = ()
) -> str:
    """Return the offered Tool that the called ``name`` clearly means, else ``name``.

    ``offered`` holds the registry names of the Tools in the Model request. A
    registered name is never remapped, even when it was not offered: it names a
    real Tool, and dispatch explains why that Tool cannot run. Otherwise a
    namespace prefix, case and separators are ignored, and a name another
    harness uses for the same capability maps to the offered vBot Tool.
    """
    if name in offered or name in registered:
        return name
    bare = name.strip()
    for prefix in _WRAPPER_PREFIXES:
        if bare.casefold().startswith(prefix):
            bare = bare[len(prefix) :]
            break
    bare = bare.removesuffix("()")
    if registry_tool_name(bare) in offered:
        return registry_tool_name(bare)
    key = _spelling_key(bare)
    same_spelling = {
        tool
        for tool in offered
        if key in (_spelling_key(tool), _spelling_key(model_tool_name(tool)))
    }
    if len(same_spelling) == 1:
        return same_spelling.pop()
    target = _HARNESS_NAMES.get(key)
    return target if target is not None and target in offered else name


def _spelling_key(name: str) -> str:
    return _SEPARATORS.sub("", name).casefold()
