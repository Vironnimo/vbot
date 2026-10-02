"""Connection drafts from the setup text that MCP servers publish for other clients.

Server instructions show a ``mcpServers`` JSON block (Claude Desktop, Cursor,
Claude Code, Windsurf, Gemini CLI, Cline), VS Code's ``servers`` with
``inputs``, Zed's ``context_servers``, Codex's TOML ``mcp_servers``, a
``claude mcp add`` command, a plain command line or just the server URL.
``parse_setup`` turns any of them into connection drafts without looking at
saved connections or credentials. A secret in the text never enters a
connection: it becomes a credential reference, and the draft carries the value
apart so the caller can store it with the host's credentials.
"""

from __future__ import annotations

import json
import os
import re
import tomllib
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from .config import (
    CONNECTION_ID_PATTERN,
    ENVIRONMENT_KEY_PATTERN,
    MAX_DESCRIPTION_CHARACTERS,
    MAX_TIMEOUT_SECONDS,
    validate_connection,
)

# The longest setup text accepted, far above any real configuration file.
MAX_SETUP_CHARACTERS = 200_000
_MAX_ID_CHARACTERS = 32
# Fields of a server entry that drafts take over; all others are reported.
_SERVER_FIELDS = frozenset(
    {
        "name",
        "type",
        "transport",
        "command",
        "args",
        "env",
        "cwd",
        "url",
        "serverUrl",
        "httpUrl",
        "headers",
        "http_headers",
        "env_http_headers",
        "bearer_token_env_var",
        "disabled",
        "enabled",
        "description",
        "tool_timeout_sec",
    }
)
_TRANSPORTS = {
    "stdio": "stdio",
    "local": "stdio",
    "http": "http",
    "streamable-http": "http",
    "streamablehttp": "http",
    "streamable_http": "http",
    "sse": "sse",
}
# Words of a variable or header name that mark its value as a secret.
_SECRET_WORDS = frozenset(
    {
        "TOKEN",
        "TOKENS",
        "SECRET",
        "SECRETS",
        "PASSWORD",
        "PASSWD",
        "PASS",
        "PASSPHRASE",
        "APIKEY",
        "KEY",
        "AUTH",
        "AUTHORIZATION",
        "CREDENTIAL",
        "CREDENTIALS",
        "COOKIE",
        "PAT",
        "PRIVATE",
        "SESSION",
        "SIGNATURE",
    }
)
# A last word that names something about a secret rather than the secret itself.
_SECRET_DESCRIPTORS = frozenset(
    {
        "PATH",
        "FILE",
        "DIR",
        "URL",
        "URI",
        "HOST",
        "ID",
        "NAME",
        "TYPE",
        "MODE",
        "REGION",
        "ENDPOINT",
        "PORT",
        "HEADER",
        "ENABLED",
        "EXPIRY",
    }
)
_SECRET_VALUE = re.compile(
    r"""^(?:
        sk-[A-Za-z0-9_-]{16,}
      | (?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{16,}
      | gh[pousr]_[A-Za-z0-9]{20,}
      | github_pat_\w{20,}
      | glpat-[\w-]{20,}
      | xox[abposr]-[\w-]{10,}
      | AKIA[0-9A-Z]{16}
      | AIza[\w-]{30,}
      | ya29\.[\w-]{20,}
      | eyJ[\w-]{8,}\.eyJ[\w-]{8,}\.[\w-]{8,}
      | (?:Bearer|Basic|Token)\s+\S{8,}
    )$""",
    re.VERBOSE,
)
# Text that stands in for a value the reader must supply.
_PLACEHOLDER_VALUE = re.compile(
    r"""^(?:
        <[^<>]+>
      | \[[^\[\]]+\]
      | \{[^{}$]+\}
      | (?:your|my)[\s_-]?\S*
      | \S*[_-](?:here|goes[_-]?here)
      | x{3,} | \*{3,} | \.{3} | …
      | replace[_-]?me | change[_-]?me | todo | tbd | fixme | placeholder
    )$""",
    re.VERBOSE | re.IGNORECASE,
)
_SCHEME_PREFIX = re.compile(r"^(?:Bearer|Basic|Token)\s+")
_ARGUMENT_SECRET_FLAG = re.compile(
    r"^--?(?:api[-_]?key|access[-_]?key|key|token|access[-_]?token|auth[-_]?token|"
    r"password|passwd|secret|client[-_]?secret|bearer)$",
    re.IGNORECASE,
)
_URL_SECRET_PARAMETERS = frozenset(
    {
        "key",
        "apikey",
        "api_key",
        "token",
        "access_token",
        "auth",
        "auth_token",
        "secret",
        "password",
        "sig",
        "signature",
    }
)
# ``${env:NAME}``, ``${input:id}``, ``${NAME}``, ``${NAME:-default}`` and VS Code variables.
_REFERENCE = re.compile(r"\$\{(?:(env|input):)?([^}:]+)(?::-([^}]*))?\}")
_EDITOR_VARIABLES = frozenset(
    {
        "workspaceFolder",
        "workspaceFolderBasename",
        "workspaceRoot",
        "file",
        "fileDirname",
        "relativeFile",
        "cwd",
        "userHome",
        "pathSeparator",
        "/",
    }
)
_ENVIRONMENT_ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.DOTALL)
_LEADING_COMMENTS = re.compile(r"^(?:\s+|//[^\n]*|/\*.*?\*/)*", re.DOTALL)
_TOML_TABLE = re.compile(r"^\s*\[\s*mcp_servers\b", re.MULTILINE)
_URL = re.compile(r"^https?://", re.IGNORECASE)
_PROMPT_PREFIX = re.compile(r"^\s*(?:\$|>|PS [^>\n]*>)\s+")
_LINE_CONTINUATION = re.compile(r"[ \t]*[\\^`][ \t]*\r?\n[ \t]*")
_SCRIPT = re.compile(r"(.+)\.(?:py|js|mjs|cjs|ts|mts)", re.IGNORECASE)
# Launchers whose first plain argument names the server's package, image or script.
_LAUNCHERS = frozenset(
    {
        "npx",
        "pnpx",
        "bunx",
        "uvx",
        "pipx",
        "npm",
        "pnpm",
        "yarn",
        "bun",
        "uv",
        "docker",
        "podman",
        "deno",
        "node",
        "python",
        "python3",
        "py",
    }
)
_LAUNCHER_SUBCOMMANDS = frozenset({"run", "exec", "dlx", "x", "tool"})
_FLAGS_WITH_VALUES = frozenset(
    {
        "-p",
        "--package",
        "--from",
        "--with",
        "--python",
        "--directory",
        "--index-url",
        "-e",
        "--env",
        "-v",
        "--volume",
        "--name",
        "--network",
        "--net",
        "-w",
        "--workdir",
        "--env-file",
        "--mount",
        "-u",
        "--user",
        "--entrypoint",
        "--platform",
    }
)
_GENERIC_FILE_NAMES = frozenset(
    {"index", "main", "server", "cli", "app", "__main__", "run", "start"}
)
_BUILD_DIRECTORIES = frozenset({"dist", "build", "src", "lib", "bin", "out", "target", "release"})
_GENERIC_HOST_LABELS = frozenset({"www", "mcp", "api", "server", "servers", "app", "gateway"})
_SECOND_LEVEL_SUFFIXES = frozenset({"co", "com", "net", "org", "gov", "edu", "ac"})
_NAME_PREFIXES = ("mcp-server-", "mcp_server_", "server-", "mcp-")
_NAME_SUFFIXES = ("-mcp-server", "_mcp_server", "-server-mcp", "-mcp", "_mcp", "-server")
# Options of ``claude mcp add`` and ``gemini mcp add`` that take a value vBot ignores.
_IGNORED_OPTIONS_WITH_VALUES = frozenset(
    {"-s", "--scope", "--timeout", "--description", "--callback-port", "--client-id"}
)


