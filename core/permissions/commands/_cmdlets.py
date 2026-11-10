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
_VARIABLE_OUTPUT_PARAMETERS = (
    "OutVariable",
    "PipelineVariable",
    "ErrorVariable",
    "WarningVariable",
    "InformationVariable",
)
_VARIABLE_SCOPES = ("global:", "script:", "local:", "private:", "using:")
# The static binder cannot discover these FileSystem-provider parameters when
# Path is a variable. They change returned content, not the accessed paths.
_CONTENT_PARAMETERS = {
    "get-content": frozenset({"raw", "encoding", "delimiter", "wait", "asbytestream"}),
    "set-content": frozenset({"encoding", "nonewline", "asbytestream"}),
    "add-content": frozenset({"encoding", "nonewline", "asbytestream"}),
    "get-childitem": frozenset({"directory", "file", "attributes", "hidden", "readonly", "system"}),
}


def prepare(analysis: Analysis, context: Context, script: Script) -> None:
    """Variables and aliases a PowerShell script changes through cmdlets, before it is read."""
    for command in script.commands:
        name = (command.resolved or "").lower()
        called = literal(command.words[0]) if command.words else None
        if command.invocation == "." and command.words and not command.words[0].block:
            context.value_limit = (
                command.offset
                if context.value_limit is None
                else min(context.value_limit, command.offset)
            )
        if (
            command.script_function
            or (called and called.lower().endswith(".ps1"))
            or name == "invoke-expression"
        ):
            context.values_unknown = True
        binding = command.binding
        if binding is None:
            continue
        for parameter in _VARIABLE_OUTPUT_PARAMETERS:
            for value in items(binding.bound.get(parameter)):
                text = literal(value)
                _invalidate_variable(context, None if text is None else text.removeprefix("+"))
        if name in ("set-variable", "new-variable", "remove-variable", "clear-variable"):
            for value in items(binding.bound.get("Name")):
                text = literal(value)
                _invalidate_variable(context, text)
                if text is None:
                    context.environment_changed = True
        elif name in ("set-alias", "new-alias"):
            for value in items(binding.bound.get("Name")):
                text = literal(value)
                if text is not None:
                    context.redefined.add(text.lower())


def _invalidate_variable(context: Context, name: str | None) -> None:
    if name is None:
        context.values_unknown = True
        return
    name = name.lower()
    for scope in _VARIABLE_SCOPES:
        if name.startswith(scope):
            name = name.removeprefix(scope)
            break
    if not name or _paths.has_glob(name):
        context.values_unknown = True
    context.assigned.add(name)
    context.invalid_values.add(name)


def _provider_target(text: str) -> tuple[str, str] | None:
    if "::" in text:
        provider, name = text.split("::", 1)
        provider = provider.lower().rsplit("\\", 1)[-1]
        if provider in ("variable", "environment"):
            return "env" if provider == "environment" else "variable", name.lstrip("\\/")
    lowered = text.lower()
    for prefix, provider in (("variable:", "variable"), ("env:", "env")):
        if lowered.startswith(prefix):
            return provider, text[len(prefix) :].lstrip("\\/")
    return None


def _invalidate_provider(
    analysis: Analysis, context: Context, provider: str, name: str | None
) -> None:
    if provider == "variable":
        _invalidate_variable(context, name)
    elif name is None or not name or _paths.has_glob(name):
        context.values_unknown = True
        context.environment_changed = True
    else:
        context.assigned.add("env:" + name.lower())
        context.invalid_values.add("env:" + name.lower())
        analysis.environment_dynamic.add(environment_key(name))


