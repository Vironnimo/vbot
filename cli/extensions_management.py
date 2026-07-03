"""Extension visibility, enable/disable, and reload RPC commands for the vBot CLI.

`list` reads `extensions.list`; `reload` drives `extensions.reload` (a full,
restart-equivalent rebuild of the whole extension layer from disk); `enable` /
`disable` are thin wrappers over `settings.update` (full-replace `extensions`
section). Every change applies **live** without a restart: disabling deactivates
the extension immediately (hooks off, tools gone, shutdown fired), and enabling
rebuilds the layer so newly-loaded code takes effect at once.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from difflib import get_close_matches
from typing import Any

from cli.rpc_client import httpx as httpx
from cli.rpc_client import rpc_call as _rpc_call
from cli.server_management import CommandResult, ServerInstance

_INTEGER_RE = re.compile(r"[+-]?\d+")
_RELOAD_STATUSES = ("loaded", "failed", "disabled", "overridden")


def extensions_list(instance: ServerInstance) -> CommandResult:
    """Return formatted extension catalog output from `extensions.list` RPC."""

    extensions = _load_extensions(instance)
    if isinstance(extensions, CommandResult):
        return extensions
    return CommandResult(ok=True, message=_format_extension_rows(extensions), instance=instance)


def extensions_reload(instance: ServerInstance) -> CommandResult:
    """Rebuild the whole extension layer live via `extensions.reload`.

    Prints one summary line with all four status counts, one line per failed
    extension, and a pointer at `vbot extensions list` when anything failed.
    """

    result = _rpc_call(instance, "extensions.reload", {})
    if not result.ok:
        return result.to_command_result()
    extensions = result.data.get("extensions")
    if not isinstance(extensions, list):
        return CommandResult(
            ok=False, message="RPC result missing extensions list", instance=instance
        )
    return CommandResult(ok=True, message=_format_reload_summary(extensions), instance=instance)


def _format_reload_summary(extensions: Sequence[Any]) -> str:
    counts = dict.fromkeys(_RELOAD_STATUSES, 0)
    for extension in extensions:
        if isinstance(extension, dict) and extension.get("status") in counts:
            counts[extension["status"]] += 1
    summary = ", ".join(f"{counts[status]} {status}" for status in _RELOAD_STATUSES)
    lines = [f"extensions reloaded: {summary}"]

    failed = [ext for ext in extensions if isinstance(ext, dict) and ext.get("status") == "failed"]
    for ext in failed:
        name = _string_or_default(ext.get("name"), "?")
        error = ext.get("error")
        detail = error if isinstance(error, str) and error else "unknown error"
        lines.append(f"  {name} failed: {detail}")
    if failed:
        lines.append("run 'vbot extensions list' for details")
    return "\n".join(lines)


def extensions_enable(instance: ServerInstance, name: str) -> CommandResult:
    """Remove *name* from the disabled set via `settings.update` (applied live)."""

    return _set_disabled(instance, name, disable=False)


def extensions_disable(instance: ServerInstance, name: str) -> CommandResult:
    """Add *name* to the disabled set via `settings.update` (applied live, no restart)."""

    return _set_disabled(instance, name, disable=True)


def _set_disabled(instance: ServerInstance, name: str, *, disable: bool) -> CommandResult:
    extensions = _load_extensions(instance)
    if isinstance(extensions, CommandResult):
        return extensions

    known_names = [
        ext["name"]
        for ext in extensions
        if isinstance(ext, dict) and isinstance(ext.get("name"), str)
    ]
    if name not in known_names:
        return CommandResult(
            ok=False, message=_format_unknown_extension(name, known_names), instance=instance
        )

    currently_disabled = [
        ext["name"] for ext in extensions if isinstance(ext, dict) and ext.get("disabled")
    ]
    if disable and name in currently_disabled:
        return CommandResult(
            ok=True,
            message=f"extension '{name}' is already disabled (no change)",
            instance=instance,
        )
    if not disable and name not in currently_disabled:
        return CommandResult(
            ok=True, message=f"extension '{name}' is already enabled (no change)", instance=instance
        )

    if disable:
        disabled = [*currently_disabled, name]
    else:
        disabled = [other for other in currently_disabled if other != name]

    config = {
        ext["name"]: ext["config"]
        for ext in extensions
        if isinstance(ext, dict) and isinstance(ext.get("config"), dict) and ext["config"]
    }

    update = _rpc_call(
        instance, "settings.update", {"extensions": {"disabled": disabled, "config": config}}
    )
    if not update.ok:
        return update.to_command_result()

    if disable:
        return CommandResult(ok=True, message=f"extension '{name}' disabled", instance=instance)
    return _enabled_result(instance, name)


def _enabled_result(instance: ServerInstance, name: str) -> CommandResult:
    """Report a live enable, warning when the freshly-loaded extension is not loaded.

    Enabling rebuilds the extension layer live, so we re-list and check the target:
    a non-``loaded`` outcome (it failed to import, or is overridden by another copy)
    is surfaced as a warning even though the toggle itself succeeded. If the re-list
    fails, the enable is still reported as a plain success.
    """
    lines = [f"extension '{name}' enabled (applied live)"]
    extensions = _load_extensions(instance)
    if not isinstance(extensions, CommandResult):
        record = _find_extension(extensions, name)
        if record is not None and record.get("status") != "loaded":
            status = _string_or_default(record.get("status"), "?")
            lines.append(f"warning: '{name}' is {status}")
            error = record.get("error")
            if isinstance(error, str) and error:
                lines.append(f"  error: {error}")
    return CommandResult(ok=True, message="\n".join(lines), instance=instance)


def extensions_show(instance: ServerInstance, name: str) -> CommandResult:
    """Show one extension's settings: schema fields, current values, secret state.

    This is the read half of the settings surface (``vbot extensions <name>``): for
    a schema'd extension it renders each field with its live value (a secret shows
    only ``set``/``not set``, never the value); a schema-less extension falls back to
    its raw persisted config.
    """

    extensions = _load_extensions(instance)
    if isinstance(extensions, CommandResult):
        return extensions

    record = _find_extension(extensions, name)
    if record is None:
        return CommandResult(
            ok=False,
            message=_format_unknown_extension(name, _known_names(extensions)),
            instance=instance,
        )
    return CommandResult(ok=True, message=_format_extension_settings(record), instance=instance)


def extensions_set(instance: ServerInstance, name: str, field: str, value: str) -> CommandResult:
    """Write one extension setting, routed by the field's declared schema type.

    A ``secret`` field goes to ``.env`` via ``extensions.set_secret`` (the server maps
    the field key to its declared env key — the caller never names the env key). Every
    other field type is coerced to its declared type and written to the extension's
    live config via ``settings.update``. Both take effect without a restart.
    """

    extensions = _load_extensions(instance)
    if isinstance(extensions, CommandResult):
        return extensions

    record = _find_extension(extensions, name)
    if record is None:
        return CommandResult(
            ok=False,
            message=_format_unknown_extension(name, _known_names(extensions)),
            instance=instance,
        )
    if record.get("status") != "loaded":
        return CommandResult(
            ok=False,
            message=f"extension '{name}' is not loaded, so its settings cannot be set",
            instance=instance,
        )

    schema = record.get("settings_schema")
    if not isinstance(schema, list) or not schema:
        return CommandResult(
            ok=False,
            message=f"extension '{name}' declares no settings schema, so it has no settable fields",
            instance=instance,
        )

    field_declaration = _find_field(schema, field)
    if field_declaration is None:
        return CommandResult(
            ok=False,
            message=_format_unknown_field(name, field, schema),
            instance=instance,
        )

    if field_declaration.get("type") == "secret":
        return _set_secret(instance, name, field, value)

    coerced, error = _coerce_value(str(field_declaration.get("type")), value)
    if error is not None:
        return CommandResult(ok=False, message=f"{field}: {error}", instance=instance)
    return _set_config_value(instance, extensions, name, field, coerced)


def _set_secret(instance: ServerInstance, name: str, field: str, value: str) -> CommandResult:
    result = _rpc_call(
        instance, "extensions.set_secret", {"name": name, "key": field, "value": value}
    )
    if not result.ok:
        return result.to_command_result()
    if result.data.get("set"):
        message = f"secret '{field}' set for '{name}' (stored in .env, applied live)"
    else:
        message = f"secret '{field}' cleared for '{name}'"
    return CommandResult(ok=True, message=message, instance=instance)


def _set_config_value(
    instance: ServerInstance,
    extensions: Sequence[Any],
    name: str,
    field: str,
    value: Any,
) -> CommandResult:
    disabled = [ext["name"] for ext in extensions if isinstance(ext, dict) and ext.get("disabled")]
    config = {
        ext["name"]: dict(ext["config"])
        for ext in extensions
        if isinstance(ext, dict) and isinstance(ext.get("config"), dict) and ext["config"]
    }
    config.setdefault(name, {})[field] = value

    update = _rpc_call(
        instance, "settings.update", {"extensions": {"disabled": disabled, "config": config}}
    )
    if not update.ok:
        return update.to_command_result()

    message = f"set '{name}.{field}' = {json.dumps(value)} (applied live)"
    return CommandResult(ok=True, message=message, instance=instance)


def _coerce_value(field_type: str, value: str) -> tuple[Any, str | None]:
    """Coerce a CLI string to the field's declared type, or return an error message."""

    if field_type == "number":
        text = value.strip()
        try:
            return (int(text) if _INTEGER_RE.fullmatch(text) else float(text)), None
        except ValueError:
            return None, f"'{value}' is not a number"
    if field_type == "toggle":
        low = value.strip().lower()
        if low in ("true", "1", "yes", "on"):
            return True, None
        if low in ("false", "0", "no", "off"):
            return False, None
        return None, f"'{value}' is not a boolean (use true or false)"
    return value, None