@dataclass
class ImportedCredential:
    """A credential an imported connection references.

    ``state`` is ``provided`` when the setup text holds the value (``value``,
    stored on import), ``reference`` when it names an environment variable the
    vBot host may already have, and ``missing`` when the user still has to
    supply it.
    """

    name: str
    kind: str
    target: str
    state: str
    description: str = ""
    value: str | None = field(default=None, repr=False)


@dataclass
class ImportDraft:
    """One server of the setup text as a connection that can be saved."""

    name: str = ""
    id: str = ""
    connection: dict[str, Any] = field(default_factory=dict)
    credentials: list[ImportedCredential] = field(default_factory=list)
    warnings: list[dict[str, str]] = field(default_factory=list)
    error: str | None = None
    # Placeholders were left in the connection; it cannot work as saved.
    unresolved: bool = False
    # A secret could not be moved out of plain configuration.
    exposes_secret: bool = False
    # The setup text turned the server off.
    disabled: bool = False
    # The server's fields under common names, and the VS Code input prompts.
    server: dict[str, Any] = field(default_factory=dict, repr=False)
    inputs: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)

    def warn(self, code: str, message: str) -> None:
        self.warnings.append({"code": code, "message": message})


def parse_setup(text: str, ids: Mapping[str, str] | None = None) -> list[ImportDraft]:
    """The servers of *text* as drafts; *ids* maps a server name to its connection id."""
    if len(text) > MAX_SETUP_CHARACTERS:
        raise ValueError(f"MCP setup text is longer than {MAX_SETUP_CHARACTERS} characters")
    text = text.lstrip("\ufeff").strip()
    if not text:
        raise ValueError("Paste an MCP server configuration, a command line or a server URL")
    document = _document(text)
    if document is not None:
        entries, inputs = _configuration_entries(document)
    else:
        entries, inputs = _command_entries(text), {}
    if not entries:
        raise ValueError("No MCP server was found in the setup text")
    drafts = []
    names: set[str] = set()
    for name, raw in entries:
        draft = _draft(raw, inputs)
        draft.name = _unique(name or _derived_name(draft.server), names, separator="-")
        names.add(draft.name)
        drafts.append(draft)
    _assign_ids(drafts, ids or {})
    return drafts