def resolve_mutations(analysis: Analysis, context: Context, script: Script) -> bool:
    """Invalidate values changed through providers, using this pass's scalar values."""
    before = (
        len(context.invalid_values),
        len(analysis.environment_dynamic),
        context.values_unknown,
        context.environment_changed,
        context.value_limit,
    )
    for raw in script.commands:
        context.value_scope = raw.top_level
        context.value_offset = raw.offset
        if raw.words and literal(raw.words[0]) is None and not raw.words[0].block:
            called = analysis.literal_of(context, raw, raw.words[0])
            if called is not None:
                raw = analysis.bind_powershell(raw, called)
                if called.lower() in context.functions:
                    context.values_unknown = True
                prepare(analysis, context, Script(commands=[raw]))
            if called is not None and called.lower().endswith((".ps1", ".psm1")):
                context.value_limit = (
                    raw.offset
                    if context.value_limit is None
                    else min(context.value_limit, raw.offset)
                )
        binding = raw.binding
        name = (raw.resolved or "").lower()
        parameters = cmdlet_parameters(name)
        if binding is None or parameters is None:
            continue
        context.value_scope = raw.top_level
        context.value_offset = raw.offset
        for parameter, spec in parameters.items():
            if spec.kind != "write":
                continue
            for value in items(binding.bound.get(parameter)):
                if value.target is not None:
                    # Bounded iteration targets are checked as filesystem
                    # accesses during the walk; an unknown one still refuses.
                    continue
                if name == "new-item" and parameter in ("Path", "LiteralPath"):
                    value = joined_path(value, binding.bound.get("Name"))
                text = analysis.literal_of(context, raw, value)
                if text is None:
                    # An unknown write could reach either provider. No value
                    # derived before it can then prove a later path.
                    context.values_unknown = True
                    context.environment_changed = True
                    continue
                target = _provider_target(text)
                if target is None:
                    continue
                provider, variable = target
                _invalidate_provider(analysis, context, provider, variable)
                if name == "rename-item" and parameter in ("Path", "LiteralPath"):
                    new_name = analysis.literal_of(context, raw, binding.bound.get("NewName"))
                    _invalidate_provider(analysis, context, provider, new_name)
    after = (
        len(context.invalid_values),
        len(analysis.environment_dynamic),
        context.values_unknown,
        context.environment_changed,
        context.value_limit,
    )
    return after != before


def powershell_command(analysis: Analysis, context: Context, raw: RawCommand) -> None:
    name_word = raw.words[0]
    if name_word.block:
        # & { ... } and . { ... }: the block's commands are part of the script.
        return
    name = analysis.value(context, name_word, analysis.first(context, raw)).text
    if name is None:
        analysis.unresolve("command", raw.source, "the command name is computed when it runs")
        return
    if literal(name_word) is None:
        raw = analysis.bind_powershell(raw, name)
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
    if lowered in ("start-bitstransfer", "bitstransfer\\start-bitstransfer"):
        # BitsTransfer is not imported into the parser helper. Bind only its
        # declared syntax here; do not depend on module discovery or execution.
        cmdlet(analysis, context, raw, "Start-BitsTransfer", raw.binding or _bits_binding(raw))
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
    analysis.program_path(context, raw, name_word, script=raw.invocation == ".")
    analysis.program(context, raw, _paths.program_name(name), raw.words[1:])
    if raw.invocation != "." and analysis.native_text_output(name):
        analysis.text_output(context, raw)