def _find_extension(extensions: Sequence[Any], name: str) -> dict[str, Any] | None:
    for extension in extensions:
        if isinstance(extension, dict) and extension.get("name") == name:
            return extension
    return None


def _find_field(schema: Sequence[Any], field: str) -> dict[str, Any] | None:
    for declaration in schema:
        if isinstance(declaration, dict) and declaration.get("key") == field:
            return declaration
    return None


def _known_names(extensions: Sequence[Any]) -> list[str]:
    return [
        ext["name"]
        for ext in extensions
        if isinstance(ext, dict) and isinstance(ext.get("name"), str)
    ]


def _format_extension_settings(record: dict[str, Any]) -> str:
    name = _string_or_default(record.get("name"), "?")
    status = _string_or_default(record.get("status"), "?")
    lines = [f"{name}  {status}"]

    schema = record.get("settings_schema")
    raw_config = record.get("config")
    config: dict[str, Any] = raw_config if isinstance(raw_config, dict) else {}

    if not isinstance(schema, list):
        if status == "loaded":
            lines.append("  no settings schema")
            if config:
                lines.append(f"  raw config: {json.dumps(config)}")
        return "\n".join(lines)
    if not schema:
        lines.append("  no settings")
        return "\n".join(lines)

    lines.append("settings:")
    for declaration in schema:
        if isinstance(declaration, dict):
            lines.append(_format_settings_field(declaration, config))
    lines.append(f"set with: vbot extensions {name} set <field> <value>")
    return "\n".join(lines)


