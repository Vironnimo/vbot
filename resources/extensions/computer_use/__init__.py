"""Computer Use's Extension entry point, loaded only by the server.

The Desktop also imports this package's native target; that path needs no kernel.
"""

from typing import Any


def __getattr__(name: str) -> Any:
    if name == "register":
        from .extension import register

        globals()[name] = register
        return register
    raise AttributeError(name)


__all__ = ["register"]
