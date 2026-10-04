"""A throwaway vBot Runtime for inspecting and exercising production Tools.

The Runtime lives in a temporary data directory, and commands its Tools run
see that directory as their vBot instance, so nothing touches a real
installation. Tools, Tool definitions and System Prompt blocks come from the
same bootstrap a server uses.
"""

from __future__ import annotations

import os
import sys
import tempfile
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

from core.runtime.runtime import Runtime
from core.utils.config import Config

DEFAULT_AGENT_ID = "main"


@asynccontextmanager
async def lab_runtime(*, extensions: bool = True) -> AsyncIterator[tuple[Runtime, Path]]:
    """Start a Runtime on a fresh data directory; yield it with the scratch root.

    ``extensions=False`` starts it without extensions, their Tools and the
    background services (the Runtime's safe verification mode), which is
    enough for the built-in file Tools and starts much faster.
    """
    with tempfile.TemporaryDirectory(prefix="vbot-tool-lab-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        # Startup chatter would bury the output; warnings and errors still show.
        env = root / "lab.env"
        env.write_text("LOG_LEVEL=WARNING\n", encoding="utf-8")
        with _lab_instance(root / "data"):
            runtime = Runtime(
                Config(data_dir=root / "data", env_path=env),
                safe_startup_mode=None if extensions else "verification",
            )
            runtime.start()
            try:
                yield runtime, root
            finally:
                await runtime.aclose()


@contextmanager
def _lab_instance(data_dir: Path) -> Iterator[None]:
    """Point the instance variables commands inherit at the lab, not at a real vBot.

    A lab started from inside vBot inherits that instance's data directory and
    server port; a command run by a Tool under test must not reach either.
    """
    saved = {name: os.environ.get(name) for name in ("VBOT_DATA_DIR", "VBOT_SERVER_PORT")}
    os.environ["VBOT_DATA_DIR"] = str(data_dir)
    os.environ.pop("VBOT_SERVER_PORT", None)
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def utf8_console() -> None:
    """Print Agent-facing text as it is, whatever the console's code page."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
