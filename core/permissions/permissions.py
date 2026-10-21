"""Permission Rules: confine what an Agent's Tools may read, change, run and fetch.

Public API of ``core/permissions/``. The rule language (``normalize_permission_rules``,
``resolve_rule_set``) turns stored rule sets into ``PermissionRules``; a
``PermissionBoundary`` built for one Run from the Agent's resolved rules, the
ceilings it inherited and a ``BoundaryContext`` answers every access check with
``None`` (permitted) or a ``PermissionRefusal``. ``PermissionDenied`` carries a
refusal through a Tool; Tools report it as ``permission_denied``.

Decision order for a path: an unsafe form (network or device path, alternate data
stream) is refused lexically unless an allow rule names it; then protected
locations; then a deny rule of any layer; then every layer must permit through its
base or an allow rule. Both the lexical and the resolved form of the path must
pass. ``PermissionBoundary.UNRESTRICTED`` (full access) answers every check
without any work.
"""

from __future__ import annotations

import functools
import ipaddress
import os
import re
import socket
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import ClassVar, Final, cast
from urllib.parse import urlsplit

from core.permissions._addresses import canonical_host
from core.permissions._describe import (
    LayerDescription,
    describe_layer,
    describe_permission_rules,
)
from core.permissions._paths import (
    LexicalPath,
    NormalizedPath,
    compile_pattern,
    has_wildcard,
    is_inside,
    join_lexical,
    lexical_text,
    literal_prefix,
    normalize_access_path,
    path_key,
    patterns_below,
    pruned_away,
    reaches,
    resolve_text,
    split_lexical,
)
from core.permissions._presets import (
    CUSTOM_PRESET,
    PERMISSION_PRESETS,
    PresetMatch,
    PresetName,
    apply_preset,
    matching_preset,
    preset_definitions,
    preset_name,
)
from core.permissions._refusal import (
    LAYER_SOURCES,
    LayerSource,
    PathAccess,
    PermissionAccess,
    PermissionDenied,
    PermissionRefusal,
    ProtectedLocation,
    RefusalReason,
)
from core.permissions._rules import (
    PLACEHOLDERS,
    RULE_KINDS,
    CommandsBase,
    PermissionRule,
    PermissionRules,
    PermissionRulesError,
    ReadBase,
    RuleKind,
    RuleNormalization,
    RuleSetCheck,
    RuleSetProblem,
    WebBase,
    WriteBase,
    check_permission_rules,
    host_matches,
    normalize_host,
    normalize_permission_rule,
    normalize_permission_rules,
    parse_permission_rule,
    permission_rules_to_dict,
    resolve_rule_set,
    validated_base,
    write_within_read,
)

type PathLike = str | os.PathLike[str]

_LAYER_FIELDS: Final = frozenset(
    {"source", "read", "write", "commands", "web", "workdir", "scratch", "allow", "deny"}
)
_DEFAULT_PORTS: Final = {"http": 80, "https": 443}


@dataclass(frozen=True, slots=True, kw_only=True)
class BoundaryContext:
    """The facts of one Run that a boundary needs, supplied by its caller.

    ``workdir`` is the Session's working directory; ``project_root``,
    ``scratch_root`` and ``workspace`` give ``{project}``, ``{scratch}`` and
    ``{workspace}`` (a rule using a missing one is dropped). ``data_dir`` is
    protected except for the Workspace, the Scratch Area and ``readable_dirs``
    (vBot-owned folders this Session may read, such as its Skill directories and
    artifact folders); ``agent_config`` (the Agent's ``agent.json``) is never
    readable or writable, ``install_dirs`` (installation and interpreter) never
    writable. ``loopback_port`` is the vBot server's own port on loopback.
    """

    workdir: PathLike
    home: PathLike
    data_dir: PathLike
    project_root: PathLike | None = None
    scratch_root: PathLike | None = None
    workspace: PathLike | None = None
    agent_config: PathLike | None = None
    install_dirs: tuple[PathLike, ...] = ()
    readable_dirs: tuple[PathLike, ...] = ()
    loopback_port: int | None = None
    platform: str = sys.platform