def cmdlet(
    analysis: Analysis, context: Context, raw: RawCommand, name: str, binding: Binding
) -> None:
    lowered = name.lower()
    arguments = (
        [item for word in raw.words[1:] for item in items(word)]
        if lowered == "start-bitstransfer"
        else raw.words[1:]
    )
    analysis.emit(context, name, arguments, raw)
    if lowered in ("set-location", "push-location"):
        target = first_bound(binding, "Path", "LiteralPath")
        mode = "push" if lowered == "push-location" else "set"
        history = [word for word in raw.words[1:] if literal(word) in ("-", "+")]
        if target is None and history:
            target = history[0]
        if target is None and not binding.bound:
            change_directory(analysis, context, raw, mode, Word.literal("~", source=raw.source))
        elif target is not None:
            change_directory(
                analysis,
                context,
                raw,
                mode,
                target,
                wildcards=target is binding.bound.get("Path"),
            )
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
    elif lowered == "start-bitstransfer":
        _bits_transfer(analysis, context, raw, binding)
        return
    parameters = cmdlet_parameters(lowered)
    if parameters is None:
        analysis.tier_b(context, raw, raw.words[1:], mentions=True)
        return
    _path_parameters(analysis, context, raw, lowered, binding, parameters)
    if lowered in LISTING_CMDLETS:
        _register_listing(analysis, context, raw, lowered, binding)
    elif (
        lowered in FILTER_CMDLETS
        and raw.pipeline is not None
        # ExpandProperty can turn the input into arbitrary objects, including files.
        and not (
            lowered == "select-object"
            and any(parameter in binding.bound for parameter in ("Property", "ExpandProperty"))
        )
    ):
        context.listings[(raw.pipeline.id, raw.pipeline.index)] = Listing(filter=True)
    if lowered in ("get-content", "out-string", "select-string", "convert-path"):
        analysis.text_output(context, raw)
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
    harmless_errors = (
        _CONTENT_PARAMETERS.get(lowered, frozenset())
        if first_bound(binding, "Path", "LiteralPath") is not None
        else frozenset()
    )
    if any(error.lower() not in harmless_errors for error in binding.errors) or any(
        word.splat for word in raw.words[1:]
    ):
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
            listing = analysis.listing_before(context, raw.pipeline)
            if lowered == "select-string" and listing is not None and listing.text:
                return
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
    string_output = (
        (lowered == "get-childitem" and "Name" in binding.bound)
        or (lowered == "resolve-path" and "Relative" in binding.bound)
        or (lowered == "select-string" and "Raw" in binding.bound)
    )
    if string_output or (lowered == "select-string" and "Quiet" in binding.bound):
        # Relative names resolve from the consumer's cwd, and raw matches can
        # contain arbitrary paths. Neither carries the listed items' bounds.
        listing = Listing(text=string_output)
        context.produced[raw.offset] = listing
        if raw.pipeline is not None:
            context.listings[(raw.pipeline.id, raw.pipeline.index)] = listing
        return
    values = [value for name in ("Path", "LiteralPath") for value in items(binding.bound.get(name))]
    if not values:
        if raw.pipeline is not None and raw.pipeline.index > 0:
            incoming = analysis.listing_before(context, raw.pipeline)
            if lowered != "select-string" or incoming is None or incoming.text:
                return  # its paths come from unknown input, or matches are text-only
            context.produced[raw.offset] = incoming
            context.listings[(raw.pipeline.id, raw.pipeline.index)] = incoming
            return
        values = [Word.literal(".", source=raw.source)]
    listing = analysis.resolve_listing(
        context,
        raw,
        values,
        recursive=_recursive(lowered, binding),
        children=lowered == "get-childitem",
    )
    listing.filesystem_items = True
    context.produced[raw.offset] = listing
    if raw.pipeline is not None:
        context.listings[(raw.pipeline.id, raw.pipeline.index)] = listing


def _rename_target(analysis: Analysis, context: Context, raw: RawCommand, binding: Binding) -> None:
    new_name = binding.bound.get("NewName")
    source = first_bound(binding, "Path", "LiteralPath")
    if new_name is None:
        return
    source_text = analysis.literal_of(context, raw, source)
    if source_text is not None and _provider_target(source_text) is not None:
        return  # Provider names are variables, not sibling filesystem paths.
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
    analysis.program_path(context, raw, program)
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
        change_directory(analysis, context, raw, "set", directory, keep_old=False, wildcards=True)
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
    context.produced[raw.offset].filesystem_items = True


def _web_address(analysis: Analysis, context: Context, raw: RawCommand, binding: Binding) -> None:
    """Invoke-WebRequest's ``-Uri``: a computed one is unresolved, one without scheme is http."""
    for parameter in ("Uri", "Proxy"):
        for value in items(binding.bound.get(parameter)):
            text = analysis.literal_of(context, raw, value)
            if text is None:
                analysis.unresolve("url", value.source, "the address is computed when it runs")
            else:
                add_web_target(analysis, text, value.source)


