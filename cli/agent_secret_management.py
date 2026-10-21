"""Agent Secret commands: the values Agents use by reference, their requests and Sources.

Every command goes through the ``agent_secrets.*`` RPCs and names a Secret by its
name or id, which ``agent_secrets.list`` resolves. A value enters only from a
hidden prompt or from piped stdin (``--value-stdin``), never from the command
line, and only ``reveal`` prints one. What hands out access refuses inside an
Agent's Run, like ``devices approve``: revealing a value, answering or declining
a request, unlocking a Secret Source, and granting a Secret to more Agents or
allowing it in more places. Values an Agent supplies itself (``add``, a new
value) stay allowed there.
"""

from __future__ import annotations

import getpass
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from difflib import get_close_matches
from typing import Any

from cli._input import _read_stdin_utf8
from cli._recovery import format_command, target_options
from cli._server_target import is_url_target
from cli.device_management import run_refusal
from cli.formatting import inline_text, record_fields
from cli.rpc_client import rpc_call
from cli.server_management import CommandResult, ServerInstance

JsonObject = Mapping[str, Any]

_RUN_AGENT_ENV = "VBOT_RUN_AGENT_ID"
#: Entries a source listing shows before it asks for a narrower one.
ENTRY_LIMIT = 50
_STORED_LIFETIMES = frozenset({"permanent", "until"})
_SETTINGS = "Settings -> Secrets"


class _InputError(Exception):
    """A value could not be read; the message never contains one."""


@dataclass(frozen=True)
class _Field:
    name: str
    label: str
    concealed: bool = True
    optional: bool = False


# -- commands --------------------------------------------------------------------------


def agent_secrets_list(instance: ServerInstance) -> CommandResult:
    """Every Agent Secret and the Secret requests waiting for the user."""
    overview = _overview(instance)
    if isinstance(overview, CommandResult):
        return overview
    secrets = _items(overview, "secrets")
    requests = _items(overview, "requests")
    lines = [
        "No Agent Secrets."
        if not secrets
        else f"{_count(len(secrets), 'Agent Secret')}{', by name' if len(secrets) > 1 else ''}:"
    ]
    lines.extend(_secret_row(secret) for secret in secrets)
    if requests:
        waits = "waits" if len(requests) == 1 else "wait"
        lines.append(f"{_count(len(requests), 'Secret request')} {waits} for the user's answer:")
        lines.extend(_request_row(request) for request in requests)
    else:
        lines.append("No Secret request waits for the user.")
    return CommandResult(ok=True, message="\n".join(lines), instance=instance)


def agent_secrets_add(
    instance: ServerInstance,
    name: str,
    *,
    kind: str | None,
    user: str | None,
    agents: Sequence[str] | None,
    sinks: Sequence[str] | None,
    description: str | None,
    expires_at: datetime | None,
    value_stdin: bool,
) -> CommandResult:
    """Store a new Secret; inside an Agent's Run it is granted to that Agent by default."""
    normalized = _normalized_name(name)
    if normalized is None:
        return _refused(
            instance,
            "Secret names start with a letter and use letters, digits and underscores, such "
            "as GITHUB_TOKEN. Nothing was stored.",
        )
    kind = kind or ("login" if user is not None else "token")
    if user is not None and kind != "login":
        return _refused(
            instance,
            f"--user is a login's username; a {kind} Secret has none. Nothing was stored. "
            "Drop --user, or use --kind login.",
        )
    overview = _overview(instance)
    if isinstance(overview, CommandResult):
        return overview
    existing = next(
        (
            secret
            for secret in _items(overview, "secrets")
            if secret.get("name") == normalized and secret.get("session_id") is None
        ),
        None,
    )
    if existing is not None:
        return _refused(
            instance,
            f"An Agent Secret named {normalized} already exists "
            f"({inline_text(existing.get('id'))}). Nothing was stored. To replace its value, "
            "run: "
            f"{_command(instance, 'update', normalized, '--value-stdin')}",
        )
    run_agent = os.environ.get(_RUN_AGENT_ENV, "").strip()
    grants = list(agents) if agents is not None else ([run_agent] if run_agent else [])
    try:
        values = _read_fields(
            _kind_fields(kind, new=True),
            subject=normalized,
            value_stdin=value_stdin,
            multiline=kind == "text",
        )
    except _InputError as error:
        return _refused(instance, f"{error} Nothing was stored.")
    if user is not None:
        values["username"] = user
    params: dict[str, Any] = {"name": normalized, "kind": kind, "values": values, "agents": grants}
    if sinks is not None:
        params["sinks"] = list(sinks)
    if description:
        params["description"] = description
    if expires_at is not None:
        params["expires_at"] = expires_at.isoformat()
    payload = rpc_call(instance, "agent_secrets.create", params)
    if not payload.ok:
        return payload.to_command_result()
    secret = payload.data.get("secret")
    if not isinstance(secret, Mapping):
        return _missing(instance, "secret")
    return CommandResult(
        ok=True,
        message=f"Stored the Agent Secret {_name(secret)}{_grant_clause(secret)}\n"
        + _secret_row(secret),
        instance=instance,
    )


