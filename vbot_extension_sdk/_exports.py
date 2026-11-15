"""Lazy access to the canonical host contracts, without loading another subsystem."""

from importlib import import_module
from typing import Any


class HostUnavailableError(ImportError):
    """A runtime contract was requested outside its installed vBot host."""


def resolve(namespace: str, exports: dict[str, str], name: str) -> Any:
    module = exports.get(name)
    if module is None:
        raise AttributeError(f"module {namespace!r} has no attribute {name!r}")
    try:
        return getattr(import_module(module), name)
    except ModuleNotFoundError as error:
        if error.name in {"core", "desktop"} or error.name == module:
            raise HostUnavailableError(
                f"{namespace}.{name} requires the matching vBot host. "
                "The standalone SDK provides authoring types; run Extensions inside vBot."
            ) from error
        raise


def exports_for(**modules: tuple[str, ...]) -> dict[str, str]:
    return {name: module for module, names in modules.items() for name in names}