def connection_id(name: str) -> str:
    """*name* as a valid connection id: lowercase ASCII letters, digits and underscores."""
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    text = re.sub(r"^[^a-z]+", "", text)
    return text[:_MAX_ID_CHARACTERS].rstrip("_") or "server"


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


def _draft(raw: dict[str, Any], inputs: dict[str, dict[str, Any]]) -> ImportDraft:
    server = _normalized(raw)
    draft = ImportDraft(server=server, inputs=inputs, disabled=server["disabled"])
    ignored = sorted(set(raw) - _SERVER_FIELDS - {"_ignored_options"})
    ignored += raw.get("_ignored_options", [])
    if "envFile" in ignored:
        draft.warn(
            "env_file_ignored",
            "envFile is not read; add its variables as environment values or credentials.",
        )
        ignored.remove("envFile")
    if ignored:
        draft.warn("ignored_fields", f"Not imported: {', '.join(ignored)}.")
    try:
        transport = _transport(server)
    except ValueError as error:
        draft.error = str(error)
        return draft
    connection: dict[str, Any] = {"transport": transport}
    description = " ".join(_text(server["description"]).split())
    if description:
        connection["description"] = description[:MAX_DESCRIPTION_CHARACTERS]
    if server["timeout"] is not None:
        connection["timeout"] = server["timeout"]
    draft.connection = connection
    return draft


