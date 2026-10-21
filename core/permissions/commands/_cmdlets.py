"""PowerShell commands: cmdlets by their bound parameters, functions, scripts, aliases.

The helper resolves aliases and binds parameters statically (``_powershell.py``);
this module gives the bound values meaning through the ``CMDLETS`` table, the
location cmdlets, ``Invoke-Expression``, ``Start-Process``, ``New-Object`` and
the listing cmdlets whose output a later pipeline element or method call uses.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import TYPE_CHECKING

from core.permissions.commands import _paths
from core.permissions.commands._directories import change_directory, switch_drive
from core.permissions.commands._dotnet import FILE_OBJECTS
from core.permissions.commands._handlers import add_web_target
from core.permissions.commands._nesting import nested, split_windows_arguments
from core.permissions.commands._specs import (
    CMDLETS,
    FILTER_CMDLETS,
    LISTING_CMDLETS,
    LOCATION_FUNCTIONS,
    MODULE_FILE,
    NAME_OR_PATH_PARAMETERS,
    AccessKind,
    CmdletParameter,
    cmdlet_parameters,
)
from core.permissions.commands._state import (
    UNRESOLVED_KIND,
    Context,
    Listing,
    environment_key,
    first_bound,
    items,
    joined_path,
    literal,
    sibling,
)
from core.permissions.commands._syntax import Binding, RawCommand, Script, Word

if TYPE_CHECKING:
    from core.permissions.commands._analysis import Analysis

_DRIVE_FUNCTION = re.compile(r"^([A-Za-z]):$")
_WEB_CMDLETS = frozenset({"invoke-webrequest", "invoke-restmethod"})


def prepare(analysis: Analysis, context: Context, script: Script) -> None:
    """Variables and aliases a PowerShell script changes through cmdlets, before it is read."""
    for command in script.commands:
        name = (command.resolved or "").lower()
        binding = command.binding
        if binding is None:
            continue
        if name in ("set-variable", "new-variable", "remove-variable", "clear-variable"):
            for value in items(binding.bound.get("Name")):
                text = literal(value)
                if text is None:
                    context.environment_changed = True
                else:
                    context.assigned.add(text.lower())
        elif name in ("set-alias", "new-alias"):
            for value in items(binding.bound.get("Name")):
                text = literal(value)
                if text is not None:
                    context.redefined.add(text.lower())
        elif name in (
            "set-item",
            "new-item",
            "remove-item",
            "clear-item",
            "set-content",
            "rename-item",
        ):
            for parameter in ("Path", "LiteralPath"):
                for value in items(binding.bound.get(parameter)):
                    text = literal(value) or ""
                    if text.lower().startswith(("env:", "variable:")):
                        variable = text.split(":", 1)[1].lstrip("\\/").lower()
                        prefix = "env:" if text.lower().startswith("env:") else ""
                        context.assigned.add(prefix + variable)
                        if prefix:
                            analysis.environment_dynamic.add(environment_key(variable))


def powershell_command(analysis: Analysis, context: Context, raw: RawCommand) -> None:
    name_word = raw.words[0]
    if name_word.block:
        # & { ... } and . { ... }: the block's commands are part of the script.
        return
    name = analysis.value(context, name_word, analysis.first(context, raw)).text
    if name is None:
        analysis.unresolve("command", raw.source, "the command name is computed when it runs")
        return
    if raw.stop_parsing:
        analysis.emit(context, raw.resolved or _paths.program_name(name), raw.words[1:], raw)
        analysis.unresolve(
            "command", raw.source, "--% passes the rest of the line to the program unparsed"
        )
        return
    lowered = name.lower()
    if lowered in context.redefined:
        analysis.unresolve("command", raw.source, f"the command line redefines {name} itself")
        return
    if raw.script_function:
        analysis.emit(context, name, raw.words[1:], raw)
        analysis.tier_b(context, raw, raw.words[1:])
        return
    if raw.resolved is not None:
        resolved = raw.resolved
        location = LOCATION_FUNCTIONS.get(resolved.lower())
        if location is not None:
            target = Word.literal(location, source=name)
            analysis.emit(context, "Set-Location", [target], raw)
            change_directory(analysis, context, raw, "set", target)
            return
        drive = _DRIVE_FUNCTION.match(resolved)
        if drive is not None:
            analysis.emit(context, resolved, [], raw)
            switch_drive(analysis, context, raw, drive.group(1).upper())
            return
        if raw.binding is not None:
            cmdlet(analysis, context, raw, resolved, raw.binding)
            return
        analysis.emit(context, resolved, raw.words[1:], raw)
        analysis.tier_b(context, raw, raw.words[1:])
        return
    # . script and & script run a script file, which they read.
    if raw.invocation == "." or (
        raw.invocation == "&"
        and (_paths.is_clearly_path(name, windows_switches=True) or lowered.endswith(".ps1"))
    ):
        analysis.access(context, raw, "read", name_word, glob=False, tier="a")
    analysis.program(context, raw, _paths.program_name(name), raw.words[1:])


def cmdlet(
    analysis: Analysis, context: Context, raw: RawCommand, name: str, binding: Binding
) -> None:
    lowered = name.lower()
    analysis.emit(context, name, raw.words[1:], raw)
    if lowered in ("set-location", "push-location"):
        target = first_bound(binding, "Path", "LiteralPath")
        mode = "push" if lowered == "push-location" else "set"
        history = [word for word in raw.words[1:] if literal(word) in ("-", "+")]
        if target is None and history:
            target = history[0]
        if target is None and not binding.bound:
            change_directory(analysis, context, raw, mode, Word.literal("~", source=raw.source))
        elif target is not None:
            change_directory(analysis, context, raw, mode, target)
        return
    if lowered == "pop-location":
        change_directory(analysis, context, raw, "pop", None)
        return
    if lowered == "invoke-expression":
        command = first_bound(binding, "Command")
        value = None if command is None else analysis.literal_of(context, raw, command)
        if value is None:
            analysis.unresolve(
                "command", raw.source, "Invoke-Expression runs a command computed when it runs"
            )
            return
        nested(analysis, context, raw, "powershell", value)
        return
    if lowered == "start-process":
        _start_process(analysis, context, raw, binding)
        return
    if lowered == "new-object":
        _new_object(analysis, context, raw, binding)
        return
    if lowered in _WEB_CMDLETS:
        _web_address(analysis, context, raw, binding)
    parameters = cmdlet_parameters(lowered)
    if parameters is None:
        analysis.tier_b(context, raw, raw.words[1:], mentions=True)
        return
    _path_parameters(analysis, context, raw, lowered, binding, parameters)
    if lowered in LISTING_CMDLETS:
        _register_listing(analysis, context, raw, lowered, binding)
    elif lowered in FILTER_CMDLETS and raw.pipeline is not None:
        context.listings[(raw.pipeline.id, raw.pipeline.index)] = Listing(filter=True)
    analysis.tier_b(context, raw, binding.unbound)


def _path_parameters(
    analysis: Analysis,
    context: Context,
    raw: RawCommand,
    lowered: str,
    binding: Binding,
    parameters: Mapping[str, CmdletParameter],
) -> None:
    kinds = {spec.kind for spec in parameters.values()}
    strongest: AccessKind = "write" if "write" in kinds else "read"
    if binding.errors or any(word.splat for word in raw.words[1:]):
        analysis.unresolve(
            UNRESOLVED_KIND[strongest], raw.source, "its parameters cannot be bound before it runs"
        )
    recursive = _recursive(lowered, binding)
    for parameter, spec in parameters.items():
        if parameter not in binding.bound:
            continue
        subtree = spec.subtree is True or (
            isinstance(spec.subtree, tuple) and any(name in binding.bound for name in spec.subtree)
        )
        for item in items(binding.bound[parameter]):
            if (lowered, parameter) in NAME_OR_PATH_PARAMETERS:
                text = analysis.literal_of(context, raw, item)
                if text is not None and not MODULE_FILE.search(text):
                    continue  # Import-Module Name: a module by name, not a file
            if (
                lowered == "new-item"
                and parameter in ("Path", "LiteralPath")
                and "Name" in binding.bound
            ):
                item = joined_path(item, binding.bound["Name"])
            analysis.access(
                context,
                raw,
                spec.kind,
                item,
                glob=not spec.literal,
                tier="a",
                subtree=subtree,
                # Get-ChildItem *.md -Recurse finds *.md at every depth.
                deep=recursive and lowered == "get-childitem",
            )
    if lowered == "rename-item":
        _rename_target(analysis, context, raw, binding)
    if lowered == "new-item":
        item_type = analysis.literal_of(context, raw, binding.bound.get("ItemType"))
        if item_type is not None and item_type.lower() in ("symboliclink", "junction", "hardlink"):
            path = first_bound(binding, "Path", "LiteralPath")
            if path is not None and "Name" in binding.bound:
                path = joined_path(path, binding.bound["Name"])
            hard = item_type.lower() == "hardlink"
            for target in items(binding.bound.get("Value")):
                if path is None:
                    analysis.access(
                        context, raw, "write", target, glob=False, tier="a", subtree=not hard
                    )
                else:
                    analysis.link(context, raw, path, target, hard=hard)
    has_path = any(name in parameters for name in ("Path", "LiteralPath", "FilePath"))
    bound_path = any(name in binding.bound for name in ("Path", "LiteralPath", "FilePath"))
    if has_path and not bound_path:
        if raw.pipeline is not None and raw.pipeline.index > 0 and lowered in CMDLETS:
            primary = (
                parameters.get("Path") or parameters.get("LiteralPath") or parameters["FilePath"]
            )
            analysis.pipeline_targets(context, raw, primary.kind)
        elif lowered in ("get-childitem", "expand-archive"):
            default_kind: AccessKind = "read" if lowered == "get-childitem" else "write"
            analysis.access(
                context,
                raw,
                default_kind,
                Word.literal(".", source=raw.source),
                glob=False,
                tier="a",
                subtree=lowered == "expand-archive" or recursive,
            )


def _recursive(lowered: str, binding: Binding) -> bool:
    return lowered == "get-childitem" and ("Recurse" in binding.bound or "Depth" in binding.bound)


def _register_listing(
    analysis: Analysis, context: Context, raw: RawCommand, lowered: str, binding: Binding
) -> None:
    """What a listing cmdlet hands on, to the next pipeline element and to method calls."""
    values = [value for name in ("Path", "LiteralPath") for value in items(binding.bound.get(name))]
    if not values:
        if raw.pipeline is not None and raw.pipeline.index > 0:
            return  # its paths come from its input
        values = [Word.literal(".", source=raw.source)]
    listing = analysis.resolve_listing(
        context,
        raw,
        values,
        recursive=_recursive(lowered, binding),
        children=lowered == "get-childitem",
    )
    context.produced[raw.offset] = listing
    if raw.pipeline is not None:
        context.listings[(raw.pipeline.id, raw.pipeline.index)] = listing


def _rename_target(analysis: Analysis, context: Context, raw: RawCommand, binding: Binding) -> None:
    new_name = binding.bound.get("NewName")
    source = first_bound(binding, "Path", "LiteralPath")
    if new_name is None:
        return
    text = analysis.literal_of(context, raw, new_name)
    if text is None:
        analysis.unresolve("path_write", raw.source, "the new name is computed when it runs")
        return
    if re.search(r"[\\/]", text) or source is None:
        analysis.access(context, raw, "write", new_name, glob=False, tier="a", subtree=True)
        return
    # The renamed item keeps everything below it.
    target = sibling(source, text)
    analysis.access(context, raw, "write", target, glob=False, tier="a", subtree=True)


def _start_process(analysis: Analysis, context: Context, raw: RawCommand, binding: Binding) -> None:
    redirects: tuple[tuple[str, AccessKind], ...] = (
        ("RedirectStandardOutput", "write"),
        ("RedirectStandardError", "write"),
        ("RedirectStandardInput", "read"),
    )
    for parameter, kind in redirects:
        for value in items(binding.bound.get(parameter)):
            analysis.access(context, raw, kind, value, glob=False, tier="a")
    program = binding.bound.get("FilePath")
    if program is None:
        return
    location = analysis.first(context, raw)
    name = analysis.value(context, program, location).text
    if name is None:
        analysis.unresolve(
            "command", raw.source, "Start-Process starts a program computed when it runs"
        )
        return
    if _paths.is_clearly_path(name, windows_switches=True):
        analysis.access(context, raw, "read", program, glob=False, tier="a")
    listed = items(binding.bound.get("ArgumentList"))
    arguments: list[Word] = []
    for value in listed:
        text = analysis.value(context, value, location).text
        if text is None:
            arguments.append(value)
        elif len(listed) == 1:
            arguments.extend(Word.literal(item) for item in split_windows_arguments(text))
        else:
            arguments.append(Word.literal(text, source=value.source))
    saved = context.locations(raw.scope)
    directory = binding.bound.get("WorkingDirectory")
    if directory is not None:
        change_directory(analysis, context, raw, "set", directory, keep_old=False)
    analysis.program(context, raw, _paths.program_name(name), arguments)
    context.states[raw.scope] = saved


def _new_object(analysis: Analysis, context: Context, raw: RawCommand, binding: Binding) -> None:
    """``New-Object IO.StreamWriter x``: objects that open or name a file."""
    type_word = binding.bound.get("TypeName")
    type_name = None if type_word is None else analysis.literal_of(context, raw, type_word)
    arguments = items(binding.bound.get("ArgumentList"))
    if type_name is None:
        if type_word is not None:
            analysis.tier_b(context, raw, arguments)
        return
    normalized = type_name.lower().removeprefix("system.")
    if normalized not in FILE_OBJECTS:
        analysis.tier_b(context, raw, arguments)
        return
    kind = FILE_OBJECTS[normalized]
    if not arguments:
        return
    if kind is not None:
        analysis.access(context, raw, kind, arguments[0], glob=False, tier="a")
        return
    # A FileInfo or DirectoryInfo: reading its properties reads the item.
    analysis.access(context, raw, "read", arguments[0], glob=False, tier="a")
    context.produced[raw.offset] = analysis.resolve_listing(
        context, raw, arguments[:1], recursive=False
    )


def _web_address(analysis: Analysis, context: Context, raw: RawCommand, binding: Binding) -> None:
    """Invoke-WebRequest's ``-Uri``: a computed one is unresolved, one without scheme is http."""
    for value in items(binding.bound.get("Uri")):
        text = analysis.literal_of(context, raw, value)
        if text is None:
            analysis.unresolve("url", value.source, "the address is computed when it runs")
        elif "://" not in text:
            add_web_target(analysis, text, value.source)
