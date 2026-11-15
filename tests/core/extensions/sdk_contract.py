"""Compare the deliberately public SDK projection with its canonical owners.

Stubtest checks callable kinds, arguments, defaults and runtime members. This
complements it with annotations, PEP 695 aliases and subclass obligations. Any
in the public declarations deliberately represents an opaque host capability.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import inspect
import sys
import types
import typing
from collections import ChainMap
from pathlib import Path
from typing import Any

MODULES = ("", ".providers", ".channels", ".desktop", ".tools", ".commands")
HOST_CONSTRUCTORS = {
    "vbot_extension_sdk.ExtensionOperations.__init__": "supplied as api.operations",
    "vbot_extension_sdk.commands.ExtensionCommandContext.__init__": "supplied to command handlers",
}


class _Namespace(dict[str, Any]):
    """Resolve an owner's TYPE_CHECKING imports only when an annotation uses one."""

    def __init__(self, module: types.ModuleType) -> None:
        super().__init__(vars(module))
        self.imports: dict[str, tuple[str, str | None]] = {}
        source = inspect.getsource(module)

        def visit(nodes: list[ast.stmt]) -> None:
            for node in nodes:
                if isinstance(node, ast.ImportFrom) and node.module:
                    package = importlib.util.resolve_name(
                        "." * node.level + node.module, module.__package__ or ""
                    )
                    for alias in node.names:
                        self.imports[alias.asname or alias.name] = (package, alias.name)
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        self.imports[alias.asname or alias.name.split(".")[0]] = (
                            alias.name if alias.asname else alias.name.split(".")[0],
                            None,
                        )
                elif isinstance(node, (ast.If, ast.Try)):
                    visit(node.body)
                    visit(node.orelse)

        visit(ast.parse(source).body)

    def __missing__(self, name: str) -> Any:
        if name not in self.imports:
            raise KeyError(name)
        module, attribute = self.imports[name]
        value = importlib.import_module(module)
        result = getattr(value, attribute) if attribute else value
        self[name] = result
        return result


def _annotation(value: Any, namespace: dict[str, Any], owner: Any) -> Any:
    local = dict(vars(owner)) if inspect.isclass(owner) else {}
    local.update({p.__name__: p for p in getattr(owner, "__type_params__", ())})
    while isinstance(value, str):
        value = eval(value, namespace, ChainMap(local, namespace))
    return value


def _normal(
    value: Any, identities: dict[type, type], namespace: dict[str, Any] | None = None
) -> Any:
    if isinstance(value, typing.TypeAliasType):
        assert value.__module__ is not None
        module = sys.modules[value.__module__]
        alias_namespace = (
            vars(module) if module.__name__.startswith("_sdk_contract") else _Namespace(module)
        )
        return _normal(value.__value__, identities, alias_namespace)
    if isinstance(value, (str, typing.ForwardRef)) and namespace is not None:
        text = value.__forward_arg__ if isinstance(value, typing.ForwardRef) else value
        try:
            resolved = _annotation(text, namespace, None)
        except NameError:
            # A quoted type inside an old-style imported alias can lack its
            # defining namespace. It can satisfy only an explicit opaque Any.
            return ("unresolved", text)
        return _normal(resolved, identities, namespace)
    if isinstance(value, typing.TypeVar):
        return (
            "parameter",
            value.__name__,
            _normal(value.__bound__, identities, namespace),
            _normal(value.__constraints__, identities, namespace),
            _normal(value.__default__, identities, namespace),
            value.__covariant__,
            value.__contravariant__,
            value.__infer_variance__,
        )
    if value is None:
        return type(None)
    if isinstance(value, (list, tuple)):
        return tuple(_normal(item, identities, namespace) for item in value)
    origin = typing.get_origin(value)
    if origin:
        args = (
            tuple((type(item), item) for item in typing.get_args(value))
            if origin is typing.Literal
            else tuple(_normal(item, identities, namespace) for item in typing.get_args(value))
        )
        if origin in (typing.Union, types.UnionType):
            return (typing.Union, frozenset(args))
        return (origin, args)
    return identities.get(value, value) if inspect.isclass(value) else value


def _compatible(expected: Any, actual: Any) -> bool:
    if expected is Any:
        return True
    if isinstance(expected, tuple) and isinstance(actual, tuple):
        return len(expected) == len(actual) and all(
            _compatible(left, right) for left, right in zip(expected, actual, strict=True)
        )
    if isinstance(expected, frozenset) and isinstance(actual, frozenset):
        if len(expected) != len(actual):
            return False
        if not actual:
            return True
        # A wildcard must consume exactly one branch; Any | None cannot hide
        # a runtime union that lost its None branch or added a required type.
        right = next(iter(actual))
        return any(
            _compatible(left, right) and _compatible(expected - {left}, actual - {right})
            for left in expected
        )
    return bool(expected == actual)


def _function(owner: Any, name: str) -> Any:
    value = inspect.getattr_static(owner, name)
    if isinstance(value, (staticmethod, classmethod)):
        return value.__func__
    return value.fget if isinstance(value, property) else value