def agent_secrets_update(
    instance: ServerInstance,
    target: str,
    *,
    agents: Sequence[str] | None,
    sinks: Sequence[str] | None,
    description: str | None,
    expires_at: datetime | None,
    permanent: bool,
    user: str | None,
    value_stdin: bool,
) -> CommandResult:
    """Change the named parts of a Secret and show it as it is now."""
    overview = _overview(instance)
    if isinstance(overview, CommandResult):
        return overview
    secret = _resolve_secret(instance, overview, target, nothing="Nothing was changed.")
    if isinstance(secret, CommandResult):
        return secret
    name = _name(secret)
    kind = str(secret.get("kind"))
    if (user is not None or value_stdin) and isinstance(secret.get("source"), Mapping):
        return _refused(
            instance,
            f"{name} takes its values from {_source_name(secret)}; change them there. "
            "Nothing was changed.",
        )
    if user is not None and kind != "login":
        return _refused(
            instance,
            f"--user is a login's username; {name} is a {kind} Secret. Nothing was changed.",
        )
    widened = _widening(secret, agents, sinks)
    if widened and os.environ.get(_RUN_AGENT_ENV, "").strip():
        retry = _command(
            instance,
            "update",
            name,
            *(["--agent", *agents] if agents is not None else []),
            *(["--sink", *sinks] if sinks is not None else []),
        )
        return _refused(
            instance,
            "Inside an Agent's Run, agent-secrets update does not grant a Secret to more "
            f"Agents or allow it in more places (here: {widened}). Nothing was changed. The "
            f"user can make this change in {_SETTINGS} or run in their terminal: {retry}",
        )
    values: dict[str, str] = {}
    if value_stdin:
        try:
            values = _read_fields(
                _kind_fields(kind, new=False),
                subject=name,
                value_stdin=True,
                multiline=kind == "text",
                keep_empty=True,
            )
        except _InputError as error:
            return _refused(instance, f"{error} Nothing was changed.")
        if not values:
            return _refused(instance, "No new value was entered. Nothing was changed.")
    if user is not None:
        values["username"] = user
    params: dict[str, Any] = {"id": secret.get("id")}
    if values:
        params["values"] = values
    if agents is not None:
        params["agents"] = list(agents)
    if sinks is not None:
        params["sinks"] = list(sinks)
    if description is not None:
        params["description"] = description
    if expires_at is not None:
        params["expires_at"] = expires_at.isoformat()
    if permanent:
        params["permanent"] = True
    payload = rpc_call(instance, "agent_secrets.update", params)
    if not payload.ok:
        return payload.to_command_result()
    updated = payload.data.get("secret")
    if not isinstance(updated, Mapping):
        return _missing(instance, "secret")
    changed = _changed_parts(secret, updated, values_given=bool(values))
    first = (
        f"Updated the Agent Secret {name}: {', '.join(changed)}."
        if changed
        else f"{name} already had these settings; nothing was changed."
    )
    return CommandResult(ok=True, message=f"{first}\n{_secret_row(updated)}", instance=instance)


