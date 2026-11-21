"""Project the broker's named Secret metadata for the Session reading a request.

Canonical history stays intact. A Secret name in ordinary prose has no broker
meaning. Only the broker's catalogs, reminders, result envelopes and designated
Tool argument fields are interpreted here.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from core.secrets._references import REDACTED, SecretMasker
from core.tools.call_syntax import spelling

if TYPE_CHECKING:
    from core.prompts import RenderedBlock

type JsonObject = dict[str, Any]

_NAME = r"[A-Z][A-Z0-9_]{0,63}"
_CATALOG_ROW = re.compile(rf"^- ({_NAME}) \((?:token|text|login)\b", re.MULTILINE)
_GIVEN = re.compile(
    rf"^The user (?:gave you a Secret for this session|replaced the Secret): ({_NAME}) \("
)
_PASS = "Your Parent Agent passed you these Secrets for this session:\n"
_CATALOG_CHANGE = "Secrets changed since your System Prompt listed them."
_RESULT_NAME = re.compile(
    rf"(?:^The user (?:stored |replaced the value of |declined to give )|"
    rf"^Generated a \d+-character password and stored it as |"
    rf"^Nothing was (?:requested|generated)\. (?:A Secret named )?|"
    rf"^Nobody answered the request for |that you need |/secret )({_NAME})(?!\w)"
)
_CONSUMED = re.compile(
    rf"One-time Secrets {_NAME}(?:, {_NAME})* were used up while preparing this launch\. "
    r"Ask the user to provide them again before retrying\."
)
_INHERITED_ENV = re.compile(
    rf"^({_NAME}(?:, {_NAME})*)(?= (?:is|are) not a granted credential, so the command sees "
    r"the value it inherits; env_keys is only for granted credentials\.$)"
)
_NAME_LIST = re.compile(
    rf"(?P<prefix>Your Secrets: |You cannot pass |Granted credentials(?: for env_keys)?: |"
    rf"env_keys names )(?P<names>{_NAME}(?:, {_NAME})*)(?!\w)"
)
# The broker's specific renderers identify the following slots by their surrounding text.
_DIAGNOSTIC_NAME = re.compile(
    rf"(?P<label>{_NAME})(?:\.(?P<field>[a-z][a-z0-9_]{{0,31}}))?(?= "
    r"(?:is unavailable in this Session|has no field|has no available fields|may not be used|"
    r"changed while|changed, expired|has no value|is not a usable TOTP|comes from))"
    rf"|(?<=did not deliver )(?P<source_name>{_NAME})\.(?P<source_field>[a-z][a-z0-9_]{{0,31}})"
)
_ESCAPED_HINT = re.compile(
    rf"(?<=To write the reference as literal text, use )\{{\{{!secret:({_NAME})"
    r"(?:\.([a-z][a-z0-9_]{0,31}))?\}\}"
)
_ENV_SLOT = re.compile(
    rf"(?P<prefix>env sets |If |pass env_keys: \[\")(?P<name>{_NAME})"
    rf"(?= to a value written in the call| is not a secret|\"\] and leave it out of env)"
    rf"|(?P<credential>{_NAME})(?= is a granted credential: pass env_keys)"
)
_SOURCE_ORIGIN = re.compile(
    rf"\b({_NAME}) comes from (.+?)(?:, which is not available|; ask the user)"
)


class _PromptWithSecretCatalog(str):
    block: RenderedBlock


def with_secret_catalog(prompt: str, block: RenderedBlock | None) -> str:
    """Keep exact catalog entry boundaries through assembly, including multiline field values."""
    if block is None or not block.text or block.text.strip() not in prompt:
        return prompt
    marked = _PromptWithSecretCatalog(prompt)
    marked.block = block
    return marked


def restore_secret_catalog(content: str, originals: list[str]) -> str:
    """An intact projected catalog survives a context hook that rebuilt its string."""
    if isinstance(content, _PromptWithSecretCatalog):
        return content
    for original in originals:
        if isinstance(original, _PromptWithSecretCatalog):
            marked = with_secret_catalog(content, original.block)
            if marked is not content:
                return marked
    return content


def mask_secret_catalog_prompt(content: str, masker: SecretMasker) -> str | None:
    """Mask the full prompt; only the broker's own public header retains its syntax examples."""
    if not isinstance(content, _PromptWithSecretCatalog):
        return None
    from core.tools.secrets import SECRETS_PROMPT_BLOCK_HEAD, secret_line

    block = content.block
    catalog = block.catalog
    pieces = content.split(block.text.strip())
    if catalog is None:
        projected = REDACTED
    else:
        entries = tuple(
            (name, masker.mask(secret_line(record)))
            for name, _ in catalog.entries
            if (record := masker.catalog_record(name)) is not None
        )
        frame = catalog.frame
        frame = (
            masker.mask_documentation(frame)
            if frame == SECRETS_PROMPT_BLOCK_HEAD and len(pieces) == 2
            else masker.mask(frame)
        )
        projected = "\n".join([frame, *(line for _, line in entries)]) if entries else ""
        if entries:
            new_block = replace(
                block, text=projected, catalog=replace(catalog, frame=frame, entries=entries)
            )
            return with_secret_catalog(projected.join(map(masker.mask, pieces)), new_block)
    return projected.join(map(masker.mask, pieces))


