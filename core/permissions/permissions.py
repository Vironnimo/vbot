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
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, replace
from typing import ClassVar, Final, cast
from urllib.parse import urlsplit

from core.permissions._addresses import canonical_host
from core.permissions._describe import (
    LayerDescription,
    describe_layer,
    describe_permission_rules,
)
from core.permissions._layers import PermissionCeiling, PermissionLayer, join_ceilings
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
    spelled_like,
    split_lexical,
)
from core.permissions._presets import (
    CUSTOM_PRESET,
    PERMISSION_PRESETS,
    PresetMatch,
    PresetName,
    apply_preset,
    editor_bases,
    matching_preset,
    preset_definitions,
    preset_name,
)
from core.permissions._refusal import (
    HostAccess,
    LayerSource,
    PathAccess,
    PermissionAccess,
    PermissionDenied,
    PermissionRefusal,
    ProtectedLocation,
    RefusalReason,
)
from core.permissions._rules import (
    PERMISSION_RULE_PARTS,
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
    host_forms,
    host_matches,
    intersect_rule_sets,
    normalize_host,
    normalize_permission_rule,
    normalize_permission_rules,
    parse_permission_rule,
    permission_rules_to_dict,
    resolve_rule_set,
    write_within_read,
)

type PathLike = str | os.PathLike[str]

_DEFAULT_PORTS: Final = {"http": 80, "https": 443}


@dataclass(frozen=True, slots=True)
class RefusedBelow:
    """What below a folder a boundary may refuse, in the form a Tool result names it.

    ``rules``: the parts of deny rules below the folder, relative to it and
    spelled as the rules write them. ``protected``: protected locations lie
    below it as well.
    """

    rules: tuple[str, ...] = ()
    protected: bool = False

    def __bool__(self) -> bool:
        return bool(self.rules) or self.protected