def agent_secrets_delete(instance: ServerInstance, target: str) -> CommandResult:
    """Delete a Secret; a name that no Secret has is reported, not an error."""
    overview = _overview(instance)
    if isinstance(overview, CommandResult):
        return overview
    matches = _secret_matches(overview, target)
    if not matches:
        return CommandResult(
            ok=True,
            message=f"{_absent(overview, target)} Nothing was deleted.",
            instance=instance,
            attention=("nothing was deleted",),
        )
    secret = _resolve_secret(instance, overview, target, nothing="Nothing was deleted.")
    if isinstance(secret, CommandResult):
        return secret
    payload = rpc_call(instance, "agent_secrets.delete", {"id": secret.get("id")})
    if not payload.ok:
        return payload.to_command_result()
    name, secret_id = _name(secret), inline_text(secret.get("id"))
    after = (
        f"its entry in {_source_name(secret)} stays."
        if isinstance(secret.get("source"), Mapping)
        else "its stored value cannot be restored."
    )
    return CommandResult(
        ok=True,
        message=f"Deleted the Agent Secret {name} ({secret_id}); {after}",
        instance=instance,
    )


def agent_secrets_reveal(instance: ServerInstance, target: str, field: str | None) -> CommandResult:
    """Print one field's value, the only command whose output carries one."""
    refusal = run_refusal(
        "agent-secrets reveal",
        "vBot shows Secret values only as their references there. Nothing was revealed. The "
        f"user can see the value in {_SETTINGS} or run in their terminal: "
        + _command(instance, "reveal", target, *(["--field", field] if field else [])),
    )
    if refusal is not None:
        return _refused(instance, refusal)
    overview = _overview(instance)
    if isinstance(overview, CommandResult):
        return overview
    secret = _resolve_secret(instance, overview, target, nothing="Nothing was revealed.")
    if isinstance(secret, CommandResult):
        return secret
    names = [str(item.get("name")) for item in _items(secret, "fields")]
    chosen = field.strip().lower() if field else _default_field(names)
    if chosen not in names:
        return _refused(
            instance,
            f"{_name(secret)} has no field {inline_text(chosen)}; its fields are "
            f"{', '.join(inline_text(item) for item in names)}. Nothing was revealed.",
        )
    payload = rpc_call(instance, "agent_secrets.reveal", {"id": secret.get("id"), "field": chosen})
    if not payload.ok:
        return payload.to_command_result()
    value = payload.data.get("value")
    if not isinstance(value, str):
        return _missing(instance, "value")
    return CommandResult(ok=True, message=value, instance=instance)


def agent_secrets_answer(
    instance: ServerInstance,
    target: str,
    *,
    user: str | None,
    permanent: bool,
    expires_at: datetime | None,
    value_stdin: bool,
) -> CommandResult:
    """Store the user's answer to a waiting request; the requesting Agent continues."""
    refusal = run_refusal(
        "agent-secrets answer",
        "requests are the user's to answer, in the form vBot shows them or in their "
        f"terminal: {_command(instance, 'answer', target)}. Nothing was stored.",
    )
    if refusal is not None:
        return _refused(instance, refusal)
    overview = _overview(instance)
    if isinstance(overview, CommandResult):
        return overview
    request = _resolve_request(instance, overview, target, nothing="Nothing was stored.")
    if isinstance(request, CommandResult):
        return request
    kind = str(request.get("kind"))
    name = inline_text(request.get("name"))
    if user is not None and kind != "login":
        return _refused(
            instance,
            f"--user is a login's username; the request asks for a {kind} Secret. "
            "Nothing was stored.",
        )
    try:
        values = _read_fields(
            _kind_fields(kind, new=True),
            subject=name,
            value_stdin=value_stdin,
            multiline=kind == "text",
        )
    except _InputError as error:
        return _refused(instance, f"{error} Nothing was stored.")
    if user is not None:
        values["username"] = user
    params: dict[str, Any] = {"id": request.get("id"), "values": values}
    if permanent:
        params["lifetime"] = "permanent"
    if expires_at is not None:
        params["lifetime"] = "until"
        params["expires_at"] = expires_at.isoformat()
    payload = rpc_call(instance, "agent_secrets.answer", params)
    if not payload.ok:
        return payload.to_command_result()
    secret = payload.data.get("secret")
    if not isinstance(secret, Mapping):
        return _missing(instance, "secret")
    agent = inline_text(request.get("agent_id"))
    return CommandResult(
        ok=True,
        message=f"Answered the request of {agent} for {name}; the Agent continues with it.\n"
        + _secret_row(secret),
        instance=instance,
    )