@dataclass(frozen=True, slots=True)
class PermissionLayer:
    """One placeholder-free rule set that a Run's accesses must satisfy.

    Bases are always set (defaults filled). ``workdir`` and ``scratch`` are the
    layer's Work Area; path patterns of its rules are absolute.
    """

    source: LayerSource
    read: ReadBase
    write: WriteBase
    commands: CommandsBase
    web: WebBase
    workdir: str
    scratch: str | None
    allow: tuple[PermissionRule, ...]
    deny: tuple[PermissionRule, ...]

    @property
    def shell_allow(self) -> tuple[str, ...]:
        return tuple(rule.pattern for rule in self.allow if rule.kind == "Shell")

    @property
    def shell_deny(self) -> tuple[str, ...]:
        return tuple(rule.pattern for rule in self.deny if rule.kind == "Shell")

    def to_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "read": self.read,
            "write": self.write,
            "commands": self.commands,
            "web": self.web,
            "workdir": self.workdir,
            "scratch": self.scratch,
            "allow": [rule.text for rule in self.allow],
            "deny": [rule.text for rule in self.deny],
        }


@dataclass(frozen=True, slots=True)
class PermissionCeiling:
    """Layers inherited from whoever caused a Run, in their JSON-safe stored form."""

    layers: tuple[PermissionLayer, ...]

    def to_ceiling_dict(self) -> dict[str, object]:
        return {"layers": [layer.to_dict() for layer in self.layers]}

    @classmethod
    def from_ceiling_dict(cls, value: object, *, platform: str = sys.platform) -> PermissionCeiling:
        """Read a stored ceiling strictly; raises :class:`PermissionRulesError`."""
        if not isinstance(value, Mapping) or set(value) != {"layers"}:
            raise PermissionRulesError(
                'A permission ceiling must be an object with exactly the field "layers".'
            )
        layers = value["layers"]
        if not isinstance(layers, list) or not layers:
            raise PermissionRulesError("permission_ceiling.layers must be a non-empty list.")
        windows = platform == "win32"
        return cls(
            tuple(
                _layer_from_dict(item, f"permission_ceiling.layers[{index}]", windows=windows)
                for index, item in enumerate(layers)
            )
        )