@dataclass(frozen=True, slots=True, kw_only=True)
class BoundaryContext:
    """The facts of one Run that a boundary needs, supplied by its caller.

    ``workdir`` is the Session's working directory; ``project_root``,
    ``scratch_root`` and ``workspace`` give ``{project}``, ``{scratch}`` and
    ``{workspace}`` (a rule using a missing one is dropped). The Workspace (an
    Identity Agent's own) is readable and writable under every layer, wherever
    the Session works; only deny rules and the protected locations below refuse
    there. ``data_dir`` is protected except for the Workspace, the Scratch Area
    and ``readable_dirs`` (vBot-owned folders this Session may read, such as its
    Skill directories and artifact folders); ``agent_config`` (the Agent's ``agent.json``) is never
    readable or writable, ``install_dirs`` (installation and interpreter) never
    writable. ``rule_files`` are the repository locations a Team member's rules come
    from (its profile, the settings files its translation reads and every location
    where a definition could replace its profile, also missing ones): readable
    where the rules allow, never writable, also under every Run
    that inherits this boundary as a ceiling. ``loopback_port`` is the vBot
    server's own port on loopback; ``own_hosts`` are the further names and
    addresses that reach it on that port when it listens beyond loopback (this
    machine's interface addresses and names, the access configuration's host
    names).
    """

    workdir: PathLike
    home: PathLike
    data_dir: PathLike
    project_root: PathLike | None = None
    scratch_root: PathLike | None = None
    workspace: PathLike | None = None
    agent_config: PathLike | None = None
    rule_files: tuple[PathLike, ...] = ()
    install_dirs: tuple[PathLike, ...] = ()
    readable_dirs: tuple[PathLike, ...] = ()
    loopback_port: int | None = None
    own_hosts: tuple[str, ...] = ()
    platform: str = sys.platform


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
        "_readable",
        "_agent_config",
        "_rule_files",
        "_install_dirs",
        "_own_hosts",
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
        self._readable = tuple(
            _Area.of(os.fspath(folder), windows=windows) for folder in context.readable_dirs
        )
        self._agent_config = _Area.optional(context.agent_config, windows=windows)
        self._rule_files = _Area.union(
            (*context.rule_files, *(file for layer in layers for file in layer.rule_files)),
            windows=windows,
        )
        self._install_dirs = tuple(
            _Area.of(os.fspath(folder), windows=windows) for folder in context.install_dirs
        )
        self._own_hosts = frozenset(_host_key(host) for host in context.own_hosts)

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
    def limits_writes(self) -> bool:
        """Whether a layer narrows where files may change: a write base or a path deny rule.

        Changes that other Agents see and that no path rule decides, such as of a
        global Skill, are refused then (``skill_manage``).
        """
        return any(
            layer.layer.write != "anywhere" or layer.read_deny or layer.edit_deny
            for layer in self._compiled
        )

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

    def is_server_host(self, host: str) -> bool:
        """Whether ``host`` reaches the vBot server at ``loopback_port``.

        Loopback names and addresses (:func:`is_own_host`) and, when the server
        listens beyond loopback, the context's ``own_hosts``.
        """
        return is_own_host(host.casefold()) or (
            bool(self._layers) and _host_key(host) in self._own_hosts
        )

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
                layer.retagged(source) if layer.source == "agent" else layer
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
        git: bool = False,
        deny_only: bool = False,
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
        ``git``: a git operation across the files below ``path`` (``git pull``,
        ``git restore .``, ``git add .``). Below ``path`` only vBot's own
        protected locations refuse, not deny rules or rule files: git changes
        those files as the repository's history does, and refusing every such
        operation in a repository with a deny rule would refuse most git work.
        ``deny_only``: ``path`` is a word that may name a path (a command
        mention), so only protected locations and deny rules apply, and links
        are followed only when it names an existing entry.
        """
        if not self._layers:
            return None
        normalized = normalize_access_path(
            path,
            base=self._base(base),
            home=self._home,
            windows=self._windows,
            follow_missing=not deny_only,
        )
        target = normalized.lexical
        lexical_key = self._key(normalized.lexical)
        if normalized.unsafe is not None:
            if not deny_only and not self._names_exactly(lexical_key, access):
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
        for layer in () if deny_only else self._compiled:
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
            reach = tuple(path_key(glob, windows=self._windows).split("/")) if glob else ()
            if subtree or not reach:
                reach += ("**",)
            names = frozenset(path_key(name, windows=self._windows) for name in pruned)
            return self._subtree_refusal(keys, access, target, shown, reach, names, git)
        return None

    def _subtree_refusal(
        self,
        keys: tuple[str, ...],
        access: PathAccess,
        target: str,
        shown: str | None,
        reach: tuple[str, ...],
        pruned: frozenset[str],
        git: bool,
    ) -> PermissionRefusal | None:
        """A refusal for what the access reaches below the permitted folder ``keys`` name.

        ``reach`` is the pattern of what it reaches below the folder, as segments.
        """
        protected: list[tuple[ProtectedLocation, _Area | None]] = [
            ("agent_config", self._agent_config)
        ]
        # A folder in a part of the data directory the access may reach (the
        # Scratch Area, the Workspace, a readable folder) has only such parts below.
        if not all(self._data_dir_allowed(key, access) for key in keys):
            protected.append(("data_dir", self._data_dirs))
        if access == "write" and not git:
            protected.append(("agent_config", self._rule_files))
        if access == "write":
            protected.extend(("installation", area) for area in self._install_dirs)
        for location, area in protected:
            if area is not None and any(
                reaches(key, reach, folder) for folder in area.keys for key in keys
            ):
                return self.refusal(
                    access, target, "protected_location", location=location, resolved=shown
                )
        for layer in () if git else self._compiled:
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
        return tuple(dict.fromkeys(pattern for pattern, _ in self._refusable(folder, access)))

    def refused_below(self, folder: PathLike, access: PathAccess) -> RefusedBelow:
        """What below ``folder`` may be refused, as a Tool result names it to the Agent.

        The deny rules' parts as :meth:`refusable_below` gives them, but spelled
        as the rules write them; protected locations only as a flag, since the
        System Prompt names no path of vBot's own.
        """
        rules: dict[str, None] = {}
        protected = False
        for _, spelled in self._refusable(folder, access):
            if spelled is None:
                protected = True
            else:
                rules.setdefault(spelled)
        return RefusedBelow(tuple(rules), protected)

    def _refusable(self, folder: PathLike, access: PathAccess) -> Iterator[tuple[str, str | None]]:
        """Each refusable pattern below ``folder``, with its deny rule's spelling.

        The spelling is ``None`` for a protected location.
        """
        if not self._layers:
            return
        normalized = normalize_access_path(
            folder, base=self._workdir, home=self._home, windows=self._windows
        )
        folders = tuple(
            dict.fromkeys((self._key(normalized.lexical), self._key(normalized.resolved)))
        )
        sources: list[tuple[str, str | None]] = []
        for layer in self._compiled:
            rules = layer.read_deny + layer.edit_deny if access == "write" else layer.read_deny
            sources.extend((key, rule.rule.pattern) for rule in rules for key in rule.keys)
        writes = (self._rule_files, *self._install_dirs) if access == "write" else ()
        for area in (self._agent_config, *writes):
            sources.extend((key, None) for key in (area.keys if area is not None else ()))
        if not all(self._data_dir_allowed(key, access) for key in folders):
            sources.extend((key, None) for key in self._data_dirs.keys)
        for key, written in sources:
            for below in folders:
                for pattern in sorted(patterns_below(key, below)):
                    yield pattern, None if written is None else spelled_like(pattern, key, written)

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

    def check_host(
        self, host: str, access: HostAccess = "web", *, by_program: bool = False
    ) -> PermissionRefusal | None:
        """Whether fetching from ``host`` (``connect``: reaching it at all) is permitted.

        ``Web(...)`` rules decide both; ``connect`` only words the refusal for a
        network program such as ssh. vBot requests the nontransitional IDNA form
        of a name; for a program a command runs (``by_program``), every form it
        may request must pass (``host_forms``).
        """
        if not self._layers:
            return None
        if not host.strip().strip("[]").rstrip("."):
            return self.refusal(
                access, f'"{host}"', "unanalyzable", detail="the address names no host"
            )
        forms = host_forms(host)
        if not forms:
            return self.refusal(
                access,
                f'"{_visible(host)}"',
                "unanalyzable",
                detail="it is not a host name every program reads the same way",
            )
        if not by_program:
            forms = forms[:1]
        for layer in self._compiled:
            for rule in layer.web_deny:
                for form in forms:
                    if host_matches(rule.pattern, form):
                        return self.refusal(
                            access, form, "deny_rule", rule=rule.text, source=layer.layer.source
                        )
        for layer in self._compiled:
            if layer.layer.web != "listed":
                continue
            for form in forms:
                if not any(host_matches(rule.pattern, form) for rule in layer.web_allow):
                    return self.refusal(
                        access, form, "not_permitted", source=layer.layer.source, base="listed"
                    )
        return None

    def check_url(self, url: str, *, by_program: bool = False) -> PermissionRefusal | None:
        """Whether fetching ``url`` (http or https) is permitted (``by_program``: see check_host).

        The vBot server's own loopback port is refused, so an Agent cannot reach
        the server's API to change its own rules.
        """
        if not self._layers:
            return None
        try:
            parts = urlsplit(url.strip())
            port = parts.port
        except ValueError:
            parts = None
            port = None
        scheme = parts.scheme.lower() if parts is not None else ""
        if parts is None or scheme not in _DEFAULT_PORTS or not parts.hostname:
            return self.refusal(
                "web",
                url.strip(),
                "unanalyzable",
                detail="only http and https URLs with a host and a valid port can be checked",
            )
        effective_port = port if port is not None else _DEFAULT_PORTS[scheme]
        if (
            self.loopback_port is not None
            and effective_port == self.loopback_port
            and any(self.is_server_host(form) for form in host_forms(parts.hostname))
        ):
            return self.refusal(
                "web", f"{scheme}://{parts.netloc}", "protected_location", location="vbot_server"
            )
        return self.check_host(parts.hostname, by_program=by_program)

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

        Lists what a restricted layer allows and every other layer permits too,
        and the Workspace, which every layer lets the Agent read and change.
        A folder to work in (``start_dir``) is named without the wildcard part
        of its rule. Skill folders and the Session's own vBot files stay
        readable but are not listed.
        """
        if not self._layers:
            return None
        if access == "command":
            return self._listed_summary("Shell", lambda layer: layer.commands == "listed")
        if access == "web" or access == "connect":
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
                folder = literal_prefix(split_lexical(candidate, windows=self._windows)).text
                shown = folder if path_access == "start_dir" else candidate
                if shown not in items and all(
                    other.permits(self._key(folder), path_access) for other in self._compiled
                ):
                    items.append(shown)
        workspace = self._context.workspace if self._context is not None else None
        if workspace is not None:
            text = lexical_text(os.fspath(workspace), windows=self._windows)
            if text not in items:
                items.append(text)
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
        if _contains(self._workspace, key):
            return True
        return access != "write" and any(area.contains(key) for area in self._readable)

    def _names_exactly(self, key: str, access: PathAccess) -> bool:
        # Every layer refuses unsafe forms by itself, so each must name the path.
        return all(layer.names_exactly(key, access) for layer in self._compiled)

    def _protected(self, keys: tuple[str, ...], access: PathAccess) -> ProtectedLocation | None:
        for key in keys:
            if self._agent_config is not None and self._agent_config.contains(key):
                return "agent_config"
            if access == "write" and self._rule_files.contains(key):
                return "agent_config"
            if access == "write" and any(area.contains(key) for area in self._install_dirs):
                return "installation"
            if self._data_dirs.contains(key) and not self._data_dir_allowed(key, access):
                return "data_dir"
        return None

    def _data_dir_allowed(self, key: str, access: PathAccess) -> bool:
        """Whether ``key`` lies in a part of the data directory ``access`` may reach."""
        if access == "write":
            return _contains(self._scratch, key) or _contains(self._workspace, key)
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

    @classmethod
    def union(cls, paths: Iterable[PathLike], *, windows: bool) -> _Area:
        """One area of several paths; empty (containing nothing) without any."""
        keys = (key for path in paths for key in cls.of(os.fspath(path), windows=windows).keys)
        return cls(tuple(dict.fromkeys(keys)))

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
        rule_files=tuple(
            dict.fromkeys(
                lexical_text(os.fspath(file), windows=windows) for file in context.rule_files
            )
        ),
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