def agent_secrets_decline(instance: ServerInstance, target: str) -> CommandResult:
    refusal = run_refusal(
        "agent-secrets decline",
        "requests are the user's to answer, in the form vBot shows them or in their "
        f"terminal: {_command(instance, 'decline', target)}. Nothing was changed.",
    )
    if refusal is not None:
        return _refused(instance, refusal)
    overview = _overview(instance)
    if isinstance(overview, CommandResult):
        return overview
    request = _resolve_request(instance, overview, target, nothing="Nothing was changed.")
    if isinstance(request, CommandResult):
        return request
    payload = rpc_call(instance, "agent_secrets.decline", {"id": request.get("id")})
    if not payload.ok:
        return payload.to_command_result()
    return CommandResult(
        ok=True,
        message=(
            f"Declined the request of {inline_text(request.get('agent_id'))} for "
            f"{inline_text(request.get('name'))}; the Agent continues without it."
        ),
        instance=instance,
    )


def agent_secrets_sources(instance: ServerInstance) -> CommandResult:
    overview = _overview(instance)
    if isinstance(overview, CommandResult):
        return overview
    sources = _items(overview, "sources")
    if not sources:
        return CommandResult(
            ok=True,
            message="No Secret Sources: no enabled Extension provides one.",
            instance=instance,
        )
    lines = [f"{_count(len(sources), 'Secret Source')}:"]
    lines.extend(_source_row(source) for source in sources)
    return CommandResult(ok=True, message="\n".join(lines), instance=instance)


def agent_secrets_source_unlock(
    instance: ServerInstance, target: str, *, value_stdin: bool
) -> CommandResult:
    refusal = run_refusal(
        "agent-secrets source unlock",
        f"only the user unlocks a Secret Source, in {_SETTINGS} or in their terminal: "
        f"{_command(instance, 'source', 'unlock', target)}. Nothing was unlocked.",
    )
    if refusal is not None:
        return _refused(instance, refusal)
    overview = _overview(instance)
    if isinstance(overview, CommandResult):
        return overview
    source = _resolve_source(instance, overview, target)
    if isinstance(source, CommandResult):
        return source
    label = inline_text(source.get("label") or source.get("name"))
    fields = [
        _Field(
            name=str(item.get("name")),
            label=inline_text(item.get("label") or item.get("name")),
            concealed=item.get("concealed") is not False,
            optional=item.get("required") is False,
        )
        for item in _items(source, "unlock_fields")
    ]
    try:
        inputs = (
            _read_fields(fields, subject=label, value_stdin=value_stdin, multiline=False)
            if fields
            else {}
        )
    except _InputError as error:
        return _refused(instance, f"{error} Nothing was unlocked.")
    payload = rpc_call(
        instance, "agent_secrets.source_unlock", {"source": source.get("name"), "inputs": inputs}
    )
    if not payload.ok:
        return payload.to_command_result()
    state = payload.data.get("state")
    if state != "available":
        reason = payload.data.get("reason")
        return CommandResult(
            ok=False,
            message=f"{label} is still {inline_text(state)} after unlocking"
            + (f": {inline_text(reason)}." if reason else ".")
            + "\n"
            + _source_row(payload.data),
            instance=instance,
        )
    return CommandResult(
        ok=True, message=f"Unlocked {label}.\n{_source_row(payload.data)}", instance=instance
    )


def agent_secrets_source_lock(instance: ServerInstance, target: str) -> CommandResult:
    overview = _overview(instance)
    if isinstance(overview, CommandResult):
        return overview
    source = _resolve_source(instance, overview, target)
    if isinstance(source, CommandResult):
        return source
    payload = rpc_call(instance, "agent_secrets.source_lock", {"source": source.get("name")})
    if not payload.ok:
        return payload.to_command_result()
    label = inline_text(source.get("label") or source.get("name"))
    return CommandResult(
        ok=True,
        message=f"Locked {label}; Secrets from it fail until the user unlocks it again.\n"
        + _source_row(payload.data),
        instance=instance,
    )


