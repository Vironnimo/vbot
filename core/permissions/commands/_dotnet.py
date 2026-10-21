"""What PowerShell expressions do: .NET method calls on files and the web, and strings.

``[IO.File]::WriteAllText(p, x)``, ``(Get-Item x).Delete()`` and
``gci | % { $_.Delete() }`` touch files without a command. A small table of the
.NET file and network APIs gives each method its accesses by argument; a call
whose target or arguments the analysis cannot follow is unresolved when its
method name only makes sense on files. Every other string an expression names
is a tier B word: a clear path counts as a read, a URL as a fetch.

.NET resolves a relative path against the process folder, not PowerShell's
location, so a call's relative path is resolved against both.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from core.permissions.commands._specs import AccessKind
from core.permissions.commands._state import Context, Listing
from core.permissions.commands._syntax import MemberCall, RawCommand, Word

if TYPE_CHECKING:
    from core.permissions.commands._analysis import Analysis

type Effect = tuple[int, str]  # (argument index, "read" | "write" | "subtree" | "url" | "cwd")


@dataclass(frozen=True, slots=True)
class _Method:
    """Accesses by argument index; ``self_kind`` is what it does to the object's own item."""

    effects: tuple[Effect, ...] = ()
    self_kind: str | None = None


def _m(*effects: Effect, self_kind: str | None = None) -> _Method:
    return _Method(tuple(effects), self_kind)


_READ0 = _m((0, "read"))
_WRITE0 = _m((0, "write"))
_FILE_READS = (
    "readalltext", "readalllines", "readallbytes", "readlines", "exists", "openread",
    "opentext", "getattributes", "getcreationtime", "getcreationtimeutc", "getlastaccesstime",
    "getlastaccesstimeutc", "getlastwritetime", "getlastwritetimeutc", "getunixfilemode",
    "readalltextasync", "readalllinesasync", "readallbytesasync", "readlinesasync",
    "openhandle",
)  # fmt: skip
_FILE_WRITES = (
    "writealltext", "writealllines", "writeallbytes", "appendalltext", "appendalllines",
    "appendtext", "create", "createtext", "delete", "setattributes", "setcreationtime",
    "setcreationtimeutc", "setlastaccesstime", "setlastaccesstimeutc", "setlastwritetime",
    "setlastwritetimeutc", "setunixfilemode", "encrypt", "decrypt", "open", "openwrite",
    "writealltextasync", "writealllinesasync", "writeallbytesasync", "appendalltextasync",
    "appendalllinesasync", "writeallbytes", "setaccesscontrol",
)  # fmt: skip
_DIRECTORY_READS = (
    "exists", "getfiles", "getdirectories", "getfilesystementries", "enumeratefiles",
    "enumeratedirectories", "enumeratefilesystementries", "getcreationtime",
    "getlastwritetime", "getlastaccesstime", "getaccesscontrol",
)  # fmt: skip

# Static methods of .NET types, by lower-case type name without "system.".
_STATIC: Mapping[str, Mapping[str, _Method]] = {
    "io.file": {
        **dict.fromkeys(_FILE_READS, _READ0),
        **dict.fromkeys(_FILE_WRITES, _WRITE0),
        "copy": _m((0, "read"), (1, "write")),
        "move": _m((0, "write"), (1, "write")),
        "replace": _m((0, "write"), (1, "write"), (2, "write")),
        "createsymboliclink": _m((0, "write"), (1, "read")),
    },
    "io.directory": {
        **dict.fromkeys(_DIRECTORY_READS, _READ0),
        "delete": _m((0, "subtree")),
        "move": _m((0, "subtree"), (1, "subtree")),
        "createdirectory": _WRITE0,
        "createsymboliclink": _m((0, "write"), (1, "read")),
        "setcurrentdirectory": _m((0, "cwd")),
        "getparent": _m(),
        "getdirectoryroot": _m(),
        "getcurrentdirectory": _m(),
        "getlogicaldrives": _m(),
        "setcreationtime": _WRITE0,
        "setlastwritetime": _WRITE0,
        "setlastaccesstime": _WRITE0,
        "setaccesscontrol": _WRITE0,
    },
    "io.compression.zipfile": {
        "extracttodirectory": _m((0, "read"), (1, "subtree")),
        "createfromdirectory": _m((0, "read"), (1, "write")),
        "openread": _READ0,
        "open": _WRITE0,
    },
    "net.webrequest": {"create": _m((0, "url")), "createhttp": _m((0, "url"))},
    "net.httpwebrequest": {"create": _m((0, "url")), "createhttp": _m((0, "url"))},
}
# Types whose unknown static methods touch the path of their first argument.
_FILE_TYPES = frozenset({"io.file", "io.directory", "io.compression.zipfile"})