def is_own_host(host: str) -> bool:
    """Whether the normalized ``host`` names this machine, where the vBot API listens.

    Loopback and unspecified addresses in every form the resolver reads
    (``127.1``, ``0x7f000001``), ``localhost`` and this machine's own name.
    Its LAN addresses only a boundary knows (``BoundaryContext.own_hosts``,
    :meth:`PermissionBoundary.is_server_host`).
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


def _visible(text: str) -> str:
    """*text* with characters that do not print as an escape, such as a zero-width joiner."""
    return "".join(c if c.isprintable() else f"\\u{ord(c):04x}" for c in text)


def _host_key(host: str) -> str:
    """One comparison form of a host name or address (no brackets, zone or IPv4 mapping)."""
    text = canonical_host(host.strip().strip("[]").rstrip(".").casefold().partition("%")[0])
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return text
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return str(address.ipv4_mapped)
    return address.compressed


__all__ = [
    "CUSTOM_PRESET",
    "PERMISSION_PRESETS",
    "PERMISSION_RULE_PARTS",
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
    "editor_bases",
    "check_permission_rules",
    "describe_layer",
    "describe_permission_rules",
    "intersect_rule_sets",
    "is_own_host",
    "join_ceilings",
    "matching_preset",
    "normalize_host",
    "normalize_path",
    "normalize_permission_rule",
    "normalize_permission_rules",
    "parse_permission_rule",
    "permission_rules_to_dict",
    "preset_definitions",
    "preset_name",
    "resolve_rule_set",
]