def _unavailable_rows(text: str, masker: SecretMasker, *, reminder: bool = False) -> bool:
    """Authenticate complete legacy rows, including multiline descriptions and source hints."""
    from core.providers._tool_result_text import neutralize_system_reminder_tags
    from core.tools.secrets import SECRET_USE_HINT, secret_line

    end = 0
    while match := _CATALOG_ROW.search(text, end):
        name = match[1]
        record = masker.catalog_record(name)
        if record is None:
            return True
        line = secret_line(record)
        if reminder:
            line = neutralize_system_reminder_tags(line)
        start = match.start()
        if not text.startswith(line, start):
            line = masker.mask(line)
            if not text.startswith(line, start):
                return True
        end = start + len(line)
        if end < len(text) and text[end] != "\n":
            return True
        next_row = _CATALOG_ROW.search(text, end)
        trailer = text[end : next_row.start() if next_row else None].strip("\n")
        source = masker.catalog_source_label(name)
        allowed = {
            "Added:",
            "Changed to:",
            "Removed:",
            SECRET_USE_HINT,
            SECRET_USE_HINT + " The user can see the password in Settings -> Secrets.",
        }
        if source:
            allowed.update(
                f"  It comes from {source}, which is {state}: "
                "ask the user to unlock it in Settings -> Secrets."
                for state in ("locked", "unavailable")
            )
        if trailer and any(item not in allowed for item in trailer.splitlines()):
            return True
    return False


def _reminder_body(body: str, masker: SecretMasker) -> str:
    if given := _GIVEN.match(body):
        from core.providers._tool_result_text import neutralize_system_reminder_tags
        from core.tools.secrets import SECRET_USE_HINT, secret_line

        record = masker.catalog_record(given[1])
        if record is None:
            return REDACTED
        expected = body[: given.start(1)] + neutralize_system_reminder_tags(secret_line(record)[2:])
        if body.startswith("The user gave you a Secret"):
            expected += " " + SECRET_USE_HINT
        return body if body in (expected, masker.mask(expected)) else REDACTED
    catalog = (
        body.startswith(_PASS)
        or _CATALOG_CHANGE in body
        or "## Secrets\n\nYou can use these Secrets" in body
    )
    if catalog and _unavailable_rows(body, masker, reminder=True):
        # Legacy entries can contain arbitrary multiline public fields and
        # descriptions. Never guess which continuation text belongs to whom.
        return REDACTED
    return _consumed_note(body)


