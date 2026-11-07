"""Bounded read-only Git facts; worktree creation and integration stay with Agents."""

from __future__ import annotations

import asyncio
import os
import re
from contextlib import suppress
from pathlib import Path

from .agent_text import error_message
from .store import FactoryError

_TIMEOUT = 10.0
_OUTPUT_LIMIT = 64 * 1024
_SHA = re.compile(r"[0-9a-f]{40,64}\Z")


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
                return code, output.decode("utf-8", errors="replace").strip()
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

    async def submission(self, branch: str, base: str) -> dict[str, str]:
        head = await self.head(branch)
        assert head is not None
        base_head = await self.base(base)
        _, count = await self._run("rev-list", "--count", f"{base_head}..{head}")
        if not count.isdecimal() or int(count) == 0:
            raise FactoryError("no_commits", error_message("no_commits"))
        code, merge_base = await self._run("merge-base", base_head, head, allowed=(0, 1))
        if code or not _SHA.fullmatch(merge_base):
            raise FactoryError("unrelated_history", error_message("unrelated_history"))
        return {"branch": branch, "base": base, "base_sha": merge_base, "head_sha": head}

    async def is_ancestor(self, head: str, base: str) -> bool:
        if not _SHA.fullmatch(head):
            raise FactoryError("git_failed", error_message("git_failed"))
        resolved = await self.base(base)
        code, _ = await self._run("merge-base", "--is-ancestor", head, resolved, allowed=(0, 1))
        return code == 0
