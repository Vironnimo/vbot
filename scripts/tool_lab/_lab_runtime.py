"""A throwaway vBot Runtime for inspecting and exercising production Tools.

The Runtime lives in a temporary data directory, so nothing touches a real
installation. Tools, Tool definitions and System Prompt blocks come from the
same bootstrap a server uses.
"""

from __future__ import annotations

import sys
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from core.runtime.runtime import Runtime
from core.utils.config import Config

DEFAULT_AGENT_ID = "main"


@asynccontextmanager
async def lab_runtime() -> AsyncIterator[tuple[Runtime, Path]]:
    """Start a Runtime on a fresh data directory; yield it with the scratch root."""
    with tempfile.TemporaryDirectory(prefix="vbot-tool-lab-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        # Startup chatter would bury the output; warnings and errors still show.
        env = root / "lab.env"
        env.write_text("LOG_LEVEL=WARNING\n", encoding="utf-8")
        runtime = Runtime(Config(data_dir=root / "data", env_path=env))
        runtime.start()
        try:
            yield runtime, root
        finally:
            await runtime.aclose()


def utf8_console() -> None:
    """Print Agent-facing text as it is, whatever the console's code page."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