# Methods of FileInfo and DirectoryInfo objects, on the item the object names.
_ITEM: Mapping[str, _Method] = {
    "delete": _m(self_kind="subtree"),
    "moveto": _m((0, "subtree"), self_kind="subtree"),
    "copyto": _m((0, "write"), self_kind="read"),
    "replace": _m((0, "write"), (1, "write"), self_kind="write"),
    "create": _m(self_kind="write"),
    "createtext": _m(self_kind="write"),
    "appendtext": _m(self_kind="write"),
    "open": _m(self_kind="write"),
    "openwrite": _m(self_kind="write"),
    "encrypt": _m(self_kind="write"),
    "decrypt": _m(self_kind="write"),
    "createsubdirectory": _m(self_kind="subtree"),
    "setaccesscontrol": _m(self_kind="write"),
    "openread": _m(self_kind="read"),
    "opentext": _m(self_kind="read"),
    "getfiles": _m(self_kind="read"),
    "getdirectories": _m(self_kind="read"),
    "getfilesysteminfos": _m(self_kind="read"),
    "enumeratefiles": _m(self_kind="read"),
    "enumeratedirectories": _m(self_kind="read"),
    "enumeratefilesysteminfos": _m(self_kind="read"),
}
# Method names that on any object only make sense for files: when the object
# cannot be followed, the call is unresolved. Ambiguous names (Replace, Open,
# Create, CopyTo) count only on objects known to be files.
_FILE_ONLY = frozenset(
    {
        "delete", "moveto", "createsubdirectory", "openwrite", "appendtext", "createtext",
        "encrypt", "decrypt", "deletefile", "deletefolder", "copyfile", "copyfolder",
        "movefile", "movefolder", "createtextfile", "opentextfile", "createfolder",
    }
)  # fmt: skip
# Web methods, on any object (WebClient, HttpClient): the address and the files they use.
_WEB: Mapping[str, _Method] = {
    **{
        name: _m((0, "url"))
        for base in (
            "downloadstring", "downloaddata", "openread", "openwrite", "uploadstring",
            "uploaddata", "uploadvalues", "getasync", "getstringasync", "getbytearrayasync",
            "getstreamasync", "postasync", "putasync", "patchasync", "deleteasync",
        )
        for name in (base, base + "async", base + "taskasync")
    },
    **{
        name: _m((0, "url"), (1, "write"))
        for name in ("downloadfile", "downloadfileasync", "downloadfiletaskasync")
    },
    **{
        name: _m((0, "url"), (-1, "read"))
        for name in ("uploadfile", "uploadfileasync", "uploadfiletaskasync")
    },
}  # fmt: skip
# Objects whose constructor's first argument names a file or folder, and what
# building one does to it (None: it only names the item, its methods decide).
FILE_OBJECTS: Mapping[str, AccessKind | None] = {
    "io.fileinfo": None,
    "io.directoryinfo": None,
    "io.streamwriter": "write",
    "io.filestream": "write",
    "io.streamreader": "read",
}


