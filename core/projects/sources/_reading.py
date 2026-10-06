"""Strict, bounded reads shared by source adapters."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from core.utils.file_status import is_dir_strict, is_file_strict, is_link_status

MAX_SOURCE_BYTES = 128 * 1024


class SourceError(ValueError):
    """A source cannot safely supply runtime configuration."""


def read_text(path: Path) -> str:
    try:
        with path.open("rb") as stream:
            content = stream.read(MAX_SOURCE_BYTES + 1)
        if len(content) > MAX_SOURCE_BYTES:
            raise SourceError("Source exceeds 128 KiB; reduce its size.")
        return content.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    except (OSError, UnicodeError) as error:
        raise SourceError(f"Cannot read source: {error}") from error


def unique_mapping(pairs: list[tuple[Any, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if not isinstance(key, str) or key in result:
            raise SourceError("Metadata requires unique string keys.")
        result[key] = value
    return result


class _YamlLoader(yaml.SafeLoader):
    pass


def _yaml_mapping(loader: _YamlLoader, node: yaml.MappingNode) -> dict[str, Any]:
    return unique_mapping(loader.construct_pairs(node, deep=True))


_YamlLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _yaml_mapping)


def markdown(path: Path) -> tuple[dict[str, Any], str]:
    content = read_text(path)
    lines = content.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return {}, content
    closing = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if closing is None:
        raise SourceError("Unterminated YAML frontmatter.")
    try:
        fields = yaml.load("".join(lines[1:closing]), Loader=_YamlLoader)
    except yaml.YAMLError as error:
        raise SourceError(f"Invalid YAML metadata: {error}") from error
    if fields is None:
        fields = {}
    if not isinstance(fields, dict):
        raise SourceError("Frontmatter must be an object.")
    return fields, "".join(lines[closing + 1 :]).removeprefix("\n")


def json_object(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(read_text(path), object_pairs_hook=unique_mapping)
    except json.JSONDecodeError as error:
        raise SourceError(f"Invalid JSON metadata: {error}") from error
    if not isinstance(data, dict):
        raise SourceError("Configuration must be an object.")
    return data


def files(root: Path, suffix: str, *, recursive: bool = False) -> list[Path]:
    """Walk a known source folder, without entering links or hiding I/O failures."""
    if not is_dir_strict(root):
        return []
    if is_link_status(root.lstat()):
        raise SourceError("Linked source directories are not scanned.")
    found: list[Path] = []
    for entry in sorted(root.iterdir()):
        status = entry.lstat()
        if is_link_status(status):
            continue
        if is_file_strict(entry) and entry.name.endswith(suffix):
            found.append(entry)
        elif recursive and is_dir_strict(entry):
            found.extend(files(entry, suffix, recursive=True))
    return found


def string(fields: dict[str, Any], key: str, default: str = "") -> str:
    if key not in fields:
        return default
    value = fields[key]
    if not isinstance(value, str):
        raise SourceError(f"{key} must be a string.")
    return value.strip()


def boolean(fields: dict[str, Any], key: str, default: bool = False) -> bool:
    value = fields.get(key, default)
    if not isinstance(value, bool):
        raise SourceError(f"{key} must be a boolean.")
    return value


def names(value: Any, *, comma_separated: bool = False) -> list[str]:
    if isinstance(value, str) and comma_separated:
        # Agent(a,b) is one entry, not two Tool names.
        entries: list[str] = []
        depth = 0
        start = 0
        for index, character in enumerate(value):
            depth += (character == "(") - (character == ")")
            if depth < 0:
                raise SourceError("Unbalanced Tool specifier.")
            if character == "," and depth == 0:
                entries.append(value[start:index].strip())
                start = index + 1
        if depth:
            raise SourceError("Unbalanced Tool specifier.")
        entries.append(value[start:].strip())
        return [entry for entry in entries if entry]
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise SourceError("Expected a list of non-empty names.")
    return list(value)