def _normalized(raw: dict[str, Any]) -> dict[str, Any]:
    """The fields of one client's server entry under common names."""
    command = raw.get("command")
    args = raw.get("args", [])
    env = dict(raw.get("env") or {})
    if isinstance(command, dict):
        # Zed's earlier form: {"command": {"path", "args", "env"}}.
        args = command.get("args", args)
        env.update(command.get("env") or {})
        command = command.get("path")
    headers = dict(raw.get("headers") or raw.get("http_headers") or {})
    if raw.get("bearer_token_env_var"):
        headers["Authorization"] = "Bearer ${env:" + str(raw["bearer_token_env_var"]) + "}"
    references = {
        str(header): "${env:" + str(variable) + "}"
        for header, variable in (raw.get("env_http_headers") or {}).items()
    }
    url = raw.get("httpUrl") or raw.get("url") or raw.get("serverUrl")
    kind = raw.get("type") or raw.get("transport") or ("http" if raw.get("httpUrl") else None)
    timeout = raw.get("tool_timeout_sec")
    return {
        "type": _text(kind).lower().replace(" ", "") or None,
        "command": _text(command) or None,
        "args": [_scalar(item) for item in args] if isinstance(args, list) else [],
        "env": {str(key): _scalar(value) for key, value in env.items()},
        "cwd": _text(raw.get("cwd")) or None,
        "url": _text(url) or None,
        "headers": {
            **{str(key): _scalar(value) for key, value in headers.items()},
            **references,
        },
        "disabled": raw.get("disabled") is True or raw.get("enabled") is False,
        "description": raw.get("description"),
        "timeout": timeout
        if isinstance(timeout, int | float)
        and not isinstance(timeout, bool)
        and 0 < timeout <= MAX_TIMEOUT_SECONDS
        else None,
    }


def _transport(server: dict[str, Any]) -> str:
    kind = server["type"]
    if kind is not None:
        transport = _TRANSPORTS.get(kind)
        if transport is None:
            raise ValueError(f"Transport {kind} is not supported; use stdio, http or sse")
    elif server["command"]:
        transport = "stdio"
    elif server["url"]:
        path = urlsplit(server["url"]).path.rstrip("/")
        transport = "sse" if path.endswith("/sse") else "http"
    else:
        raise ValueError("The server names neither a command nor a URL")
    if transport == "stdio" and not server["command"]:
        raise ValueError("A local server needs a command")
    if transport != "stdio" and not server["url"]:
        raise ValueError("A remote server needs a URL")
    return transport


def _assign_ids(drafts: list[ImportDraft], ids: Mapping[str, str]) -> None:
    unknown = sorted(set(ids) - {draft.name for draft in drafts})
    if unknown:
        raise ValueError(f"No imported server is named {', '.join(unknown)}")
    chosen = {draft.name: ids[draft.name] for draft in drafts if draft.name in ids}
    for identifier in chosen.values():
        if not re.fullmatch(CONNECTION_ID_PATTERN, identifier):
            raise ValueError(
                f"Connection id {identifier} must start with a lowercase letter and use only "
                "lowercase letters, digits and underscores, at most 32"
            )
    if len(set(chosen.values())) != len(chosen):
        raise ValueError("Two imported servers cannot share a connection id")
    taken = set(chosen.values())
    for draft in drafts:
        if draft.name in chosen:
            draft.id = chosen[draft.name]
        else:
            draft.id = _unique(
                connection_id(draft.name), taken, separator="_", limit=_MAX_ID_CHARACTERS
            )
            taken.add(draft.id)
        if draft.error is None:
            _complete(draft)


def _derived_name(server: dict[str, Any]) -> str:
    """The name of an unnamed command or URL: its package, image, module, script or host."""
    if server["command"]:
        return _launched_name(server["command"], server["args"])
    if server["url"]:
        return _host_name(server["url"])
    return "server"