class PermissionBoundary:
    """The effective, placeholder-free Permission Rules of one Run.

    Build it with :meth:`build`; ``UNRESTRICTED`` stands for full access. The
    instance is immutable.
    """

    UNRESTRICTED: ClassVar[PermissionBoundary]
    __slots__ = (
        "_context",
        "_layers",
        "_compiled",
        "_windows",
        "_workdir",
        "_home",
        "_data_dirs",
        "_workspace",
        "_scratch",
        "_workdir_area",
        "_readable",
        "_agent_config",
        "_install_dirs",
    )

    def __init__(
        self,
        layers: tuple[PermissionLayer, ...] = (),
        context: BoundaryContext | None = None,
    ) -> None:
        self._layers = layers
        self._context = context
        self._compiled: tuple[_CompiledLayer, ...] = ()
        if not layers or context is None:
            self._layers = ()
            return
        windows = context.platform == "win32"
        self._windows = windows
        self._workdir = lexical_text(os.fspath(context.workdir), windows=windows)
        self._home = lexical_text(os.fspath(context.home), windows=windows)
        self._compiled = tuple(_CompiledLayer(layer, windows=windows) for layer in layers)
        self._data_dirs = _Area.of(os.fspath(context.data_dir), windows=windows)
        self._workspace = _Area.optional(context.workspace, windows=windows)
        self._scratch = _Area.optional(context.scratch_root, windows=windows)
        self._workdir_area = _Area.of(self._workdir, windows=windows)
        self._readable = tuple(
            _Area.of(os.fspath(folder), windows=windows) for folder in context.readable_dirs
        )
        self._agent_config = _Area.optional(context.agent_config, windows=windows)
        self._install_dirs = tuple(
            _Area.of(os.fspath(folder), windows=windows) for folder in context.install_dirs
        )

    @classmethod
    def build(
        cls,
        rules: PermissionRules | None,
        context: BoundaryContext,
        *,
        ceilings: Iterable[PermissionCeiling] = (),
    ) -> PermissionBoundary:
        """The boundary of one Run: the Agent's resolved rules plus inherited ceilings.

        Placeholders expand against ``context``; a rule whose placeholder has no
        value in this Run is dropped. Without ceilings, rules that restrict nothing
        (:attr:`PermissionRules.restricts`) give the Run full access
        (``UNRESTRICTED``): no protected locations either.
        """
        layers: list[PermissionLayer] = []
        if rules is not None and rules.restricts:
            layers.append(_expand(rules, context))
        for ceiling in ceilings:
            layers.extend(ceiling.layers)
        if not layers:
            return cls.UNRESTRICTED
        return cls(tuple(layers), context)

    @property
    def restricted(self) -> bool:
        return bool(self._layers)

    @property
    def layers(self) -> tuple[PermissionLayer, ...]:
        """Every layer, the Agent's own first; each must permit an access."""
        return self._layers

    @property
    def context(self) -> BoundaryContext | None:
        return self._context

    @property
    def loopback_port(self) -> int | None:
        return self._context.loopback_port if self._context is not None else None

    def to_ceiling_dict(self, *, source: LayerSource) -> dict[str, object] | None:
        """This boundary as a ceiling for a Run it causes, or ``None`` for full access.

        The Agent's own layer is stored as ``source`` (``parent`` for a Sub-Agent,
        ``cron`` for a Cron job); inherited layers keep their source.
        """
        ceiling = self.ceiling(source=source)
        return ceiling.to_ceiling_dict() if ceiling is not None else None

    def ceiling(self, *, source: LayerSource) -> PermissionCeiling | None:
        if not self._layers:
            return None
        return PermissionCeiling(
            tuple(
                _retagged(layer, source) if layer.source == "agent" else layer
                for layer in self._layers
            )
        )

    def check_path(
        self,
        path: PathLike,
        access: PathAccess,
        *,
        base: PathLike | None = None,
        subtree: bool = False,
        glob: str | None = None,
        pruned: Sequence[str] = (),
    ) -> PermissionRefusal | None:
        """Whether ``access`` to ``path`` is permitted; relative paths join ``base``.

        ``base`` defaults to the working directory. ``start_dir`` (the directory a
        process starts in) is decided like reading that directory. ``glob`` (a
        relative pattern in rule syntax, ``*.md``) means the access also reaches
        the entries below ``path`` it matches; ``subtree`` that it reaches
        everything below them, or below ``path`` (a recursive delete or
        listing). A protected location or a deny rule that could match what it
        reaches refuses too, except a deny rule only below a folder whose name
        is in ``pruned``, which the access leaves out at every depth. Such a
        refusal names the denied part in ``PermissionRefusal.below``.
        """
        if not self._layers:
            return None
        normalized = normalize_access_path(
            path, base=self._base(base), home=self._home, windows=self._windows
        )
        target = normalized.lexical
        lexical_key = self._key(normalized.lexical)
        if normalized.unsafe is not None:
            if not self._names_exactly(lexical_key, access):
                return self.refusal(access, target, "unsafe_form", location=normalized.unsafe)
            keys: tuple[str, ...] = (lexical_key,)
        else:
            keys = tuple(dict.fromkeys((lexical_key, self._key(normalized.resolved))))
        # Name where a link leads, so a refusal of a path inside reads right.
        shown = normalized.resolved if len(keys) == 2 else None
        location = self._protected(keys, access)
        if location is not None:
            return self.refusal(
                access, target, "protected_location", location=location, resolved=shown
            )
        for layer in self._compiled:
            denied = layer.denying_rule(keys, access)
            if denied is not None:
                return self.refusal(
                    access,
                    target,
                    "deny_rule",
                    rule=denied,
                    source=layer.layer.source,
                    resolved=shown,
                )
        for layer in self._compiled:
            if not all(self._granted(key, access) or layer.permits(key, access) for key in keys):
                return self.refusal(
                    access,
                    target,
                    "not_permitted",
                    source=layer.layer.source,
                    base=layer.layer.write if access == "write" else layer.layer.read,
                    resolved=shown,
                )
        if subtree or glob is not None:
            reach = tuple(glob.split("/")) if glob else ()
            if subtree or not reach:
                reach += ("**",)
            names = frozenset(path_key(name, windows=self._windows) for name in pruned)
            return self._subtree_refusal(keys, access, target, shown, reach, names)
        return None

    def _subtree_refusal(
        self,
        keys: tuple[str, ...],
        access: PathAccess,
        target: str,
        shown: str | None,
        reach: tuple[str, ...],
        pruned: frozenset[str],
    ) -> PermissionRefusal | None:
        """A refusal for what the access reaches below the permitted folder ``keys`` name.

        ``reach`` is the pattern of what it reaches below the folder, as segments.
        """
        protected: list[tuple[ProtectedLocation, _Area | None]] = [
            ("agent_config", self._agent_config),
            ("data_dir", self._data_dirs),
        ]
        if access == "write":
            protected.extend(("installation", area) for area in self._install_dirs)
        for location, area in protected:
            if area is not None and any(
                reaches(key, reach, folder) for folder in area.keys for key in keys
            ):
                return self.refusal(
                    access, target, "protected_location", location=location, resolved=shown
                )
        for layer in self._compiled:
            rules = layer.read_deny + layer.edit_deny if access == "write" else layer.read_deny
            for rule in rules:
                for pattern in rule.keys:
                    for key in keys:
                        if reaches(key, reach, pattern) and not pruned_away(pattern, key, pruned):
                            refusal = self.refusal(
                                access,
                                target,
                                "deny_rule",
                                rule=rule.rule.text,
                                source=layer.layer.source,
                                resolved=shown,
                            )
                            below = sorted(patterns_below(pattern, key))
                            part = target.rstrip("/") + "/" + below[0] if below else target
                            return replace(refusal, below=part.removesuffix("/**"))
        return None

    def refusable_below(self, folder: PathLike, access: PathAccess) -> tuple[str, ...]:
        """Patterns, relative to ``folder``, of what below it may be refused.

        Below a folder the boundary permits, only deny rules, protected locations
        and links leading out can refuse an entry, because bases and allow rules
        always cover whole subtrees. Each pattern uses the rule syntax with ``/``
        separators and covers what it names and everything below; ``**`` stands
        for the whole folder. Together they cover every refused entry except
        links, which a caller checks with :meth:`check_path`. Empty for full
        access and for a folder below which nothing but links can be refused.
        """
        if not self._layers:
            return ()
        normalized = normalize_access_path(
            folder, base=self._workdir, home=self._home, windows=self._windows
        )
        folders = tuple(
            dict.fromkeys((self._key(normalized.lexical), self._key(normalized.resolved)))
        )
        keys: list[str] = []
        for layer in self._compiled:
            rules = layer.read_deny + layer.edit_deny if access == "write" else layer.read_deny
            keys.extend(key for rule in rules for key in rule.keys)
        for area in (self._agent_config, *(self._install_dirs if access == "write" else ())):
            keys.extend(area.keys if area is not None else ())
        if not all(self._data_dir_allowed(key, access) for key in folders):
            keys.extend(self._data_dirs.keys)
        found: dict[str, None] = {}
        for key in keys:
            for below in folders:
                found.update(dict.fromkeys(sorted(patterns_below(key, below))))
        return tuple(found)

    def readable_entries(
        self, folder: PathLike, entries: Iterable[os.DirEntry[str]]
    ) -> list[os.DirEntry[str]]:
        """The entries of a listing of the readable ``folder`` that may be read.

        Only links and junctions are checked unless :meth:`refusable_below`
        names something below the folder, so listing a plain permitted folder
        costs no check per entry; full access checks nothing.
        """
        listed = list(entries)
        if not self._layers:
            return listed
        every = bool(self.refusable_below(folder, "read"))
        kept: list[os.DirEntry[str]] = []
        for entry in listed:
            if not every:
                try:
                    linked = entry.is_symlink() or entry.is_junction()
                except OSError:
                    linked = True
                if not linked:
                    kept.append(entry)
                    continue
            if self.check_path(entry.path, "read") is None:
                kept.append(entry)
        return kept

    def check_host(self, host: str) -> PermissionRefusal | None:
        """Whether a web access to ``host`` is permitted."""
        if not self._layers:
            return None
        normalized = normalize_host(host)
        if not normalized:
            return self.refusal("web", host, "unanalyzable", detail="the URL names no host")
        for layer in self._compiled:
            for rule in layer.web_deny:
                if host_matches(rule.pattern, normalized):
                    return self.refusal(
                        "web", normalized, "deny_rule", rule=rule.text, source=layer.layer.source
                    )
        for layer in self._compiled:
            if layer.layer.web == "listed" and not any(
                host_matches(rule.pattern, normalized) for rule in layer.web_allow
            ):
                return self.refusal(
                    "web", normalized, "not_permitted", source=layer.layer.source, base="listed"
                )
        return None

    def check_url(self, url: str) -> PermissionRefusal | None:
        """Whether fetching ``url`` (http or https) is permitted.

        The vBot server's own loopback port is refused, so an Agent cannot reach
        the server's API to change its own rules.
        """
        if not self._layers:
            return None
        parts = urlsplit(url.strip())
        scheme = parts.scheme.lower()
        try:
            port = parts.port
        except ValueError:
            port = -1
        if scheme not in _DEFAULT_PORTS or not parts.hostname or port == -1:
            return self.refusal(
                "web",
                url.strip(),
                "unanalyzable",
                detail="only http and https URLs with a host and a valid port can be checked",
            )
        # 127.1 and 2130706433 reach 127.0.0.1 too.
        host = canonical_host(normalize_host(parts.hostname))
        effective_port = port if port is not None else _DEFAULT_PORTS[scheme]
        if (
            self.loopback_port is not None
            and effective_port == self.loopback_port
            and is_own_host(host)
        ):
            return self.refusal(
                "web", f"{scheme}://{parts.netloc}", "protected_location", location="vbot_server"
            )
        return self.check_host(host)

    def refusal(
        self,
        access: PermissionAccess,
        target: str,
        reason: RefusalReason,
        *,
        source: LayerSource = "agent",
        rule: str | None = None,
        base: str | None = None,
        location: ProtectedLocation | str | None = None,
        detail: str | None = None,
        resolved: str | None = None,
        hint: str | None = None,
    ) -> PermissionRefusal:
        """A refusal of this boundary with its allowed summary (also for command checks)."""
        return PermissionRefusal(
            access=access,
            target=target,
            reason=reason,
            source=source,
            rule=rule,
            base=base,
            location=location,
            detail=detail,
            resolved=resolved,
            allowed=self.allowed_summary(access),
            hint=hint,
        )

    def allowed_summary(self, access: PermissionAccess) -> tuple[str, ...] | None:
        """The folders, commands or hosts ``access`` may reach; ``None`` for no limit.

        Lists what a restricted layer allows and every other layer permits too.
        """
        if not self._layers:
            return None
        if access == "command":
            return self._listed_summary("Shell", lambda layer: layer.commands == "listed")
        if access == "web":
            return self._listed_summary("Web", lambda layer: layer.web == "listed")
        path_access: PathAccess = access
        restricting = [
            layer for layer in self._compiled if not layer.base_covers_everything(path_access)
        ]
        if not restricting:
            return None
        items: list[str] = []
        for layer in restricting:
            for candidate in layer.candidates(path_access):
                probe = self._key(
                    literal_prefix(split_lexical(candidate, windows=self._windows)).text
                )
                if candidate not in items and all(
                    other.permits(probe, path_access) for other in self._compiled
                ):
                    items.append(candidate)
        return tuple(items)

    def _listed_summary(
        self, kind: RuleKind, listed: Callable[[PermissionLayer], bool]
    ) -> tuple[str, ...] | None:
        restricting = [layer for layer in self._layers if listed(layer)]
        if not restricting:
            return None
        items: list[str] = []
        for layer in restricting:
            for rule in layer.allow:
                if rule.kind != kind or rule.pattern in items:
                    continue
                if all(
                    _listed_in(other, rule.pattern, kind)
                    for other in restricting
                    if other is not layer
                ):
                    items.append(rule.pattern)
        return tuple(items)

    def _base(self, base: PathLike | None) -> str:
        if base is None:
            return self._workdir
        return join_lexical(
            split_lexical(self._workdir, windows=self._windows),
            split_lexical(os.fspath(base), windows=self._windows),
        ).text

    def _key(self, text: str) -> str:
        return path_key(text, windows=self._windows)

    def _granted(self, key: str, access: PathAccess) -> bool:
        if access == "write":
            return False
        return any(area.contains(key) for area in (*self._readable, self._workspace) if area)

    def _names_exactly(self, key: str, access: PathAccess) -> bool:
        return any(layer.names_exactly(key, access) for layer in self._compiled)

    def _protected(self, keys: tuple[str, ...], access: PathAccess) -> ProtectedLocation | None:
        for key in keys:
            if self._agent_config is not None and self._agent_config.contains(key):
                return "agent_config"
            if access == "write" and any(area.contains(key) for area in self._install_dirs):
                return "installation"
            if self._data_dirs.contains(key) and not self._data_dir_allowed(key, access):
                return "data_dir"
        return None

    def _data_dir_allowed(self, key: str, access: PathAccess) -> bool:
        """Whether ``key`` lies in a part of the data directory ``access`` may reach."""
        if access == "write":
            return _contains(self._scratch, key) or (
                _contains(self._workspace, key) and self._workdir_area.contains(key)
            )
        return (
            _contains(self._scratch, key)
            or _contains(self._workspace, key)
            or any(area.contains(key) for area in self._readable)
        )