def check_annotations(root: Path) -> tuple[list[str], set[str]]:
    """Return mismatches and the aliases fully checked outside stubtest."""
    errors: list[str] = []
    aliases: set[str] = set()
    loaded: list[tuple[types.ModuleType, types.ModuleType, ast.Module]] = []
    identities: dict[type, type] = {}
    namespaces: dict[str, _Namespace] = {}
    for suffix in MODULES:
        public = importlib.import_module(f"vbot_extension_sdk{suffix}")
        path = root / "vbot_extension_sdk" / f"{suffix[1:] or '__init__'}.pyi"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        offered: set[str] = set()
        for declaration in tree.body:
            if isinstance(declaration, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                offered.add(declaration.name)
            elif isinstance(declaration, ast.TypeAlias):
                offered.add(declaration.name.id)
            elif isinstance(declaration, ast.AnnAssign) and isinstance(
                declaration.target, ast.Name
            ):
                offered.add(declaration.target.id)
            elif isinstance(declaration, ast.ImportFrom):
                offered.update(
                    alias.name for alias in declaration.names if alias.asname == alias.name
                )
        if offered != set(public.__all__):
            errors.append(
                f"{public.__name__}: export inventory differs: {offered ^ set(public.__all__)}"
            )
        if not set(public._EXPORTS) <= offered:
            errors.append(f"{public.__name__}: runtime export has no public declaration")
        declared = types.ModuleType(f"_sdk_contract{suffix}")
        declared.__package__ = "vbot_extension_sdk"
        sys.modules[declared.__name__] = declared
        exec(compile(tree, str(path), "exec"), vars(declared))
        loaded.append((public, declared, tree))
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                identities[getattr(declared, node.name)] = getattr(public, node.name)

    def compare(label: str, expected: Any, actual: Any, expected_owner: Any, owner: Any) -> None:
        try:
            expected_module = sys.modules[expected_owner.__module__]
            actual_module = sys.modules[owner.__module__]
            if actual_module.__name__ not in namespaces:
                namespaces[actual_module.__name__] = _Namespace(actual_module)
            namespace = namespaces[actual_module.__name__]
            left = _normal(
                _annotation(expected, vars(expected_module), expected_owner),
                identities,
                vars(expected_module),
            )
            right = _normal(_annotation(actual, namespace, owner), identities, namespace)
            if not _compatible(left, right):
                errors.append(f"{label}: annotation {left!r} differs from {right!r}")
        except (NameError, AttributeError, TypeError, KeyError) as error:
            errors.append(f"{label}: cannot resolve annotation: {error}")

    for public, declared, tree in loaded:
        for node in tree.body:
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                expected_value = getattr(declared, node.target.id)
                actual_value = getattr(public, node.target.id)
                if expected_value is not Ellipsis and (
                    type(expected_value) is not type(actual_value) or expected_value != actual_value
                ):
                    errors.append(f"{public.__name__}.{node.target.id}: constant value differs")
                continue
            name = (
                node.name
                if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                else None
            )
            if isinstance(node, ast.TypeAlias):
                name = node.name.id
            elif (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
            ):
                name = node.targets[0].id
            if not name or name.startswith("_"):
                continue
            expected, actual = getattr(declared, name), getattr(public, name)
            label = f"{public.__name__}.{name}"
            if isinstance(node, (ast.TypeAlias, ast.Assign)):
                aliases.add(label)
                owner = importlib.import_module(public._EXPORTS[name].partition(":")[0])
                if not _compatible(
                    _normal(expected, identities, vars(declared)),
                    _normal(actual, identities, _Namespace(owner)),
                ):
                    errors.append(f"{label}: type alias differs")
                continue
            if isinstance(node, ast.ClassDef):
                if getattr(expected, "__abstractmethods__", frozenset()) != getattr(
                    actual, "__abstractmethods__", frozenset()
                ):
                    errors.append(f"{label}: abstract subclass obligations differ")
                if typing.is_protocol(expected) and (
                    not typing.is_protocol(actual)
                    or typing.get_protocol_members(expected) != typing.get_protocol_members(actual)
                ):
                    errors.append(f"{label}: protocol implementation obligations differ")
                members = node.body
            else:
                members = [node]
            for member in members:
                if isinstance(member, ast.AnnAssign) and isinstance(member.target, ast.Name):
                    field = member.target.id
                    expected_hint = expected.__annotations__[field]
                    actual_hints = inspect.get_annotations(actual, eval_str=False)
                    if field in actual_hints:
                        compare(
                            f"{label}.{field}", expected_hint, actual_hints[field], expected, actual
                        )
                    else:
                        errors.append(f"{label}.{field}: runtime attribute annotation missing")
                elif isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    location = f"{label}.{member.name}" if isinstance(node, ast.ClassDef) else label
                    if location in HOST_CONSTRUCTORS:
                        continue
                    left = (
                        _function(expected, member.name)
                        if isinstance(node, ast.ClassDef)
                        else expected
                    )
                    if isinstance(node, ast.ClassDef) and isinstance(
                        inspect.getattr_static(expected, member.name), property
                    ):
                        actual_hints = inspect.get_annotations(actual, eval_str=False)
                        if member.name in actual_hints:
                            compare(
                                location,
                                inspect.get_annotations(left)["return"],
                                actual_hints[member.name],
                                left,
                                actual,
                            )
                            continue
                    right = (
                        _function(actual, member.name) if isinstance(node, ast.ClassDef) else actual
                    )
                    if inspect.iscoroutinefunction(left) != inspect.iscoroutinefunction(right):
                        errors.append(f"{location}: async declaration differs")
                    left_hints, right_hints = (
                        inspect.get_annotations(left),
                        inspect.get_annotations(right),
                    )
                    for key, hint in left_hints.items():
                        if key not in right_hints:
                            errors.append(f"{location}.{key}: runtime annotation missing")
                        else:
                            compare(f"{location}.{key}", hint, right_hints[key], left, right)
    return errors, aliases