_BITS_SWITCHES = frozenset(
    {
        "Asynchronous", "Dynamic", "CustomHeadersWriteOnly", "Suspended", "WhatIf", "Confirm",
        "Verbose", "Debug",
    }
)  # fmt: skip
_BITS_VALUES = frozenset(
    {
        "Source", "Destination", "TransferType", "ProxyList", "NotifyCmdLine", "Authentication",
        "Credential", "Description", "HttpMethod", "DisplayName", "Priority", "TransferPolicy",
        "ACLFlags", "SecurityFlags", "UseStoredCredential", "ProxyAuthentication", "ProxyBypass",
        "ProxyCredential", "ProxyUsage", "RetryInterval", "RetryTimeout", "MaxDownloadTime",
        "CustomHeaders", "NotifyFlags", "CertStoreLocation", "CertStoreName", "CertHash",
        "ErrorAction", "WarningAction", "InformationAction", "ProgressAction", "ErrorVariable",
        "WarningVariable", "InformationVariable", "OutVariable", "OutBuffer", "PipelineVariable",
    }
)  # fmt: skip


def _bits_binding(raw: RawCommand) -> Binding:
    """Bind the two positional parameters and unambiguous named BITS parameters."""
    binding = Binding({}, [], [])
    index = 1
    while index < len(raw.words):
        word = raw.words[index]
        index += 1
        if word.parameter is not None:
            matches = [
                name
                for name in _BITS_VALUES | _BITS_SWITCHES
                if name.lower().startswith(word.parameter.lower())
            ]
            if len(matches) != 1:
                binding.errors.append(word.parameter)
                continue
            parameter = matches[0]
            value = word.argument
            if parameter in _BITS_VALUES and value is None:
                if index < len(raw.words) and raw.words[index].parameter is None:
                    value = raw.words[index]
                    index += 1
                else:
                    binding.errors.append(parameter)
            if parameter in binding.bound:
                binding.errors.append(parameter)
            binding.bound[parameter] = value
        else:
            positional = next(
                (name for name in ("Source", "Destination") if name not in binding.bound), None
            )
            if positional is None or word.splat:
                binding.errors.append("arguments")
            else:
                binding.bound[positional] = word
    return binding


def _bits_transfer(analysis: Analysis, context: Context, raw: RawCommand, binding: Binding) -> None:
    if binding.errors or any(word.splat for word in raw.words):
        analysis.unresolve(
            "path_write", raw.source, "its parameters cannot be bound before it runs"
        )
    accesses: tuple[tuple[str, AccessKind], ...] = (("Source", "read"), ("Destination", "write"))
    for parameter, kind in accesses:
        values = items(binding.bound.get(parameter))
        if not values:
            if parameter == "Destination" and (raw.pipeline is None or raw.pipeline.index == 0):
                values = (Word.literal(".", source=raw.source),)
            else:
                analysis.unresolve(
                    "path_read" if kind == "read" else "path_write",
                    raw.source,
                    "its parameters cannot be bound before it runs",
                )
        for value in values:
            text = analysis.literal_of(context, raw, value)
            if text is not None and "://" in text and not text.lower().startswith("file://"):
                add_web_target(analysis, text, value.source)
            else:
                # A destination can be a folder; BITS then chooses filenames
                # below it from Source, including the default working folder.
                analysis.access(
                    context, raw, kind, value, glob=True, tier="a", subtree=kind == "write"
                )
    for value in items(binding.bound.get("ProxyList")):
        text = analysis.literal_of(context, raw, value)
        if text is None:
            analysis.unresolve("url", value.source, "the address is computed when it runs")
        else:
            add_web_target(analysis, text, value.source)
    if "NotifyCmdLine" in binding.bound:
        analysis.unresolve(
            "command",
            raw.source,
            "the transfer starts another command; run that command separately",
        )