PermissionBoundary.UNRESTRICTED = PermissionBoundary()


def normalize_path(
    path: PathLike, *, base: PathLike, home: PathLike, platform: str = sys.platform
) -> NormalizedPath:
    """The lexical and resolved form of ``path`` exactly as every check compares it.

    Relative paths join ``base``; ``~`` expands to ``home``. An unsafe form
    (``unsafe`` set) is returned without touching the filesystem.
    """
    windows = platform == "win32"
    return normalize_access_path(
        path,
        base=lexical_text(os.fspath(base), windows=windows),
        home=lexical_text(os.fspath(home), windows=windows),
        windows=windows,
    )


@dataclass(frozen=True, slots=True)
class _Area:
    """A folder in its lexical and resolved comparison keys."""

    keys: tuple[str, ...]

    @classmethod
    def of(cls, text: str, *, windows: bool) -> _Area:
        lexical = split_lexical(text, windows=windows)
        keys = [path_key(lexical.text, windows=windows)]
        if lexical.unsafe is None:
            keys.append(path_key(resolve_text(lexical.text, windows=windows), windows=windows))
        return cls(tuple(dict.fromkeys(keys)))

    @classmethod
    def optional(cls, path: PathLike | None, *, windows: bool) -> _Area | None:
        return None if path is None else cls.of(os.fspath(path), windows=windows)

    def contains(self, key: str) -> bool:
        return any(is_inside(key, folder) for folder in self.keys)


