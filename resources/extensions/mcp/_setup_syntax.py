"""MCP setup syntax: documents and client command lines, without saved state."""

from __future__ import annotations

import json
import re
import tomllib
from typing import Any

_ENVIRONMENT_ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.DOTALL)

_LEADING_COMMENTS = re.compile(r"^(?:\s+|//[^\n]*|/\*.*?\*/)*", re.DOTALL)

_TOML_TABLE = re.compile(r"^\s*(?:\[\s*mcp_servers\b|mcp_servers\s*=)", re.MULTILINE)

_URL = re.compile(r"^https?://", re.IGNORECASE)

_PROMPT_PREFIX = re.compile(r"^\s*(?:\$|>|PS [^>\n]*>)\s+")

_LINE_CONTINUATION = re.compile(r"[ \t]*[\\^`][ \t]*\r?\n[ \t]*")

_IGNORED_OPTIONS_WITH_VALUES = frozenset(
    {"-s", "--scope", "--timeout", "--description", "--callback-port", "--client-id"}
)


def split_command_line(text: str) -> list[str]:
    """Split one shell command line into arguments, keeping Windows paths intact.

    Double and single quotes group words; a backslash escapes only a quote or
    whitespace, so ``C:\\Users\\me`` and ``\\\\server\\share`` stay as written.
    """
    tokens: list[str] = []
    current: list[str] = []
    quote: str | None = None
    started = False
    index = 0
    while index < len(text):
        character = text[index]
        following = text[index + 1] if index + 1 < len(text) else ""
        if quote is not None:
            if character == quote:
                quote = None
            elif character == "\\" and quote == '"' and following == '"':
                current.append('"')
                index += 1
            else:
                current.append(character)
        elif character in "\"'":
            quote = character
            started = True
        elif character == "\\" and following and (following in "\"'" or following.isspace()):
            current.append(following)
            started = True
            index += 1
        elif character.isspace():
            if started:
                tokens.append("".join(current))
                current, started = [], False
        else:
            current.append(character)
            started = True
        index += 1
    if quote is not None:
        raise ValueError("The command line has an unclosed quote")
    if started:
        tokens.append("".join(current))
    return tokens


def _document(text: str) -> Any:
    """*text* as a JSON (comments allowed) or TOML document, or ``None`` for other text."""
    toml_error = None
    if _TOML_TABLE.search(text):
        try:
            return tomllib.loads(text)
        except tomllib.TOMLDecodeError as error:
            toml_error = error
    if _LEADING_COMMENTS.sub("", text)[:1] in {"{", "[", '"'}:
        try:
            return json.loads(text)
        except json.JSONDecodeError as error:
            cleaned = _without_comments(text)
            # A fragment such as ``"name": {...},`` copied out of a larger file.
            for candidate in (cleaned, "{" + cleaned + "}"):
                try:
                    return json.loads(candidate)
                except ValueError:
                    continue
            if toml_error is None:
                raise ValueError(
                    f"The setup text is not valid JSON: {error.msg} "
                    f"(line {error.lineno}, column {error.colno})"
                ) from None
    if toml_error is not None:
        raise ValueError(f"The setup text is not valid TOML: {toml_error}")
    return None


def _without_comments(text: str) -> str:
    """JSON with comments (JSONC) as JSON: comments and trailing commas removed."""
    output: list[str] = []
    index = 0
    in_string = False
    while index < len(text):
        character = text[index]
        if in_string:
            output.append(character)
            if character == "\\" and index + 1 < len(text):
                output.append(text[index + 1])
                index += 1
            elif character == '"':
                in_string = False
        elif character == '"':
            in_string = True
            output.append(character)
        elif text.startswith("//", index):
            end = text.find("\n", index)
            index = len(text) if end < 0 else end
            continue
        elif text.startswith("/*", index):
            end = text.find("*/", index + 2)
            index = len(text) if end < 0 else end + 2
            continue
        elif character == "," and _closes(text, index + 1):
            pass
        else:
            output.append(character)
        index += 1
    return "".join(output).strip()


def _closes(text: str, index: int) -> bool:
    """Whether only whitespace and comments precede a closing bracket or the end."""
    rest = _LEADING_COMMENTS.sub("", text[index:])
    return not rest or rest[0] in "}]"


def _looks_like_server(value: Any) -> bool:
    return isinstance(value, dict) and bool(
        {"command", "url", "serverUrl", "httpUrl", "type", "transport"} & set(value)
    )


