"""Exact, per-invocation authorization for missing Permission Rules grants."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from core.permissions._paths import is_inside, normalize_access_path, path_key
from core.permissions._refusal import HostAccess, PathAccess, PermissionRefusal
from core.permissions._rules import host_forms, normalize_host
from core.permissions.permissions import PermissionBoundary


@dataclass(frozen=True, slots=True)
class ApprovalRequirement:
    kind: str
    target: str
    access: str
    computer_id: str = "server"
    resolved: str | None = None
    subtree: bool = False
    glob: str | None = None
    pruned: tuple[str, ...] = ()
    git: bool = False
    shell: str | None = None
    cwd: str | None = None
    command_identity: tuple[tuple[str, bool], ...] = ()
    filtered: bool = False
    host_aliases: tuple[str, ...] = ()

    @classmethod
    def path(
        cls,
        boundary: PermissionBoundary,
        path: str | Path,
        access: PathAccess,
        *,
        base: str | Path | None = None,
        subtree: bool = False,
        glob: str | None = None,
        pruned: Sequence[str] = (),
        git: bool = False,
        computer_id: str = "server",
        filtered: bool = False,
    ) -> ApprovalRequirement:
        context = boundary.context
        windows = (context.platform if context else sys.platform) == "win32"
        folder = os.fspath(base or (context.workdir if context else Path.cwd()))
        home = os.fspath(context.home if context else Path.home())
        normalized = normalize_access_path(path, base=folder, home=home, windows=windows)
        return cls(
            "path",
            path_key(normalized.lexical, windows=windows),
            access,
            computer_id,
            path_key(normalized.resolved, windows=windows),
            subtree,
            path_key(glob, windows=windows) if glob else None,
            tuple(sorted(path_key(item, windows=windows) for item in pruned)),
            git,
            filtered=filtered,
        )

    @classmethod
    def command(
        cls,
        command: str,
        *,
        shell: str,
        cwd: str | Path,
        computer_id: str = "server",
        identity: Sequence[tuple[str, bool]] = (),
    ) -> ApprovalRequirement:
        return cls(
            "command",
            command,
            "command",
            computer_id,
            shell=shell,
            cwd=os.path.normcase(os.path.realpath(cwd)),
            command_identity=tuple(identity),
        )

    @classmethod
    def host(cls, host: str, access: str, *, computer_id: str = "server") -> ApprovalRequirement:
        forms = host_forms(host)
        return cls("host", normalize_host(host) or host, access, computer_id, host_aliases=forms)

    @classmethod
    def url(cls, url: str, *, computer_id: str = "server") -> ApprovalRequirement:
        parts = urlsplit(url.strip())
        host = normalize_host(parts.hostname or "") or parts.hostname or ""
        host = f"[{host}]" if ":" in host and not host.startswith("[") else host
        port = parts.port
        scheme = parts.scheme.lower()
        suffix = (
            f":{port}"
            if port is not None and (scheme, port) not in (("http", 80), ("https", 443))
            else ""
        )
        # Keep userinfo as supplied: changing credentials changes the invocation.
        userinfo = parts.netloc.rpartition("@")[0] + "@" if "@" in parts.netloc else ""
        target = urlunsplit((scheme, userinfo + host + suffix, parts.path or "/", parts.query, ""))
        return cls("url", target, "web", computer_id)

    @classmethod
    def review(cls, target: str, *, computer_id: str = "server") -> ApprovalRequirement:
        return cls("review", target, "review", computer_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "target": self.target,
            "access": self.access,
            "computer_id": self.computer_id,
            "resolved": self.resolved,
            "subtree": self.subtree,
            "glob": self.glob,
            "pruned": list(self.pruned),
            "git": self.git,
            "shell": self.shell,
            "cwd": self.cwd,
            "command_identity": [list(word) for word in self.command_identity],
            "filtered": self.filtered,
            "host_aliases": list(self.host_aliases),
        }

    @property
    def key(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @property
    def suppression_key(self) -> str:
        # A link and its target name the same requested access for rejection.
        target = self.resolved or self.target
        if self.kind == "command" and self.command_identity:
            target = ""
        access = self.access
        if self.kind == "path" and access == "start_dir":
            access = "read"
        elif self.kind == "host" and access == "connect":
            access = "web"
        item = replace(self, target=target, resolved=None, access=access)
        return item.key

    def suppression_keys(self) -> tuple[str, ...]:
        keys = [self.suppression_key]
        if self.kind == "path" and self.access == "write":
            keys.append(replace(self, access="read").suppression_key)
        return tuple(keys)


class PermissionGrant:
    """An exact-call overlay: only missing grants, never hard refusals, are bypassed."""

    def __init__(self, boundary: PermissionBoundary, *, computer_id: str = "server") -> None:
        self.boundary = boundary
        self.computer_id = computer_id
        self._approved: set[str] = set()

    def approve(self, requirements: Sequence[ApprovalRequirement]) -> None:
        self._approved.update(requirement.key for requirement in requirements)

    def permits(self, requirement: ApprovalRequirement) -> bool:
        return requirement.key in self._approved

    def check_path(
        self,
        path: str | Path,
        access: PathAccess,
        *,
        base: str | Path | None = None,
        subtree: bool = False,
        glob: str | None = None,
        pruned: Sequence[str] = (),
        git: bool = False,
        deny_only: bool = False,
    ) -> PermissionRefusal | None:
        refusal = self.boundary.check_path(
            path,
            access,
            base=base,
            subtree=subtree,
            glob=glob,
            pruned=pruned,
            git=git,
            deny_only=deny_only,
        )
        if refusal is None or refusal.reason != "not_permitted":
            return refusal
        requirement = ApprovalRequirement.path(
            self.boundary,
            path,
            access,
            base=base,
            subtree=subtree,
            glob=glob,
            pruned=pruned,
            git=git,
            computer_id=self.computer_id,
        )
        if requirement.key in self._approved:
            return None
        if access == "read" and not subtree and glob is None:
            filtered = replace(requirement, subtree=True, filtered=True)
            if filtered.key in self._approved:
                return None
        return refusal

    def output_write_allowed(
        self, path: str | Path, root: str | Path, *, base: str | Path | None = None
    ) -> bool:
        """Check one generated output directly inside an approved output folder."""
        root_access = ApprovalRequirement.path(
            self.boundary, root, "write", base=base, computer_id=self.computer_id
        )
        if root_access.key not in self._approved:
            return False
        candidate = ApprovalRequirement.path(
            self.boundary, path, "write", base=base, computer_id=self.computer_id
        )
        prefix = root_access.target.rstrip("/") + "/"
        relative = candidate.target.removeprefix(prefix)
        if relative == candidate.target or "/" in relative or not relative:
            return False
        if not is_inside(
            candidate.resolved or candidate.target, root_access.resolved or root_access.target
        ):
            return False
        return self.boundary.check_path(path, "write", base=base, deny_only=True) is None

    def listing_read_allowed(
        self, path: str | Path, root: str | Path, *, base: str | Path | None = None
    ) -> bool:
        """Allow an immediate directory entry under an approved listing target."""
        root_access = ApprovalRequirement.path(
            self.boundary, root, "read", base=base, computer_id=self.computer_id
        )
        if root_access.key not in self._approved:
            return False
        candidate = ApprovalRequirement.path(
            self.boundary, path, "read", base=base, computer_id=self.computer_id
        )
        prefix = root_access.target.rstrip("/") + "/"
        relative = candidate.target.removeprefix(prefix)
        if relative == candidate.target or "/" in relative or not relative:
            return False
        if not is_inside(
            candidate.resolved or candidate.target, root_access.resolved or root_access.target
        ):
            return False
        return self.boundary.check_path(path, "read", base=base, deny_only=True) is None

    def search_read_allowed(
        self, path: str | Path, root: str | Path, *, base: str | Path | None = None
    ) -> bool:
        """Allow filtered enumeration within an approved explicit search root."""
        root_access = ApprovalRequirement.path(
            self.boundary,
            root,
            "read",
            base=base,
            computer_id=self.computer_id,
            subtree=True,
            filtered=True,
        )
        if root_access.key not in self._approved:
            return False
        candidate = ApprovalRequirement.path(
            self.boundary, path, "read", base=base, computer_id=self.computer_id
        )
        if not (
            is_inside(candidate.target, root_access.target)
            and is_inside(
                candidate.resolved or candidate.target, root_access.resolved or root_access.target
            )
        ):
            return False
        return self.boundary.check_path(path, "read", base=base, deny_only=True) is None

    def check_url(self, url: str, *, by_program: bool = False) -> PermissionRefusal | None:
        refusal = self.boundary.check_url(url, by_program=by_program)
        if refusal is None or refusal.reason != "not_permitted":
            return refusal
        requirement = ApprovalRequirement.url(url, computer_id=self.computer_id)
        return None if requirement.key in self._approved else refusal

    def check_host(
        self, host: str, access: HostAccess = "web", *, by_program: bool = False
    ) -> PermissionRefusal | None:
        refusal = self.boundary.check_host(host, access, by_program=by_program)
        if refusal is None or refusal.reason != "not_permitted":
            return refusal
        requirement = ApprovalRequirement.host(host, access, computer_id=self.computer_id)
        return None if requirement.key in self._approved else refusal