def _contains(area: _Area | None, key: str) -> bool:
    return area is not None and area.contains(key)


@dataclass(frozen=True, slots=True)
class _PathRule:
    rule: PermissionRule
    # Comparison keys of the pattern as written and with its literal prefix resolved.
    keys: tuple[str, ...]
    patterns: tuple[re.Pattern[str], ...]
    literal: str | None

    @classmethod
    def of(cls, rule: PermissionRule, *, windows: bool) -> _PathRule:
        lexical = split_lexical(rule.pattern, windows=windows)
        variants = [lexical.text]
        prefix = literal_prefix(lexical)
        if lexical.unsafe is None and prefix.segments:
            resolved = split_lexical(resolve_text(prefix.text, windows=windows), windows=windows)
            rest = lexical.segments[len(prefix.segments) :]
            variants.append(LexicalPath(resolved.root, resolved.segments + rest).text)
        keys = tuple(dict.fromkeys(path_key(variant, windows=windows) for variant in variants))
        literal = None if has_wildcard(lexical.text) else path_key(lexical.text, windows=windows)
        return cls(rule, keys, tuple(compile_pattern(key) for key in keys), literal)

    def matches(self, key: str) -> bool:
        return any(pattern.fullmatch(key) is not None for pattern in self.patterns)


class _CompiledLayer:
    __slots__ = (
        "layer",
        "workdir",
        "scratch",
        "read_allow",
        "edit_allow",
        "read_deny",
        "edit_deny",
        "web_allow",
        "web_deny",
    )

    def __init__(self, layer: PermissionLayer, *, windows: bool) -> None:
        self.layer = layer
        self.workdir = _Area.of(layer.workdir, windows=windows)
        self.scratch = None if layer.scratch is None else _Area.of(layer.scratch, windows=windows)

        def compiled(rules: tuple[PermissionRule, ...], kind: str) -> tuple[_PathRule, ...]:
            return tuple(_PathRule.of(rule, windows=windows) for rule in rules if rule.kind == kind)

        self.read_allow = compiled(layer.allow, "Read")
        self.edit_allow = compiled(layer.allow, "Edit")
        self.read_deny = compiled(layer.deny, "Read")
        self.edit_deny = compiled(layer.deny, "Edit")
        self.web_allow = tuple(rule for rule in layer.allow if rule.kind == "Web")
        self.web_deny = tuple(rule for rule in layer.deny if rule.kind == "Web")

    def denying_rule(self, keys: tuple[str, ...], access: PathAccess) -> str | None:
        rules = self.read_deny + self.edit_deny if access == "write" else self.read_deny
        for rule in rules:
            if any(rule.matches(key) for key in keys):
                return rule.rule.text
        return None

    def base_covers_everything(self, access: PathAccess) -> bool:
        return (self.layer.write if access == "write" else self.layer.read) == "anywhere"

    def permits(self, key: str, access: PathAccess) -> bool:
        if access == "write":
            base = self.layer.write
            if base == "anywhere" or _contains(self.scratch, key):
                return True
            if base == "work_area" and self.workdir.contains(key):
                return True
            return any(rule.matches(key) for rule in self.edit_allow)
        if self.layer.read == "anywhere" or _contains(self.scratch, key):
            return True
        if self.workdir.contains(key):
            return True
        return any(rule.matches(key) for rule in self.read_allow + self.edit_allow)

    def names_exactly(self, key: str, access: PathAccess) -> bool:
        rules = self.edit_allow if access == "write" else self.read_allow + self.edit_allow
        return any(rule.literal is not None and is_inside(key, rule.literal) for rule in rules)

    def candidates(self, access: PathAccess) -> list[str]:
        items: list[str] = []
        if access != "write" or self.layer.write == "work_area":
            items.append(self.layer.workdir)
        if self.layer.scratch is not None:
            items.append(self.layer.scratch)
        rules = self.edit_allow if access == "write" else self.read_allow + self.edit_allow
        items.extend(rule.rule.pattern for rule in rules)
        return items