def _complete(draft: ImportDraft) -> None:
    """Fill the connection of *draft*, now that its id names its credentials."""
    server = draft.server
    connection = {"id": draft.id, **draft.connection}
    names: set[str] = set()
    if connection["transport"] == "stdio":
        connection["command"] = _location(draft, "The command", server["command"])
        connection["args"] = [
            _location(draft, f"Argument {index + 1}", argument)
            for index, argument in enumerate(server["args"])
        ]
        _check_arguments(draft, connection["args"])
        _check_bridge(draft, connection["args"])
        if server["cwd"]:
            directory = _with_editor_values(server["cwd"])
            if _REFERENCE.search(directory) or not _absolute(directory):
                draft.warn(
                    "directory_ignored",
                    f"Working directory {server['cwd']} is not an absolute path; "
                    "set one after importing if the server needs it.",
                )
            else:
                connection["cwd"] = directory
        environment: dict[str, str] = {}
        credentials: dict[str, str] = {}
        for key, value in server["env"].items():
            if not re.fullmatch(ENVIRONMENT_KEY_PATTERN, key):
                draft.error = f"Environment variable name {key} is not valid"
                return
            credential = _credential(draft, names, "environment", key, value)
            if credential is None:
                environment[key] = _location(draft, f"Environment variable {key}", value)
            else:
                credentials[key] = credential.name
        connection["environment"] = environment
        connection["credential_environment"] = credentials
    else:
        url = _location(draft, "The URL", server["url"])
        connection["url"] = url
        _check_url(draft, url)
        headers: dict[str, str] = {}
        for header, value in server["headers"].items():
            credential = _credential(draft, names, "header", header, value, required=True)
            assert credential is not None
            headers[header] = credential.name
        connection["credential_headers"] = headers
    connection = {key: value for key, value in connection.items() if value not in ({}, [])}
    try:
        draft.connection = validate_connection(connection)
    except ValueError as error:
        draft.error = str(error)
        draft.credentials.clear()


def _credential(
    draft: ImportDraft,
    names: set[str],
    kind: str,
    target: str,
    value: str,
    *,
    required: bool = False,
) -> ImportedCredential | None:
    """The credential behind one environment variable or header, or ``None`` for a plain value.

    A value that is exactly an ``${env:NAME}`` reference keeps naming that
    variable; any other secret gets a name derived from the connection id.
    Placeholders and empty secrets leave the credential missing. A *required*
    value is a credential even when it does not look secret: a connection
    keeps no plain header values.
    """
    value = _with_editor_values(value)
    reference = _REFERENCE.fullmatch(value)
    derived = _credential_name(draft.id, target)
    if reference is not None and reference.group(1) == "input":
        prompt = draft.inputs.get(reference.group(2), {})
        description = _text(prompt.get("description"))
        return _add_credential(draft, names, derived, kind, target, "missing", description)
    if reference is not None:
        variable = reference.group(2)
        if variable not in _EDITOR_VARIABLES and re.fullmatch(ENVIRONMENT_KEY_PATTERN, variable):
            if reference.group(3):
                draft.warn(
                    "default_ignored",
                    f"The default value of {variable} is not used; set {variable} on the "
                    "vBot host instead.",
                )
            names.add(variable)
            credential = ImportedCredential(variable, kind, target, "reference")
            draft.credentials.append(credential)
            return credential
    secret = required or _secret_name(target) or bool(_SECRET_VALUE.match(value))
    if not secret or (not required and _plain_value(value)):
        return None
    if _REFERENCE.search(value):
        description = f"The complete value, of the form {value}"
        return _add_credential(draft, names, derived, kind, target, "missing", description)
    bare = _SCHEME_PREFIX.sub("", value.strip())
    if not bare or _PLACEHOLDER_VALUE.match(bare):
        return _add_credential(draft, names, derived, kind, target, "missing")
    return _add_credential(draft, names, derived, kind, target, "provided", value=value)