def agent_secrets_source_entries(
    instance: ServerInstance, target: str, *, container: str | None, query: str | None
) -> CommandResult:
    overview = _overview(instance)
    if isinstance(overview, CommandResult):
        return overview
    source = _resolve_source(instance, overview, target)
    if isinstance(source, CommandResult):
        return source
    params: dict[str, Any] = {"source": source.get("name")}
    if container is not None:
        params["container"] = container
    payload = rpc_call(instance, "agent_secrets.source_entries", params)
    if not payload.ok:
        return payload.to_command_result()
    label = inline_text(source.get("label") or source.get("name"))
    entries = _items(payload.data, "entries")
    if query:
        needle = query.casefold()
        entries = [item for item in entries if needle in str(item.get("title", "")).casefold()]
    scope = "".join(
        [
            f" in {inline_text(container)}" if container is not None else "",
            f" whose title contains {inline_text(query)!r}" if query else "",
        ]
    )
    lines = [
        f"{label} has no entries{scope}."
        if not entries
        else f"{_count(len(entries), 'entry', 'entries')}{scope} in {label}"
        + (f"; showing the first {ENTRY_LIMIT}:" if len(entries) > ENTRY_LIMIT else ":")
    ]
    lines.extend(_entry_row(entry) for entry in entries[:ENTRY_LIMIT])
    containers = [inline_text(item) for item in payload.data.get("containers") or ()]
    if len(entries) > ENTRY_LIMIT:
        lines.append(
            "Narrow the list with --query and part of a title"
            + (
                ", or --container and a container name."
                if containers and container is None
                else "."
            )
        )
    if containers and container is None:
        lines.append(f"Containers: {', '.join(containers)}")
    return CommandResult(ok=True, message="\n".join(lines), instance=instance)


# -- resolution --------------------------------------------------------------------------


def _overview(instance: ServerInstance) -> JsonObject | CommandResult:
    payload = rpc_call(instance, "agent_secrets.list", {})
    if not payload.ok:
        return payload.to_command_result()
    return payload.data


def _secret_matches(overview: JsonObject, target: str) -> list[JsonObject]:
    secrets = _items(overview, "secrets")
    by_id = [secret for secret in secrets if secret.get("id") == target.strip()]
    if by_id:
        return by_id
    name = _normalized_name(target)
    return [secret for secret in secrets if name is not None and secret.get("name") == name]


def _resolve_secret(
    instance: ServerInstance, overview: JsonObject, target: str, *, nothing: str
) -> JsonObject | CommandResult:
    matches = _secret_matches(overview, target)
    if not matches:
        return _refused(instance, f"{_absent(overview, target)} {nothing}")
    if len(matches) > 1:
        choices = ", ".join(
            f"{inline_text(item.get('id'))} ({_lifetime_text(item)})" for item in matches
        )
        return _refused(
            instance,
            f"{_name(matches[0])} names {len(matches)} Agent Secrets; name one by its id: "
            f"{choices}. {nothing}",
        )
    return matches[0]


def _absent(overview: JsonObject, target: str) -> str:
    names = [str(item.get("name")) for item in _items(overview, "secrets")]
    shown = _normalized_name(target) or target.strip()
    if not names:
        return f"No Agent Secret is named {inline_text(shown)}; there are no Agent Secrets."
    closest = get_close_matches(shown, names, n=3, cutoff=0.6)
    listed = closest if closest else names[:20]
    label = "Closest names" if closest else "Agent Secrets"
    more = f" and {len(names) - len(listed)} more" if not closest and len(names) > 20 else ""
    return (
        f"No Agent Secret is named {inline_text(shown)}. {label}: "
        f"{', '.join(inline_text(item) for item in listed)}{more}."
    )


