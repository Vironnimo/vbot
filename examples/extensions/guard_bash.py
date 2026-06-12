"""Example extension: block obviously destructive ``bash`` commands.

Copy this file into ``<data_dir>/extensions/`` (``~/.vbot/extensions/`` by
default) to enable it on the next server start.

It demonstrates the ``tool_call`` **decision hook**. A handler inspects a
pending tool call and returns one of:

- ``None``           — proceed unchanged (the common case)
- ``Modify(input)``  — rewrite the tool's arguments, then proceed
- ``Deny(reason)``   — refuse the call; the tool never runs
- ``Replace(result)``— skip the tool and use a result envelope instead

Here we refuse a few unmistakably destructive shell commands and drop a
``<system-reminder>`` note (via ``ctx.add_note``) so the model learns why its
call was blocked.

This is illustrative, **not** a security boundary: extensions run with full
kernel trust and the pattern list below is deliberately tiny. Treat it as a
starting point, not a sandbox.
"""

from __future__ import annotations

from core.extensions import Deny

# Substrings that flag a command as destructive enough to refuse outright.
# A real guard would do far more (and probably allowlist instead) — kept short
# on purpose so the example stays readable.
_DANGEROUS_SUBSTRINGS = (
    "rm -rf /",
    "mkfs",
    "dd if=",
    ":(){ :|:& };:",  # classic fork bomb
)


def _guard_bash(ctx, *, tool_name, tool_call_id, input):
    # Only the bash tool is interesting; every other tool proceeds untouched.
    if tool_name != "bash":
        return None

    command = input.get("command", "")
    if not isinstance(command, str):
        return None

    for needle in _DANGEROUS_SUBSTRINGS:
        if needle in command:
            # Leave a breadcrumb for the model, then refuse. The reason surfaces
            # to the model as a tool_call_denied result naming this extension.
            ctx.add_note(f"guard_bash blocked a bash command matching {needle!r}.")
            return Deny(reason=f"Refused: command matches dangerous pattern {needle!r}.")

    return None


def register(api):
    # register(api) only *declares* — the runtime wires the handler into the
    # tool_call pipeline during its apply phase. Nothing runs at import time.
    api.on("tool_call", _guard_bash)