def _format_settings_field(declaration: dict[str, Any], config: dict[str, Any]) -> str:
    key = _string_or_default(declaration.get("key"), "?")
    field_type = _string_or_default(declaration.get("type"), "?")
    label = declaration.get("label")
    suffix = f"   {label}" if isinstance(label, str) and label else ""

    if field_type == "secret":
        state = "set" if declaration.get("set") else "not set"
        return f"  {key} (secret): {state}{suffix}"
    if key in config:
        return f"  {key} ({field_type}): {json.dumps(config[key])}{suffix}"
    default = declaration.get("default")
    if default is not None:
        return f"  {key} ({field_type}): (default {json.dumps(default)}){suffix}"
    return f"  {key} ({field_type}): (unset){suffix}"


def _format_unknown_field(name: str, field: str, schema: Sequence[Any]) -> str:
    available = [
        str(declaration.get("key"))
        for declaration in schema
        if isinstance(declaration, dict) and declaration.get("key")
    ]
    lines = [f"extension '{name}' has no setting '{field}'"]
    if available:
        lines.append(f"available settings: {', '.join(available)}")
        suggestions = get_close_matches(field, available, n=1)
        if suggestions:
            lines.append(f"did you mean: {suggestions[0]}")
    return "\n".join(lines)


def _load_extensions(instance: ServerInstance) -> list[Any] | CommandResult:
    payload = _rpc_call(instance, "extensions.list", {})
    if not payload.ok:
        return payload.to_command_result()
    extensions = payload.data.get("extensions")
    if not isinstance(extensions, list):
        return CommandResult(
            ok=False, message="RPC result missing extensions list", instance=instance
        )
    return extensions