def _resolve_request(
    instance: ServerInstance, overview: JsonObject, target: str, *, nothing: str
) -> JsonObject | CommandResult:
    requests = _items(overview, "requests")
    matches = [item for item in requests if item.get("id") == target.strip()]
    if not matches:
        name = _normalized_name(target)
        matches = [item for item in requests if name is not None and item.get("name") == name]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        waiting = ", ".join(
            f"{inline_text(item.get('id'))} ({inline_text(item.get('name'))})" for item in requests
        )
        return _refused(
            instance,
            f"No waiting Secret request has the id or name {inline_text(target.strip())}; a "
            f"request no longer waits once it is answered, declined or withdrawn. {nothing} "
            + (f"Waiting requests: {waiting}." if waiting else "No request waits."),
        )
    choices = ", ".join(
        f"{inline_text(item.get('id'))} (from {inline_text(item.get('agent_id'))})"
        for item in matches
    )
    return _refused(
        instance,
        f"{len(matches)} requests ask for {inline_text(matches[0].get('name'))}; name one by "
        f"its id: {choices}. {nothing}",
    )


def _resolve_source(
    instance: ServerInstance, overview: JsonObject, target: str
) -> JsonObject | CommandResult:
    sources = _items(overview, "sources")
    wanted = target.strip().casefold()
    match = next(
        (
            source
            for source in sources
            if wanted in {str(source.get("name")).casefold(), str(source.get("label")).casefold()}
        ),
        None,
    )
    if match is not None:
        return match
    available = ", ".join(inline_text(source.get("name")) for source in sources)
    return _refused(
        instance,
        f"No Secret Source is named {inline_text(target.strip())}. Nothing was changed. "
        + (
            f"Secret Sources: {available}."
            if available
            else "No enabled Extension provides a Secret Source."
        ),
    )


def _widening(secret: JsonObject, agents: Sequence[str] | None, sinks: Sequence[str] | None) -> str:
    """What a change adds to who may use a Secret and where; empty when it adds nothing."""
    added: list[str] = []
    if agents is not None:
        new = [agent for agent in agents if agent not in _strings(secret.get("agents"))]
        if new:
            added.append(f"Agents {', '.join(inline_text(agent) for agent in new)}")
    if sinks is not None:
        new = [sink for sink in sinks if sink not in _strings(secret.get("sinks"))]
        if new:
            added.append(f"uses {', '.join(new)}")
    return " and ".join(added)


def _changed_parts(before: JsonObject, after: JsonObject, *, values_given: bool) -> list[str]:
    parts = [
        label
        for key, label in (
            ("agents", "granted Agents"),
            ("sinks", "allowed uses"),
            ("description", "description"),
            ("lifetime", "lifetime"),
            ("expires_at", "expiry"),
        )
        if before.get(key) != after.get(key)
    ]
    if values_given:
        parts.append("value")
    return parts


def _normalized_name(raw: str) -> str | None:
    from core.secrets.agent_secrets import SecretRecordError, normalize_secret_name

    try:
        return normalize_secret_name(raw)
    except SecretRecordError:
        return None


# -- values -------------------------------------------------------------------------------


def _kind_fields(kind: str, *, new: bool) -> list[_Field]:
    """The fields a value input fills, in the order of the prompts and stdin lines."""
    if kind != "login":
        return [_Field("value", "Value" if new else "New value")]
    if new:
        return [
            _Field("password", "Password"),
            _Field("totp", "TOTP seed", optional=True),
        ]
    return [
        _Field("password", "New password", optional=True),
        _Field("totp", "New TOTP seed", optional=True),
    ]