def _listed_in(layer: PermissionLayer, pattern: str, kind: RuleKind) -> bool:
    for rule in layer.allow:
        if rule.kind != kind:
            continue
        if rule.pattern in ("*", pattern):
            return True
        if kind == "Web" and host_matches(rule.pattern, pattern.removeprefix("*.")):
            return True
    return False


def _expand(rules: PermissionRules, context: BoundaryContext) -> PermissionLayer:
    windows = context.platform == "win32"
    workdir = lexical_text(os.fspath(context.workdir), windows=windows)
    values: dict[str, str | None] = {
        "workdir": workdir,
        "project": _optional_text(context.project_root, windows=windows),
        "scratch": _optional_text(context.scratch_root, windows=windows),
        "workspace": _optional_text(context.workspace, windows=windows),
        "home": lexical_text(os.fspath(context.home), windows=windows),
    }

    def expanded(listed: tuple[PermissionRule, ...] | None) -> tuple[PermissionRule, ...]:
        result: list[PermissionRule] = []
        for rule in listed or ():
            if rule.kind in ("Read", "Edit"):
                pattern = _expand_pattern(rule.pattern, values, windows=windows)
                if pattern is None:
                    continue
                rule = PermissionRule(rule.kind, pattern)
            if rule not in result:
                result.append(rule)
        return tuple(result)

    read = rules.read or "anywhere"
    return PermissionLayer(
        source="agent",
        read=read,
        write=write_within_read(read, rules.write or "anywhere"),
        commands=rules.commands or "any",
        web=rules.web or "any",
        workdir=workdir,
        scratch=values["scratch"],
        allow=expanded(rules.allow),
        deny=expanded(rules.deny),
    )


