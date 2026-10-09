"""Strict, bounded reads shared by source adapters."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from core.utils.file_status import is_dir_strict, is_file_strict, is_link_status, stat_or_none

MAX_SOURCE_BYTES = 128 * 1024


class SourceError(ValueError):
    """A source cannot safely supply runtime configuration."""


def is_source_file(folder: Path, path: Path) -> bool:
    """Check a previously discovered file without walking its siblings or following links."""
    if path == folder or not path.is_relative_to(folder):
        return False
    for entry in (path, *path.parents):
        status = stat_or_none(entry, follow_symlinks=False)
        if status is None or is_link_status(status):
            return False
        if entry == folder:
            return is_file_strict(path)
    return False


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


def has_frontmatter(path: Path) -> bool:
    return read_text(path).lstrip("\ufeff").startswith("---")


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
    text = read_text(path)
    if path.suffix == ".jsonc":
        text = _strip_jsonc(text)
    try:
        data = json.loads(text, object_pairs_hook=unique_mapping)
    except json.JSONDecodeError as error:
        raise SourceError(f"Invalid JSON metadata: {error}") from error
    if not isinstance(data, dict):
        raise SourceError("Configuration must be an object.")
    return data


def _strip_jsonc(text: str) -> str:
    """Remove JSONC comments and trailing commas outside of strings."""
    result: list[str] = []
    index = 0
    in_string = False
    while index < len(text):
        character = text[index]
        if in_string:
            result.append(character)
            if character == "\\":
                result.append(text[index + 1 : index + 2])
                index += 1
            elif character == '"':
                in_string = False
        elif character == '"':
            in_string = True
            result.append(character)
        elif text.startswith("//", index):
            end = text.find("\n", index)
            index = len(text) if end < 0 else end
            continue
        elif text.startswith("/*", index):
            end = text.find("*/", index + 2)
            if end < 0:
                raise SourceError("Unterminated comment in JSONC metadata.")
            index = end + 2
            continue
        elif character in "]}":
            # A trailing comma before a closing bracket is valid JSONC.
            position = len(result) - 1
            while position >= 0 and result[position].isspace():
                position -= 1
            if position >= 0 and result[position] == ",":
                del result[position]
            result.append(character)
        else:
            result.append(character)
        index += 1
    return "".join(result)


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