def member_call(analysis: Analysis, context: Context, raw: RawCommand) -> None:
    call = raw.call
    assert call is not None
    member = (call.member or "").lower()
    target = call.target
    if call.member is None:
        if target.kind == "type" and target.type_name in _FILE_TYPES:
            analysis.unresolve("path_write", raw.source, "the method it calls is computed")
        return
    if call.static and member == "new" and target.kind == "type":
        if (target.type_name or "") in FILE_OBJECTS:
            kind = FILE_OBJECTS[target.type_name or ""] or "read"
            _apply(analysis, context, raw, _m((0, kind)), call.arguments)
        return
    web = _WEB.get(member)
    if web is not None and not call.static:
        _apply(analysis, context, raw, web, call.arguments)
        return
    if target.kind == "type":
        _static(analysis, context, raw, call, member)
        return
    method = _ITEM.get(member)
    if target.kind == "new" and target.type_name in ("io.fileinfo", "io.directoryinfo"):
        if method is not None:
            items = analysis.resolve_listing(context, raw, target.arguments[:1], recursive=False)
            _on_items(analysis, context, raw, call, method, items)
        return
    listing: Listing | None = None
    if target.kind == "command" and target.offset is not None:
        listing = context.produced.get(target.offset)
    elif target.kind == "item" and target.feed is not None:
        listing = analysis.listing_before(context, target.feed)
    if method is not None and listing is not None and listing.roots is not None:
        _on_items(analysis, context, raw, call, method, listing)
    elif member in _FILE_ONLY:
        analysis.unresolve(
            "path_write",
            raw.source,
            f"it calls {call.member} on an object this analysis cannot follow",
        )


def expression_string(analysis: Analysis, context: Context, raw: RawCommand) -> None:
    """A string an expression names: a clear path is a read, a URL a fetch (tier B)."""
    assert raw.expression is not None
    analysis.tier_b(context, raw, [raw.expression])


def _static(
    analysis: Analysis, context: Context, raw: RawCommand, call: MemberCall, member: str
) -> None:
    type_name = call.target.type_name or ""
    methods = _STATIC.get(type_name)
    if methods is None:
        return
    method = methods.get(member)
    if method is None:
        if type_name in _FILE_TYPES and call.arguments and member != "new":
            method = _WRITE0  # an unlisted method of a file type: its first argument may change
        else:
            return
    if member.startswith(
        ("getfiles", "getdirectories", "getfilesystementries", "enumerate")
    ) and any("alldirectories" in word.source.lower() for word in call.arguments):
        method = _m((0, "subtree_read"))
    _apply(analysis, context, raw, method, call.arguments)


def _on_items(
    analysis: Analysis,
    context: Context,
    raw: RawCommand,
    call: MemberCall,
    method: _Method,
    listing: Listing,
) -> None:
    if listing.roots is None:
        analysis.unresolve(
            "path_write", raw.source, f"it calls {call.member} on items this analysis cannot tell"
        )
        return
    if method.self_kind is not None:
        # Changing a listed folder reaches below it.
        analysis.listed_targets(listing, "read" if method.self_kind == "read" else "write")
    _apply(analysis, context, raw, _Method(method.effects), call.arguments)


def _apply(
    analysis: Analysis,
    context: Context,
    raw: RawCommand,
    method: _Method,
    arguments: Sequence[Word],
) -> None:
    for index, kind in method.effects:
        if not -len(arguments) <= index < len(arguments):
            continue
        word = arguments[index]
        if kind == "url":
            if analysis.literal_of(context, raw, word) is None:
                analysis.unresolve("url", word.source, "the address is computed when it runs")
            else:
                analysis.collect_urls(analysis.value(context, word, analysis.first(context, raw)))
            continue
        with _process_folder(context, raw):
            if kind == "cwd":
                analysis.access(context, raw, "start_dir", word, glob=False, tier="a")
            elif kind in ("subtree", "subtree_read"):
                access: AccessKind = "read" if kind == "subtree_read" else "write"
                analysis.access(context, raw, access, word, glob=False, tier="a", subtree=True)
            else:
                access = "read" if kind == "read" else "write"
                analysis.access(context, raw, access, word, glob=False, tier="a")


@contextlib.contextmanager
def _process_folder(context: Context, raw: RawCommand) -> Iterator[None]:
    """Resolve relative paths against PowerShell's locations and the process folder."""
    saved = context.locations(raw.scope)
    context.states[raw.scope] = tuple(dict.fromkeys((*saved, *context.start)))
    try:
        yield
    finally:
        context.states[raw.scope] = saved
