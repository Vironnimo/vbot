"""Lazy access to the canonical host contracts, without loading another subsystem."""

from importlib import import_module
from typing import Any


class HostUnavailableError(ImportError):
    """A runtime contract was requested outside its installed vBot host."""


def resolve(namespace: str, exports: dict[str, str], name: str) -> Any:
    target = exports.get(name)
    if target is None:
        raise AttributeError(f"module {namespace!r} has no attribute {name!r}")
    module, _, attribute = target.partition(":")
    try:
        return getattr(import_module(module), attribute or name)
    except ModuleNotFoundError as error:
        if error.name in {"core", "desktop"} or error.name == module:
            raise HostUnavailableError(
                f"{namespace}.{name} requires the matching vBot host. "
                "The standalone SDK provides authoring types; run Extensions inside vBot."
            ) from error
        raise


def exports_for(**modules: tuple[str, ...]) -> dict[str, str]:
    return {name: module for module, names in modules.items() for name in names}