def _add_credential(
    draft: ImportDraft,
    names: set[str],
    name: str,
    kind: str,
    target: str,
    state: str,
    description: str = "",
    *,
    value: str | None = None,
) -> ImportedCredential:
    name = _unique(name, names, separator="_")
    names.add(name)
    credential = ImportedCredential(name, kind, target, state, description, value)
    draft.credentials.append(credential)
    return credential


def _credential_name(identifier: str, target: str) -> str:
    prefix = identifier.upper()
    key = re.sub(r"[^A-Z0-9]+", "_", target.upper()).strip("_") or "SECRET"
    if key == prefix or key.startswith(prefix + "_"):
        key = key[len(prefix) :].lstrip("_") or "SECRET"
    return f"MCP_{prefix}_{key}"


def _secret_name(name: str) -> bool:
    words = [word.upper() for word in re.split(r"[^A-Za-z0-9]+|(?<=[a-z])(?=[A-Z])", name) if word]
    if not words or words[-1] in _SECRET_DESCRIPTORS:
        return False
    joined = "_".join(words)
    return bool(_SECRET_WORDS & set(words)) or any(
        part in joined for part in ("API_KEY", "ACCESS_KEY", "PRIVATE_KEY")
    )


def _plain_value(value: str) -> bool:
    """A value no secret looks like: a switch, a number or a path."""
    text = value.strip()
    if not text:
        return False
    return (
        text.lower() in {"true", "false", "yes", "no", "on", "off"}
        or bool(re.fullmatch(r"-?\d+(?:\.\d+)?", text))
        or _absolute(text)
    )


def _with_editor_values(value: str) -> str:
    """*value* with the editor variables vBot can resolve on its host replaced."""
    return (
        value.replace("${userHome}", str(Path.home()))
        .replace("${pathSeparator}", os.sep)
        .replace("${/}", os.sep)
    )


def _location(draft: ImportDraft, label: str, value: str) -> str:
    """A command, argument, value or URL; a placeholder vBot cannot fill is reported."""
    value = _with_editor_values(value)
    for match in _REFERENCE.finditer(value):
        draft.unresolved = True
        draft.warn(
            "unresolved_placeholder",
            f"{label} contains {match.group(0)}, which vBot cannot fill in; "
            "edit the connection after importing.",
        )
    return value


def _check_arguments(draft: ImportDraft, arguments: list[str]) -> None:
    for index, argument in enumerate(arguments):
        flag, _, inline = argument.partition("=")
        previous = arguments[index - 1] if index else ""
        if inline and _ARGUMENT_SECRET_FLAG.match(flag):
            value = inline
        elif _SECRET_VALUE.match(argument) or (
            _ARGUMENT_SECRET_FLAG.match(previous) and not argument.startswith("-")
        ):
            value = argument
        else:
            continue
        if _PLACEHOLDER_VALUE.match(value):
            draft.unresolved = True
            draft.warn(
                "placeholder_argument",
                f"Argument {index + 1} is a placeholder; edit the connection after importing.",
            )
        elif not _plain_value(value):
            draft.exposes_secret = True
            draft.warn(
                "secret_in_arguments",
                f"Argument {index + 1} looks like a secret, and arguments are saved as plain "
                "configuration. Prefer an environment variable if the server supports one.",
            )


def _check_bridge(draft: ImportDraft, arguments: list[str]) -> None:
    """Point out ``mcp-remote``, a local bridge to a remote server vBot can reach itself."""
    if not any(_artifact_name(argument) == "mcp-remote" for argument in arguments):
        return
    url = next((item for item in arguments if _URL.match(item)), None)
    if url is not None:
        draft.warn(
            "remote_bridge",
            f"This starts mcp-remote to reach {url.split('?')[0]}. vBot can connect to that "
            "URL directly with the HTTP connection type and, if the server asks for it, "
            "OAuth sign-in.",
        )