def _expand_pattern(pattern: str, values: dict[str, str | None], *, windows: bool) -> str | None:
    if pattern.startswith("{"):
        name, _, rest = pattern[1:].partition("}")
        value = values.get(name)
        if value is None:
            return None
        text = value.rstrip("/") + rest
    elif pattern == "~" or pattern.startswith("~/"):
        text = cast(str, values["home"]).rstrip("/") + pattern[1:]
    else:
        text = pattern
    base = split_lexical(cast(str, values["workdir"]), windows=windows)
    return join_lexical(base, split_lexical(text, windows=windows)).text


def _optional_text(path: PathLike | None, *, windows: bool) -> str | None:
    return None if path is None else lexical_text(os.fspath(path), windows=windows)


def _retagged(layer: PermissionLayer, source: LayerSource) -> PermissionLayer:
    return PermissionLayer(
        source=source,
        read=layer.read,
        write=layer.write,
        commands=layer.commands,
        web=layer.web,
        workdir=layer.workdir,
        scratch=layer.scratch,
        allow=layer.allow,
        deny=layer.deny,
    )


def is_own_host(host: str) -> bool:
    """Whether the normalized ``host`` names this machine, where the vBot API listens.

    Loopback and unspecified addresses in every form the resolver reads
    (``127.1``, ``0x7f000001``), ``localhost`` and this machine's own name.
    Its LAN addresses are not recognized.
    """
    host = canonical_host(host.strip("[]").rstrip("."))
    if host == "localhost" or host.endswith(".localhost"):
        return True
    machine = _machine_name()
    if machine and (host == machine or host.startswith(machine + ".")):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped.is_loopback or address.ipv4_mapped.is_unspecified
    return address.is_loopback or address.is_unspecified