def _format_extension_rows(extensions: Sequence[object]) -> str:
    if not extensions:
        return "no extensions discovered"

    lines = ["extensions:"]
    for extension in extensions:
        lines.extend(_format_extension_row(extension))
    return "\n".join(lines)


def _format_extension_row(extension: object) -> list[str]:
    if not isinstance(extension, dict):
        return ["- invalid extension entry"]

    name = _string_or_default(extension.get("name"), "?")
    status = _string_or_default(extension.get("status"), "?")
    header = f"- {name}  {status}"
    version = extension.get("version")
    if isinstance(version, str) and version:
        header += f"  v{version}"
    description = extension.get("description")
    if isinstance(description, str) and description:
        header += f"  {description}"

    rows = [header]
    error = extension.get("error")
    if isinstance(error, str) and error:
        rows.append(f"    error: {error}")
    overridden_by = extension.get("overridden_by")
    if isinstance(overridden_by, str) and overridden_by:
        rows.append(f"    overridden by {overridden_by}")
    waiting_row = _format_waiting(extension)
    if waiting_row:
        rows.append(f"    {waiting_row}")
    capabilities = _format_capabilities(extension.get("capabilities"))
    if capabilities:
        rows.append(f"    {capabilities}")
    capability_errors = extension.get("capability_errors")
    if isinstance(capability_errors, list):
        for capability_error in capability_errors:
            if isinstance(capability_error, str) and capability_error:
                rows.append(f"    warning: {capability_error}")
    return rows


def _format_waiting(extension: dict[str, object]) -> str:
    """Render the derived waiting state, naming the not-ready tools.

    Only a ``loaded`` extension whose derived ``ready_state`` is ``"waiting"``
    prints this line. It names the not-ready tools and points at how to fix it, so
    an agent reading the output knows the next step is to configure the extension.
    """
    if extension.get("ready_state") != "waiting":
        return ""
    not_ready = _not_ready_tool_names(extension.get("capabilities"))
    suffix = f" ({', '.join(not_ready)})" if not_ready else ""
    name = _string_or_default(extension.get("name"), "<name>")
    return (
        f"waiting for configuration{suffix}: "
        f"run 'vbot extensions {name}' to see its settings, then "
        f"'vbot extensions {name} set <field> <value>' (or Settings > Extensions)"
    )


def _not_ready_tool_names(capabilities: object) -> list[str]:
    if not isinstance(capabilities, dict):
        return []
    tools = capabilities.get("tools")
    if not isinstance(tools, list):
        return []
    return [
        tool["name"]
        for tool in tools
        if isinstance(tool, dict)
        and isinstance(tool.get("name"), str)
        and tool.get("ready") is False
    ]


def _format_capabilities(capabilities: object) -> str:
    if not isinstance(capabilities, dict):
        return ""

    parts: list[str] = []
    hooks = capabilities.get("hooks")
    if isinstance(hooks, dict) and hooks:
        hook_summary = ", ".join(f"{event}({count})" for event, count in hooks.items())
        parts.append(f"hooks: {hook_summary}")
    tools = capabilities.get("tools")
    if isinstance(tools, list) and tools:
        parts.append(f"tools: {', '.join(_format_tool_entry(tool) for tool in tools)}")
    backends = capabilities.get("recall_backends")
    if isinstance(backends, list) and backends:
        parts.append(f"recall_backends: {', '.join(str(backend) for backend in backends)}")
    if capabilities.get("startup"):
        parts.append("startup")
    if capabilities.get("shutdown"):
        parts.append("shutdown")
    return "; ".join(parts)


def _format_tool_entry(tool: object) -> str:
    """Render one capability tool as ``name`` (or ``name (waiting)`` if not ready).

    Each tool is a ``{"name", "ready"}`` object; a not-ready tool is marked inline
    so the tool list itself shows what the extension is waiting on.
    """
    if not isinstance(tool, dict):
        return str(tool)
    name = tool.get("name")
    if not isinstance(name, str) or not name:
        return str(tool)
    return name if tool.get("ready") is not False else f"{name} (waiting)"


def _format_unknown_extension(name: str, candidates: list[str]) -> str:
    lines = [f"extension '{name}' not found"]
    if candidates:
        lines.append(f"available extensions: {', '.join(candidates)}")
        suggestions = get_close_matches(name, candidates, n=1)
        if suggestions:
            lines.append(f"did you mean: {suggestions[0]}")
    return "\n".join(lines)


def _string_or_default(value: object, default: str) -> str:
    if isinstance(value, str) and value:
        return value
    return default