def _consumed_note(body: str) -> str:
    if "One-time Secrets " not in body:
        return body
    from core.tools._shell_results import CONSUMED_SECRETS_NOTE

    return _CONSUMED.sub(CONSUMED_SECRETS_NOTE, body)


def _diagnostic(body: str, masker: SecretMasker) -> str:
    # A name can refer to a different record after Takeover. A stored provider
    # error can include the former source label and account in its own prose.
    for source_error in _DIAGNOSTIC_NAME.finditer(body):
        if name := source_error["source_name"]:
            source_label = masker.catalog_source_label(name)
            if not (
                source_label
                and body[: source_error.start()].endswith(f"{source_label} did not deliver ")
                and masker.allows_reference(name, source_error["source_field"])
            ):
                return "List the available Secrets before trying again."
    if (origin := _SOURCE_ORIGIN.search(body)) and masker.catalog_source_label(origin[1]) != origin[
        2
    ]:
        return "List the available Secrets before trying again."

    def names(match: re.Match[str]) -> str:
        allowed = (
            masker.allows_name
            if match["prefix"] in {"Your Secrets: ", "You cannot pass "}
            else masker.allows_environment_name
        )
        return match["prefix"] + ", ".join(
            name if allowed(name) else REDACTED for name in match["names"].split(", ")
        )

    def label(match: re.Match[str]) -> str:
        name = match["label"] or match["source_name"]
        field = match["field"] or match["source_field"]
        if not masker.allows_name(name):
            return REDACTED
        return match[0] if field is None or masker.allows_reference(name, field) else REDACTED

    def env(match: re.Match[str]) -> str:
        name = match["name"] or match["credential"]
        return (
            match[0] if masker.allows_environment_name(name) else (match["prefix"] or "") + REDACTED
        )

    body = _ENV_SLOT.sub(env, _NAME_LIST.sub(names, body))
    body = _ESCAPED_HINT.sub(
        lambda match: match[0] if masker.allows_reference(match[1], match[2]) else REDACTED, body
    )
    return _DIAGNOSTIC_NAME.sub(label, body)


def _project_argument(value: Any, allowed: Callable[[str], bool], *, csv: bool = False) -> Any:
    if isinstance(value, list):
        return [_project_argument(item, allowed) for item in value]
    if isinstance(value, str):
        if csv and "," in value:
            return ",".join(_project_argument(part, allowed) for part in value.split(","))
        return value if allowed(value.strip()) else REDACTED
    return value


def _project_arguments(arguments: Any, tool: str, masker: SecretMasker, depth: int = 0) -> Any:
    if depth > 8:
        return REDACTED
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except ValueError, RecursionError:
            return arguments
        if isinstance(parsed, dict):
            projected = _project_arguments(parsed, tool, masker, depth)
            if projected is not parsed:
                return json.dumps(projected, ensure_ascii=False)
    if not isinstance(arguments, dict):
        return arguments
    changes = {}
    for key, value in arguments.items():
        if tool == "secrets" and key in {"name", "secret", "secret_name", "key"}:
            new = _project_argument(value, masker.allows_name)
        elif tool == "subagent" and key == "secrets":
            new = _project_argument(value, masker.allows_name, csv=True)
        elif tool in {"bash", "powershell"} and spelling(key) == "envkeys":
            new = _project_argument(value, masker.allows_environment_name)
        elif tool in {"bash", "powershell"} and spelling(key) in {"request", "arguments", "action"}:
            new = _project_arguments(value, tool, masker, depth + 1)
        else:
            continue
        if value != new:
            changes[key] = new
    return {**arguments, **changes} if changes else arguments


def _project_call(call: Any, masker: SecretMasker) -> Any:
    if not isinstance(call, dict) or call.get("name") not in (
        "secrets",
        "subagent",
        "bash",
        "powershell",
    ):
        return call
    arguments = call.get("arguments")
    new = _project_arguments(arguments, call["name"], masker)
    return call if new is arguments else {**call, "arguments": new}