def _configuration_entries(
    document: Any,
) -> tuple[list[tuple[str | None, dict[str, Any]]], dict[str, dict[str, Any]]]:
    """The named server entries of a configuration document and its VS Code inputs."""
    if isinstance(document, list):
        servers = [item for item in document if _looks_like_server(item)]
        return [(_text(item.get("name")) or None, item) for item in servers], {}
    if not isinstance(document, dict):
        raise ValueError("The setup text is not an MCP server configuration")
    if isinstance(document.get("mcp"), dict) and "servers" in document["mcp"]:
        # VS Code user settings nest the servers under "mcp".
        return _configuration_entries(document["mcp"])
    inputs = {
        item["id"]: item
        for item in document.get("inputs", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    for key in ("mcpServers", "servers", "context_servers", "mcp_servers"):
        if key in document:
            servers = document[key]
            if not isinstance(servers, dict):
                raise ValueError(f"{key} must map server names to their settings")
            entries: list[tuple[str | None, dict[str, Any]]] = []
            for name, server in servers.items():
                if not isinstance(server, dict):
                    raise ValueError(f"Server {name} must be an object")
                entries.append((name, server))
            return entries, inputs
    if _looks_like_server(document):
        return [(_text(document.get("name")) or None, document)], inputs
    if document and all(_looks_like_server(value) for value in document.values()):
        return list(document.items()), inputs
    raise ValueError(
        "No MCP servers found: expected mcpServers, servers, context_servers or mcp_servers"
    )


def _command_entries(text: str) -> list[tuple[str | None, dict[str, Any]]]:
    """Each line of *text*, continuations joined, as a command line or server URL."""
    entries = []
    for line in _LINE_CONTINUATION.sub(" ", text).splitlines():
        line = _PROMPT_PREFIX.sub("", line).strip()
        if line and not line.startswith("#"):
            entries.append(_command_entry(split_command_line(line)))
    return entries


def _command_entry(tokens: list[str]) -> tuple[str | None, dict[str, Any]]:
    if not tokens:
        raise ValueError("The command line is empty")
    program = _program_name(tokens[0])
    if program in {"claude", "gemini"} and tokens[1:3] == ["mcp", "add"]:
        return _client_add(tokens[3:])
    if program == "claude" and tokens[1:3] == ["mcp", "add-json"] and len(tokens) >= 5:
        return tokens[3], _json_server(tokens[4])
    if program in {"code", "code-insiders"} and len(tokens) >= 3 and tokens[1] == "--add-mcp":
        server = _json_server(tokens[2])
        return _text(server.get("name")) or None, server
    if len(tokens) == 1 and re.match(r"^[a-z][a-z0-9+.-]*://", tokens[0], re.IGNORECASE):
        return None, {"url": tokens[0]}
    environment = {}
    if program == "env":
        tokens = tokens[1:]
    while tokens and (assignment := _ENVIRONMENT_ASSIGNMENT.match(tokens[0])):
        environment[assignment.group(1)] = assignment.group(2)
        tokens = tokens[1:]
    if not tokens:
        raise ValueError("The command line names no program")
    return None, {"command": tokens[0], "args": tokens[1:], "env": environment}


def _client_add(tokens: list[str]) -> tuple[str | None, dict[str, Any]]:
    """``claude mcp add`` / ``gemini mcp add`` options and arguments as a server entry."""
    server: dict[str, Any] = {"env": {}, "headers": {}}
    positional: list[str] = []
    ignored: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        following = tokens[index + 1] if index + 1 < len(tokens) else None
        if token == "--":
            positional.extend(tokens[index + 1 :])
            break
        if len(positional) >= 2:
            # Everything after the command or URL belongs to the server.
            positional.append(token)
        elif token in {"-t", "--transport"} and following is not None:
            server["type"] = following
            index += 1
        elif token in {"-e", "--env"}:
            while index + 1 < len(tokens) and _ENVIRONMENT_ASSIGNMENT.match(tokens[index + 1]):
                key, value = tokens[index + 1].split("=", 1)
                server["env"][key] = value
                index += 1
        elif token in {"-H", "--header"}:
            while index + 1 < len(tokens) and re.match(r"^[\w-]+\s*:", tokens[index + 1]):
                key, value = tokens[index + 1].split(":", 1)
                server["headers"][key.strip()] = value.strip()
                index += 1
        elif token.startswith("-"):
            ignored.append(token.split("=", 1)[0])
            if token in _IGNORED_OPTIONS_WITH_VALUES and following is not None:
                index += 1
        else:
            positional.append(token)
        index += 1
    if len(positional) < 2:
        raise ValueError("mcp add needs a server name and a command or URL")
    name, target, *arguments = positional
    if _URL.match(target):
        server["url"] = target
    else:
        server["command"] = target
        server["args"] = arguments
    if ignored:
        server["_ignored_options"] = ignored
    return name, server


def _json_server(text: str) -> dict[str, Any]:
    try:
        server = json.loads(text)
    except ValueError:
        raise ValueError("The server definition in the command is not valid JSON") from None
    if not _looks_like_server(server):
        raise ValueError("The server definition in the command names no command or URL")
    return dict(server)


def _program_name(command: str) -> str:
    name = re.split(r"[\\/]", command)[-1].lower()
    return re.sub(r"\.(?:exe|cmd|bat|ps1)$", "", name)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""
