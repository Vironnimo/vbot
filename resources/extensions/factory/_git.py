"""Bounded read-only Git facts; worktree creation and integration stay with Agents."""

from __future__ import annotations

import asyncio
import json
import os
import re
from contextlib import suppress
from itertools import pairwise
from pathlib import Path
from typing import Any

from .agent_text import error_message
from .store import FactoryError

_TIMEOUT = 10.0
_OUTPUT_LIMIT = 64 * 1024
_HISTORY_LIMIT = 256
_REFLOG_LIMIT = 128
_SHA = re.compile(r"[0-9a-f]{40,64}\Z")
_SHA_HINT = re.compile(r"[0-9a-fA-F]{7,64}\Z")


class GitRepository:
    def __init__(self, cwd: str | Path) -> None:
        self.cwd = Path(cwd)

    async def _run(self, *arguments: str, allowed: tuple[int, ...] = (0,)) -> tuple[int, str]:
        process: asyncio.subprocess.Process | None = None

        async def bounded(stream: asyncio.StreamReader | None) -> bytes:
            if stream is None:
                return b""
            result = await stream.read(_OUTPUT_LIMIT + 1)
            while len(result) <= _OUTPUT_LIMIT:
                part = await stream.read(_OUTPUT_LIMIT + 1 - len(result))
                if not part:
                    return result
                result += part
            raise FactoryError("git_output_limit", error_message("git_output_limit"))

        try:
            async with asyncio.timeout(_TIMEOUT):
                process = await asyncio.create_subprocess_exec(
                    "git",
                    "--no-optional-locks",
                    "--no-replace-objects",
                    "-c",
                    "core.fsmonitor=false",
                    "-c",
                    "core.pager=cat",
                    *arguments,
                    cwd=self.cwd,
                    env={
                        key: value
                        for key, value in os.environ.items()
                        if not key.upper().startswith("GIT_")
                    },
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                output, _errors, code = await asyncio.gather(
                    bounded(process.stdout), bounded(process.stderr), process.wait()
                )
                if code not in allowed:
                    raise FactoryError("git_failed", error_message("git_failed"))
                # NUL-delimited Git paths can contain whitespace and non-UTF-8
                # bytes. Keep their identities until comparison and rendering.
                return code, output.decode("utf-8", errors="surrogateescape").removesuffix("\n")
        except TimeoutError as error:
            raise FactoryError("git_timeout", error_message("git_timeout")) from error
        except OSError as error:
            raise FactoryError("git_unavailable", error_message("git_unavailable")) from error
        finally:
            if process is not None and process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
                await process.wait()

    async def validate_branch(self, value: str) -> None:
        # --branch also accepts @{-N} as a checkout-history expression. References
        # submitted by an Agent must instead name precisely the branch it supplied.
        if (
            not isinstance(value, str)
            or not value
            or value.startswith("-")
            or "@{" in value
            or "\x00" in value
        ):
            raise FactoryError("invalid_ref", error_message("invalid_ref", value=value))
        code, canonical = await self._run(
            "check-ref-format", "--branch", value, allowed=(0, 128, 1)
        )
        if code or canonical != value:
            raise FactoryError("invalid_ref", error_message("invalid_ref", value=value))

    async def head(self, branch: str, *, missing_ok: bool = False) -> str | None:
        await self.validate_branch(branch)
        code, head = await self._run(
            "rev-parse",
            "--verify",
            "--end-of-options",
            f"refs/heads/{branch}^{{commit}}",
            allowed=(0, 128, 1),
        )
        if code or not _SHA.fullmatch(head):
            if missing_ok:
                return None
            raise FactoryError("missing_branch", error_message("missing_branch", value=branch))
        return head

    async def base(self, base: str) -> str:
        await self.validate_branch(base)
        code, resolved = await self._run(
            "rev-parse", "--verify", "--end-of-options", f"{base}^{{commit}}", allowed=(0, 128, 1)
        )
        if code or not _SHA.fullmatch(resolved):
            raise FactoryError("missing_base", error_message("missing_base", value=base))
        return resolved

    async def submission(
        self, branch: str, base: str, *, head_sha: str | None = None, base_sha: str | None = None
    ) -> dict[str, str]:
        if head_sha is not None:
            if not _SHA.fullmatch(head_sha):
                raise FactoryError("git_failed", error_message("git_failed"))
            await self.validate_branch(branch)
        head = head_sha if head_sha is not None else await self.head(branch)
        assert head is not None
        if base_sha is not None and not _SHA.fullmatch(base_sha):
            raise FactoryError("git_failed", error_message("git_failed"))
        base_head = await self.base(base_sha if base_sha is not None else base)
        _, count = await self._run("rev-list", "--count", f"{base_head}..{head}")
        if not count.isdecimal() or int(count) == 0:
            raise FactoryError("no_commits", error_message("no_commits"))
        code, merge_base = await self._run("merge-base", base_head, head, allowed=(0, 1))
        if code or not _SHA.fullmatch(merge_base):
            raise FactoryError("unrelated_history", error_message("unrelated_history"))
        if base_sha is not None and merge_base != base_sha:
            raise FactoryError("review_base_changed", error_message("review_base_changed"))
        return {"branch": branch, "base": base, "base_sha": merge_base, "head_sha": head}

    async def integration(
        self,
        branch: str,
        base: str,
        *,
        reviewed_base: str,
        reviewed_head: str,
        expected_branch_head: str | None = None,
        accepted_base: str | None = None,
        base_commit: str | None = None,
        reported_reference: str | None = None,
        verify_merged: bool = True,
    ) -> dict[str, Any]:
        """Apply one deadline to the entire read-only integration inspection."""
        try:
            async with asyncio.timeout(_TIMEOUT):
                return await self._inspect_integration(
                    branch,
                    base,
                    reviewed_base=reviewed_base,
                    reviewed_head=reviewed_head,
                    expected_branch_head=expected_branch_head,
                    accepted_base=accepted_base,
                    base_commit=base_commit,
                    reported_reference=reported_reference,
                    verify_merged=verify_merged,
                )
        except TimeoutError as error:
            raise FactoryError("git_timeout", error_message("git_timeout")) from error

    async def _inspect_integration(
        self,
        branch: str,
        base: str,
        *,
        reviewed_base: str,
        reviewed_head: str,
        expected_branch_head: str | None,
        accepted_base: str | None,
        base_commit: str | None,
        reported_reference: str | None,
        verify_merged: bool,
    ) -> dict[str, Any]:
        """Inspect immutable integration facts without changing the repository.

        A landing-merge review can differ from the submitted branch tip, so the
        caller retains that tip as expected_branch_head across the review.
        An unknown already-landed rewrite keeps its captured facts, but exposes
        an empty review base: the caller must request an explicit base, not mark
        the integration complete or launch an empty-range review.
        """
        expected = reviewed_head if expected_branch_head is None else expected_branch_head
        for value in (reviewed_base, reviewed_head, expected, accepted_base, base_commit):
            if value is not None and not _SHA.fullmatch(value):
                raise FactoryError("git_failed", error_message("git_failed"))
        branch_head = await self.head(branch, missing_ok=True)
        hint = await self._reference_commit(reported_reference) if branch_head is None else None
        moved = branch_head is not None and branch_head != expected
        head = branch_head if moved else reviewed_head
        assert head is not None
        facts: dict[str, Any] = {
            "branch_head_sha": branch_head,
            "target_sha": "",
            "landed": False,
            "review_required": moved,
            "base_sha": reviewed_base,
            "head_sha": head,
            "landing_sha": "",
            "extra_paths": [],
            "merge_paths": [],
        }
        # References are not assertions of a local merge. Preserve that
        # distinction, including Project cleanup that deleted the branch.
        if not verify_merged and not moved and hint in {None, reviewed_head}:
            return facts
        target = await self.base(base)
        facts["target_sha"] = target
        # Cleanup can remove a rebased branch before the report. A commit hint
        # recovers that rewritten candidate, but an ordinary merge reference
        # must not replace the reviewed feature head already present on target.
        if (
            hint is not None
            and hint != reviewed_head
            and not await self._ancestor(reviewed_head, target)
        ):
            head = hint
            moved = True
            facts.update(head_sha=head, review_required=True)
        if moved:
            # A related rewritten base is valid for a fresh review; an entirely
            # different history is not a continuation of the accepted change.
            await self._merge_base(reviewed_base, head)
        landed = await self._ancestor(head, target)
        facts["landed"] = landed
        if moved:
            if landed:
                landing, parent = await self._landing(head, target)
                facts["landing_sha"] = landing
                if landing != head:
                    assert parent is not None
                    if base_commit is not None:
                        await self._pinned_base(base_commit, landing)
                    return await self._inspect_merge(
                        facts,
                        reviewed_base=reviewed_base,
                        reviewed_head=reviewed_head,
                        landing=landing,
                        parent=parent,
                        force_review=True,
                    )
                review_base, source = await self._integration_base(
                    base, target, head, accepted_base=accepted_base, base_commit=base_commit
                )
            else:
                review_base = await self._merge_base(target, head)
                source = "merge_base"
            facts.update(base_sha=review_base, base_source=source)
            return facts
        if not landed:
            return facts
        landing, parent = await self._landing(head, target)
        facts["landing_sha"] = landing
        if landing == head:
            return facts
        assert parent is not None
        return await self._inspect_merge(
            facts,
            reviewed_base=reviewed_base,
            reviewed_head=reviewed_head,
            landing=landing,
            parent=parent,
            force_review=False,
        )

    async def _inspect_merge(
        self,
        facts: dict[str, Any],
        *,
        reviewed_base: str,
        reviewed_head: str,
        landing: str,
        parent: str,
        force_review: bool,
    ) -> dict[str, Any]:
        reviewed_paths = await self._changed_paths(reviewed_base, reviewed_head)
        extra_paths = await self._changed_paths(parent, landing) - reviewed_paths
        # Combined diff finds paths whose result differs from all merge
        # parents, including some automatic overlaps. This is a conservative
        # review gate, not merge simulation or proof of every resolution.
        _, combined = await self._run(
            "diff-tree", "--cc", "--name-only", "--no-commit-id", "-r", "-z", landing, "--"
        )
        merge_paths = self._paths(combined)
        facts.update(
            extra_paths=self._display_paths(extra_paths),
            merge_paths=self._display_paths(merge_paths),
        )
        if force_review or extra_paths or merge_paths:
            facts.update(
                review_required=True,
                base_sha=parent,
                head_sha=landing,
                base_source="landing_parent",
            )
        return facts

    async def _reference_commit(self, value: str | None) -> str | None:
        if not isinstance(value, str) or not _SHA_HINT.fullmatch(value):
            return None
        prefix = value.lower()
        # Object disambiguation, not revision/ref-name interpretation: a branch
        # named like a SHA prefix is not evidence of that commit's identity.
        code, output = await self._run("rev-parse", f"--disambiguate={prefix}", allowed=(0, 1, 128))
        matches = output.splitlines()
        if code or len(matches) != 1:
            return None
        sha = matches[0]
        if not _SHA.fullmatch(sha) or not sha.startswith(prefix):
            raise FactoryError("git_failed", error_message("git_failed"))
        code, commit = await self._run(
            "rev-parse", "--verify", "--end-of-options", f"{sha}^{{commit}}", allowed=(0, 1, 128)
        )
        return sha if not code and commit == sha else None

    async def _ancestor(self, ancestor: str, descendant: str) -> bool:
        code, _ = await self._run(
            "merge-base", "--is-ancestor", ancestor, descendant, allowed=(0, 1)
        )
        return code == 0

    async def _merge_base(self, base: str, head: str) -> str:
        code, common = await self._run("merge-base", base, head, allowed=(0, 1))
        if code or not _SHA.fullmatch(common):
            raise FactoryError("unrelated_history", error_message("unrelated_history"))
        return common

    async def _landing(self, head: str, target: str) -> tuple[str, str | None]:
        if head == target:
            return head, None
        _, output = await self._run(
            "rev-list",
            "--first-parent",
            "--ancestry-path",
            "--parents",
            f"--max-count={_HISTORY_LIMIT + 1}",
            f"{head}..{target}",
        )
        commits = [line.split() for line in output.splitlines()]
        if len(commits) > _HISTORY_LIMIT:
            raise FactoryError("git_output_limit", error_message("git_output_limit"))
        if not commits or any(not _SHA.fullmatch(sha) for row in commits for sha in row):
            raise FactoryError("git_failed", error_message("git_failed"))
        oldest, *parents = commits[-1]
        if not parents:
            raise FactoryError("git_failed", error_message("git_failed"))
        if await self._ancestor(head, parents[0]):
            return head, None
        # The first-parent path first acquires head here through another parent.
        if len(parents) < 2:
            raise FactoryError("git_failed", error_message("git_failed"))
        return oldest, parents[0]

    async def _pinned_base(self, base: str, head: str) -> str:
        resolved = await self.base(base)
        if resolved != base:
            raise FactoryError("missing_base", error_message("missing_base", value=base))
        common = await self._merge_base(resolved, head)
        if common != resolved:
            raise FactoryError("review_base_changed", error_message("review_base_changed"))
        if resolved == head:
            raise FactoryError("no_commits", error_message("no_commits"))
        return resolved

    async def _integration_base(
        self,
        base: str,
        target: str,
        head: str,
        *,
        accepted_base: str | None,
        base_commit: str | None,
    ) -> tuple[str, str]:
        if base_commit is not None:
            return await self._pinned_base(base_commit, head), "base_commit"
        previous = await self._pre_landing_base(base, target, head)
        if previous is not None:
            return previous, "reflog"
        if (
            accepted_base is not None
            and accepted_base != head
            and await self._ancestor(accepted_base, head)
        ):
            return accepted_base, "accepted_base"
        return "", "unknown"

    async def _pre_landing_base(self, base: str, target: str, head: str) -> str | None:
        code, output = await self._run(
            "reflog",
            "show",
            "--format=%H",
            f"--max-count={_REFLOG_LIMIT + 1}",
            base,
            "--",
            allowed=(0, 1, 128),
        )
        entries = output.splitlines()
        if code or not entries:
            return None
        if any(not _SHA.fullmatch(sha) for sha in entries):
            raise FactoryError("git_failed", error_message("git_failed"))
        # Ignore updates after our snapshot, never replace it with a second
        # branch lookup. A missing/truncated reflog is not evidence of a base.
        if target not in entries:
            return None
        entries = entries[entries.index(target) :]
        for newer, older in pairwise(entries):
            if newer == older:
                continue
            if await self._ancestor(head, newer) and not await self._ancestor(head, older):
                code, common = await self._run("merge-base", older, head, allowed=(0, 1))
                if code:
                    return None
                if not _SHA.fullmatch(common) or common == head:
                    raise FactoryError("git_failed", error_message("git_failed"))
                return common
        return None

    async def _changed_paths(self, base: str, head: str) -> set[str]:
        _, output = await self._run("diff", "--name-only", "--no-renames", "-z", base, head, "--")
        return self._paths(output)

    @staticmethod
    def _paths(output: str) -> set[str]:
        if not output:
            return set()
        parts = output.split("\x00")
        if parts[-1] or any(not path for path in parts[:-1]):
            raise FactoryError("git_failed", error_message("git_failed"))
        return set(parts[:-1])

    @staticmethod
    def _display_paths(paths: set[str]) -> list[str]:
        # Quote unsafe names reversibly for JSON/Tool output. Comparisons above
        # use lossless raw identities, not these display strings. Quoting literal
        # backslashes/quotes as well prevents collisions with escaped names.
        return [
            json.dumps(path, ensure_ascii=True)
            if any(not char.isprintable() or char in '\\"' for char in path)
            else path
            for path in sorted(paths)
        ]

    async def is_ancestor(self, head: str, base: str) -> bool:
        if not _SHA.fullmatch(head):
            raise FactoryError("git_failed", error_message("git_failed"))
        resolved = await self.base(base)
        return await self._ancestor(head, resolved)