def _read_fields(
    fields: Sequence[_Field],
    *,
    subject: str,
    value_stdin: bool,
    multiline: bool,
    keep_empty: bool = False,
) -> dict[str, str]:
    """Read ``fields`` from hidden prompts in a terminal, else from piped stdin.

    An empty optional field is left out; with ``keep_empty`` an empty field of
    any kind is (an update keeps it). Raises :class:`_InputError`.
    """
    stdin = sys.stdin
    if stdin is not None and stdin.isatty():
        if multiline:
            raise _InputError(
                "A text Secret holds several lines, which a hidden prompt cannot take. Pipe "
                "the value into the same command and add --value-stdin."
            )
        return _prompted(fields, subject=subject, keep_empty=keep_empty)
    if not value_stdin:
        raise _InputError(
            "The value comes from a hidden prompt or from stdin, but stdin is not a terminal "
            "and --value-stdin was not given. Pipe the value into the same command with "
            "--value-stdin, or let the user run the command in their terminal."
        )
    if stdin is None:
        raise _InputError("--value-stdin was given, but this process has no stdin.")
    try:
        text = _read_stdin_utf8() if not multiline else _read_stdin_exact()
    except OSError, UnicodeError:
        raise _InputError("stdin is not readable UTF-8 text.") from None
    if multiline:
        if not text.strip():
            raise _InputError("stdin was empty.")
        return {fields[0].name: text}
    lines = [line.removesuffix("\r") for line in text.split("\n")] if text else []
    if len(lines) > len(fields):
        raise _InputError(f"stdin has {len(lines)} lines; {_line_layout(fields)}.")
    values: dict[str, str] = {}
    for field, line in zip(fields, lines, strict=False):
        if line:
            values[field.name] = line
    missing = [
        field.label.lower()
        for field in fields
        if field.name not in values and not field.optional and not keep_empty
    ]
    if missing:
        raise _InputError(f"stdin has no {' or '.join(missing)}.")
    return values


def _line_layout(fields: Sequence[_Field]) -> str:
    names = [field.name for field in fields]
    if names == ["value"]:
        return "a token is one line; for several lines use --kind text"
    if names == ["password", "totp"]:
        return (
            "a login takes the password on the first line and an optional TOTP seed on the second"
        )
    labels = ", ".join(field.label for field in fields)
    return f"it takes one line per field, in this order: {labels}"


def _prompted(fields: Sequence[_Field], *, subject: str, keep_empty: bool) -> dict[str, str]:
    values: dict[str, str] = {}
    for field in fields:
        if field.optional or keep_empty:
            hint = "; press Enter to keep it" if keep_empty else "; press Enter if there is none"
        else:
            hint = ""
        shown = "not shown" if field.concealed else "shown"
        prompt = f"{field.label} of {subject} ({shown}{hint}): "
        try:
            value = getpass.getpass(prompt) if field.concealed else input(prompt)
        except EOFError, OSError:
            raise _InputError(
                f"The {field.label.lower()} could not be read from the terminal."
            ) from None
        if value:
            values[field.name] = value
        elif not (field.optional or keep_empty):
            raise _InputError(f"No {field.label.lower()} was entered.")
    return values


def _read_stdin_exact() -> str:
    """Piped text kept as it is, apart from a leading BOM: line breaks are its content."""
    buffer = getattr(sys.stdin, "buffer", None)
    if buffer is None:
        return sys.stdin.read()
    raw = buffer.read()
    return raw.decode("utf-8-sig") if isinstance(raw, bytes) else str(raw)


# -- output -------------------------------------------------------------------------------


def _secret_row(secret: JsonObject) -> str:
    lifetime = str(secret.get("lifetime"))
    fields = [
        _name(secret),
        f"id={inline_text(secret.get('id'))}",
        f"kind={inline_text(secret.get('kind'))}",
        f"lifetime={inline_text(lifetime)}",
    ]
    if secret.get("expires_at"):
        fields.append(f"expires={inline_text(secret.get('expires_at'))}")
    if secret.get("session_id"):
        fields.append(f"session={inline_text(secret.get('session_id'))}")
    if lifetime in _STORED_LIFETIMES:
        fields.append(f"agents={_joined(secret.get('agents'))}")
    fields.append(f"sinks={_joined(secret.get('sinks'))}")
    fields.append("fields=" + ",".join(_field_text(item) for item in _items(secret, "fields")))
    source = secret.get("source")
    if isinstance(source, Mapping):
        fields.append(f"source={inline_text(source.get('source'))}")
        fields.append(f"source_state={inline_text(source.get('state'))}")
    if secret.get("description"):
        fields.append(f"description={inline_text(secret.get('description'))}")
    return record_fields(fields)