@functools.cache
def _machine_name() -> str:
    return socket.gethostname().casefold().rstrip(".")


def _layer_from_dict(value: object, where: str, *, windows: bool) -> PermissionLayer:
    if not isinstance(value, Mapping):
        raise PermissionRulesError(f"{where} must be an object.")
    unknown = sorted(str(key) for key in value if key not in _LAYER_FIELDS)
    if unknown:
        raise PermissionRulesError(f"{where} has no field {unknown[0]!r}.")
    missing = sorted(_LAYER_FIELDS - {"scratch"} - set(value))
    if missing:
        raise PermissionRulesError(f"{where} needs the field {missing[0]!r}.")
    source = value["source"]
    if source not in LAYER_SOURCES:
        raise PermissionRulesError(
            f"{where}.source must be one of {', '.join(LAYER_SOURCES)}; received {source!r}."
        )
    try:
        bases = {
            part: validated_base(part, value[part]) for part in ("read", "write", "commands", "web")
        }
    except PermissionRulesError as error:
        raise PermissionRulesError(f"{where}.{error}") from None
    workdir = _absolute_folder(value["workdir"], f"{where}.workdir", windows=windows)
    scratch_value = value.get("scratch")
    scratch = (
        None
        if scratch_value is None
        else _absolute_folder(scratch_value, f"{where}.scratch", windows=windows)
    )
    platform = "win32" if windows else "linux"
    rules: dict[str, tuple[PermissionRule, ...]] = {}
    for part in ("allow", "deny"):
        listed = value[part]
        if not isinstance(listed, list):
            raise PermissionRulesError(f"{where}.{part} must be a list of rule strings.")
        parsed: list[PermissionRule] = []
        for index, text in enumerate(listed):
            if not isinstance(text, str):
                raise PermissionRulesError(f"{where}.{part}[{index}] must be a rule string.")
            rule = parse_permission_rule(text, platform=platform)
            if rule.kind in ("Read", "Edit") and (
                rule.pattern.startswith(("{", "~"))
                or not split_lexical(rule.pattern, windows=windows).absolute
            ):
                raise PermissionRulesError(
                    f"{where}.{part}[{index}] {rule.text} must name an absolute path "
                    "without placeholders."
                )
            parsed.append(rule)
        rules[part] = tuple(parsed)
    return PermissionLayer(
        source=cast(LayerSource, source),
        read=cast(ReadBase, bases["read"]),
        write=write_within_read(cast(ReadBase, bases["read"]), cast(WriteBase, bases["write"])),
        commands=cast(CommandsBase, bases["commands"]),
        web=cast(WebBase, bases["web"]),
        workdir=workdir,
        scratch=scratch,
        allow=rules["allow"],
        deny=rules["deny"],
    )


def _absolute_folder(value: object, where: str, *, windows: bool) -> str:
    if not isinstance(value, str):
        raise PermissionRulesError(f"{where} must be an absolute path.")
    lexical = split_lexical(value, windows=windows)
    if not lexical.absolute or lexical.unsafe is not None:
        raise PermissionRulesError(f"{where} must be an absolute path; received {value!r}.")
    return lexical.text


__all__ = [
    "CUSTOM_PRESET",
    "PERMISSION_PRESETS",
    "PLACEHOLDERS",
    "RULE_KINDS",
    "BoundaryContext",
    "CommandsBase",
    "LayerDescription",
    "LayerSource",
    "NormalizedPath",
    "PathAccess",
    "PermissionAccess",
    "PermissionBoundary",
    "PermissionCeiling",
    "PermissionDenied",
    "PermissionLayer",
    "PermissionRefusal",
    "PermissionRule",
    "PermissionRules",
    "PermissionRulesError",
    "PresetMatch",
    "PresetName",
    "ProtectedLocation",
    "ReadBase",
    "RefusalReason",
    "RuleKind",
    "RuleNormalization",
    "RuleSetCheck",
    "RuleSetProblem",
    "WebBase",
    "WriteBase",
    "apply_preset",
    "check_permission_rules",
    "describe_layer",
    "describe_permission_rules",
    "is_own_host",
    "matching_preset",
    "normalize_path",
    "normalize_permission_rule",
    "normalize_permission_rules",
    "parse_permission_rule",
    "permission_rules_to_dict",
    "preset_definitions",
    "preset_name",
    "resolve_rule_set",
]