def _project_result(raw: str, tool: str, masker: SecretMasker) -> str:
    shell = tool in {"bash", "powershell", "background_tasks"}
    if (
        tool != "secrets"
        and "secret_unavailable" not in raw
        and not (
            shell
            and (
                "One-time Secrets " in raw
                or "Granted credentials" in raw
                or "env_keys names" in raw
                or "env sets " in raw
                or "env_keys is only for granted credentials" in raw
            )
        )
    ):
        return raw
    try:
        result = json.loads(raw)
    except ValueError, RecursionError:
        return REDACTED if tool == "secrets" else raw
    if not isinstance(result, dict):
        return REDACTED if tool == "secrets" else raw
    projected = project_secret_result(result, tool, masker)
    return (
        raw
        if projected is result
        else json.dumps(projected, ensure_ascii=False, separators=(",", ":"))
    )


def project_secret_result(result: JsonObject, tool: str, masker: SecretMasker) -> JsonObject:
    """Apply the current recipient's grants to broker-rendered text in a Tool Result."""
    if not masker.filters_references:
        return result
    shell = tool in {"bash", "powershell", "background_tasks"}
    ok = result.get("ok") is True
    slot = "data" if ok else "error"
    payload = result.get(slot)
    field = "content" if ok else "message"
    if not isinstance(payload, dict):
        return result
    if shell and ok:
        if isinstance(reason := payload.get("not_run_because"), str):
            projected_reason = _consumed_note(reason)
            if projected_reason != reason:
                payload = {**payload, "not_run_because": projected_reason}
        if isinstance(notes := payload.get("notes"), list):
            projected_notes = [
                _INHERITED_ENV.sub(
                    lambda match: ", ".join(
                        name if masker.allows_environment_name(name) else REDACTED
                        for name in match[1].split(", ")
                    ),
                    note,
                )
                if isinstance(note, str)
                else note
                for note in notes
            ]
            if projected_notes != notes:
                payload = {**payload, "notes": projected_notes}
        if payload is not result[slot]:
            result = {**result, slot: payload}
    if not isinstance(body := payload.get(field), str):
        return result
    if tool == "secrets":
        unavailable = _unavailable_rows(body, masker) or any(
            not masker.allows_name(match[1]) for match in _RESULT_NAME.finditer(body)
        )
        projected = REDACTED if unavailable else _diagnostic(body, masker) if not ok else body
    elif not ok and (
        payload.get("code") == "secret_unavailable"
        or (shell and payload.get("code") == "invalid_arguments")
    ):
        projected = _diagnostic(body, masker)
    else:
        projected = body
    if shell:
        projected = _consumed_note(projected)
    if projected == body:
        return result
    return {**result, slot: {**payload, field: projected}}


def project_secret_history(message: JsonObject, masker: SecretMasker) -> JsonObject:
    if not masker.filters_references:
        return message
    role = message.get("role")
    content = message.get("content")
    if role == "assistant" and isinstance(calls := message.get("tool_calls"), list):
        projected = [_project_call(call, masker) for call in calls]
        if any(before is not after for before, after in zip(calls, projected, strict=True)):
            return {**message, "tool_calls": projected}
    if not isinstance(content, str):
        return message
    if role == "tool":
        text = _project_result(content, message.get("name", ""), masker)
    elif role == "user":
        from core.chat.wire_shaping import (
            SYSTEM_REMINDER_CLOSE_TAG,
            SYSTEM_REMINDER_OPEN_TAG,
            is_system_reminder_request_message,
        )

        if not is_system_reminder_request_message(message):
            return message
        start, end = SYSTEM_REMINDER_OPEN_TAG + "\n", "\n" + SYSTEM_REMINDER_CLOSE_TAG
        separator = end + "\n" + start
        bodies = content.removeprefix(start).removesuffix(end).split(separator)
        text = type(content)(
            start + separator.join(_reminder_body(body, masker) for body in bodies) + end
        )
    else:
        return message
    return {**message, "content": text} if text != content else message