def _check_url(draft: ImportDraft, url: str) -> None:
    query = parse_qsl(urlsplit(url).query, keep_blank_values=True)
    if any(key.lower() in _URL_SECRET_PARAMETERS and value for key, value in query):
        draft.exposes_secret = True
        draft.warn(
            "secret_in_url",
            "The URL carries a key or token, and URLs are saved as plain configuration. "
            "Prefer a credential header if the server supports one.",
        )


def _absolute(path: str) -> bool:
    return PurePosixPath(path).is_absolute() or PureWindowsPath(path).is_absolute()


def _launched_name(command: str, arguments: list[str]) -> str:
    """The name a launched server is known by, from its package, image, module or script."""
    program = _program_name(command)
    if program not in _LAUNCHERS:
        return _without_affixes(program) or program
    if program in {"python", "python3", "py"} and "-m" in arguments[:-1]:
        return _without_affixes(arguments[arguments.index("-m") + 1].split(".")[0]) or program
    plain = list(_plain_arguments(arguments))
    for index, candidate in enumerate(plain):
        if candidate in _LAUNCHER_SUBCOMMANDS:
            continue
        name = _artifact_name(candidate)
        if name == "mcp-remote":
            url = next((item for item in plain[index + 1 :] if _URL.match(item)), None)
            if url is not None:
                return _host_name(url)
        return _without_affixes(name) or program
    return program


def _plain_arguments(arguments: list[str]) -> list[str]:
    plain = []
    skip = False
    for argument in arguments:
        if skip:
            skip = False
        elif argument.startswith("-"):
            skip = argument in _FLAGS_WITH_VALUES
        else:
            plain.append(argument)
    return plain


def _artifact_name(reference: str) -> str:
    """The base name of a package, image or script reference."""
    reference = re.sub(r"(?<=.)@[^/@]*$", "", reference)  # npm and uv versions
    reference = re.split(r"[=<>~!]=|[<>]", reference)[0]  # Python version specifiers
    parts = [part for part in re.split(r"[\\/]", reference) if part] or [reference]
    script = _SCRIPT.fullmatch(parts[-1])
    if script is not None:
        stem = script.group(1)
        if stem.lower() in _GENERIC_FILE_NAMES:
            folders = [part for part in parts[:-1] if part.lower() not in _BUILD_DIRECTORIES]
            return folders[-1] if folders else stem
        return stem
    return re.sub(r":[^:]*$", "", parts[-1])  # image tags


def _without_affixes(name: str) -> str:
    name = name.lower()
    for prefix in _NAME_PREFIXES:
        if name.startswith(prefix) and len(name) > len(prefix):
            name = name[len(prefix) :]
            break
    for suffix in _NAME_SUFFIXES:
        if name.endswith(suffix) and len(name) > len(suffix):
            name = name[: -len(suffix)]
            break
    return name


def _host_name(url: str) -> str:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if not host or host == "localhost" or ":" in host or re.fullmatch(r"[\d.]+", host):
        segments = [
            segment
            for segment in parts.path.split("/")
            if segment and segment.lower() not in {"mcp", "sse", "v1", "api"}
        ]
        return segments[0] if segments else "local"
    labels = host.split(".")
    if len(labels) > 1:
        top = labels.pop()
        if len(labels) > 1 and len(top) == 2 and labels[-1] in _SECOND_LEVEL_SUFFIXES:
            labels.pop()
    meaningful = [label for label in labels if label not in _GENERIC_HOST_LABELS]
    return (meaningful or labels)[-1]


def _program_name(command: str) -> str:
    name = re.split(r"[\\/]", command)[-1].lower()
    return re.sub(r"\.(?:exe|cmd|bat|ps1)$", "", name)


def _unique(name: str, taken: set[str], *, separator: str, limit: int | None = None) -> str:
    if name not in taken:
        return name
    number = 2
    while True:
        suffix = f"{separator}{number}"
        stem = name if limit is None else name[: limit - len(suffix)].rstrip("_")
        if (candidate := stem + suffix) not in taken:
            return candidate
        number += 1


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return value if isinstance(value, str) else json.dumps(value)