def _request_row(request: JsonObject) -> str:
    fields = [
        inline_text(request.get("id")),
        f"name={inline_text(request.get('name'))}",
        f"kind={inline_text(request.get('kind'))}",
        f"agent={inline_text(request.get('agent_id'))}",
    ]
    if request.get("project_id"):
        fields.append(f"project={inline_text(request.get('project_id'))}")
    fields += [
        f"session={inline_text(request.get('session_id'))}",
        f"lifetime={inline_text(request.get('lifetime'))}",
        f"replaces={'yes' if request.get('replaces') else 'no'}",
        f"waits_until={inline_text(request.get('expires_at'))}",
    ]
    if request.get("description"):
        fields.append(f"description={inline_text(request.get('description'))}")
    return record_fields(fields)


def _source_row(source: JsonObject) -> str:
    fields = [
        inline_text(source.get("name")),
        f"label={inline_text(source.get('label'))}",
        f"state={inline_text(source.get('state'))}",
    ]
    unlock = [
        inline_text(item.get("name")) + ("" if item.get("required") is not False else "?")
        for item in _items(source, "unlock_fields")
    ]
    fields.append(f"unlock_fields={','.join(unlock) or 'none'}")
    if source.get("reason"):
        fields.append(f"reason={inline_text(source.get('reason'))}")
    return record_fields(fields)


def _entry_row(entry: JsonObject) -> str:
    fields = [
        inline_text(entry.get("reference")),
        f"title={inline_text(entry.get('title'))}",
        f"kind={inline_text(entry.get('kind'))}",
    ]
    if entry.get("container"):
        fields.append(f"container={inline_text(entry.get('container'))}")
    fields.append("fields=" + ",".join(_field_text(item) for item in _items(entry, "fields")))
    return record_fields(fields)


def _field_text(item: JsonObject) -> str:
    name = inline_text(item.get("name"))
    value = item.get("value")
    return f"{name}:{inline_text(value)}" if isinstance(value, str) else name


def _grant_clause(secret: JsonObject) -> str:
    if str(secret.get("lifetime")) not in _STORED_LIFETIMES:
        return "."
    agents = _strings(secret.get("agents"))
    granted = (
        f" for {', '.join(inline_text(agent) for agent in agents)}"
        if agents
        else "; no Agent can use it until update --agent grants it"
    )
    nowhere = (
        "" if _strings(secret.get("sinks")) else "; it is allowed nowhere until --sink allows a use"
    )
    return f"{granted}{nowhere}."


def _lifetime_text(secret: JsonObject) -> str:
    lifetime = inline_text(secret.get("lifetime"))
    if secret.get("session_id"):
        return f"{lifetime}, Session {inline_text(secret.get('session_id'))}"
    if secret.get("expires_at"):
        return f"{lifetime} {inline_text(secret.get('expires_at'))}"
    return lifetime


def _default_field(names: Sequence[str]) -> str:
    if len(names) == 1:
        return names[0]
    fallback = names[0] if names else ""
    return next((name for name in ("password", "value") if name in names), fallback)


def _source_name(secret: JsonObject) -> str:
    source = secret.get("source")
    return inline_text(source.get("source")) if isinstance(source, Mapping) else "its source"


def _name(item: JsonObject) -> str:
    return inline_text(item.get("name"))


def _items(data: JsonObject, key: str) -> list[JsonObject]:
    value = data.get(key)
    return [item for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []


def _strings(value: object) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _joined(value: object) -> str:
    return ",".join(inline_text(item) for item in _strings(value)) or "none"


def _count(number: int, singular: str, plural: str | None = None) -> str:
    return f"{number} {singular if number == 1 else plural or singular + 's'}"


def _command(instance: ServerInstance, *tokens: str) -> str:
    """A follow-up command line that reaches the same server."""
    return format_command(
        (
            "vbot",
            "agent-secrets",
            *tokens,
            *target_options(instance, include_data_dir=not is_url_target(instance)),
        )
    )


def _refused(instance: ServerInstance, message: str) -> CommandResult:
    return CommandResult(ok=False, message=message, instance=instance)


def _missing(instance: ServerInstance, key: str) -> CommandResult:
    return CommandResult(ok=False, message=f"RPC result missing {key}", instance=instance)
