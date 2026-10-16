"""Model and reasoning choices for the terminal conversation accessor."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from cli.tui.dialogs import Picker, TextDialog
from cli.tui.rendering import pretty
from cli.tui.state import Conversation, JsonObject
from cli.tui.transport import ClientError
from core.settings import ALLOWED_THINKING_EFFORTS

if TYPE_CHECKING:
    from cli.tui.app import ConversationApp


def _current(app: ConversationApp, view: Conversation, key: tuple) -> bool:
    return app.conversation is view and view.key == key and view.available


def configured_value(app: ConversationApp, view: Conversation, field: str) -> Any:
    """Read the Agent/Team Agent's effective value from the loaded roster."""
    if "@" in view.agent:
        agent_id, _separator, project_id = view.agent.partition("@")
        project = next((row for row in app.projects if row.get("project_id") == project_id), None)
        scan = (project or {}).get("scan") or {}
        team = scan.get("team", [])
        row = next((item for item in team if item.get("agent_id") == agent_id), None)
    else:
        row = next((item for item in app.agents if item.get("id") == view.agent), None)
    value = ((row or {}).get("effective") or {}).get(field) or {}
    return value.get("value")


def effective_value(app: ConversationApp, view: Conversation, field: str) -> Any:
    """Resolve a Session capture or draft override before its Agent setting."""
    if field == "thinking_effort" and view.overrides.get("thinking_effort_default") is True:
        return None
    if field in view.overrides and view.overrides[field] is not None:
        return view.overrides[field]
    return configured_value(app, view, field)


def _model_reference(app: ConversationApp, view: Conversation) -> str | None:
    value = effective_value(app, view, "model")
    return value if isinstance(value, str) and value else None


def _current_effort_label(app: ConversationApp, view: Conversation) -> str:
    value = effective_value(app, view, "thinking_effort")
    return value if isinstance(value, str) and value else "Provider default"


def _model_choices(models: Any, current: str | None) -> list[tuple[str, str]]:
    if not isinstance(models, list):
        return []
    current_base = current.partition("::")[0] if current else None
    choices: list[tuple[str, str]] = []
    for row in models:
        if not isinstance(row, dict):
            continue
        provider = row.get("provider_id")
        model_id = row.get("model_id")
        reference = row.get("id")
        if (
            not isinstance(reference, str)
            and isinstance(provider, str)
            and isinstance(model_id, str)
        ):
            reference = f"{provider}/{model_id}"
        if not isinstance(reference, str) or not reference:
            continue
        label = f"{row.get('name') or model_id or reference} · {reference}"
        if reference.partition("::")[0] == current_base:
            label += " · current"
        choices.append((label, reference))
    return choices


def _reasoning_choices(
    model: JsonObject, model_reference: str, current_effort: str | None
) -> list[tuple[str, str]]:
    """Expose the selected Model's effective Connection ladder within accepted values."""

    def default_choice(label: str = "Provider default") -> list[tuple[str, str]]:
        if not current_effort:
            label += " · current"
        return [(label, "")]

    reasoning = (model.get("capabilities") or {}).get("reasoning") or {}
    if reasoning.get("supported") is False:
        return default_choice()

    profiles = model.get("wire_profiles")
    if not isinstance(profiles, dict):
        profiles = {}
    usable = model.get("usable_connections")
    if not isinstance(usable, list):
        usable = list(profiles)
    pinned = model_reference.partition("::")[2] if "::" in model_reference else None
    pinned_connection = pinned.split(":", 1)[0] if pinned else None
    connection: str | None
    if pinned_connection:
        if pinned_connection not in usable:
            return default_choice(
                f"Provider default · pinned Connection {pinned_connection} unavailable"
            )
        connection = pinned_connection
    else:
        connection = next(iter(usable), None)
    profile = profiles.get(connection) if isinstance(connection, str) else None
    ladder = profile.get("reasoning_ladder") if isinstance(profile, dict) else None
    if not isinstance(ladder, list):
        ladder = []

    accepted = ALLOWED_THINKING_EFFORTS - {""}
    mandatory = reasoning.get("mandatory") is True
    efforts: list[str] = []
    for value in ladder:
        if (
            isinstance(value, str)
            and value in accepted
            and not (value == "none" and mandatory)
            and value not in efforts
        ):
            efforts.append(value)
    default_label = "Provider default"
    if connection is None:
        default_label += " · no usable Connection"
    if not current_effort:
        default_label += " · current"
    choices = [(default_label, "")]
    for value in efforts:
        label = f"{value} · current" if value == current_effort else value
        choices.append((label, value))
    return choices


async def menu(app: ConversationApp) -> None:
    """Open model settings while preserving conversation and async stale guards."""
    view = app.conversation
    if view is None or not view.available:
        return
    key = view.key
    current_model = _model_reference(app, view)
    model_label = current_model or "not configured"
    operation = await app.push_screen_wait(
        Picker(
            f"Session settings · Model {model_label} · effort {_current_effort_label(app, view)}",
            [
                ("Choose Model", "model"),
                ("Choose thinking effort", "thinking_effort"),
                ("Inspect context and Usage", "usage"),
            ],
        )
    )
    if not _current(app, view, key):
        return
    if operation == "usage":
        await app.push_screen_wait(
            TextDialog(
                "Context and Usage",
                pretty(
                    {
                        field: view.facts.get(field)
                        for field in ("context_usage", "session_usage", "compaction_policy")
                    }
                ),
                read_only=True,
            )
        )
        return
    if operation == "model":
        response = await app.client.rpc("model.list", {"task": "chat"})
        if not _current(app, view, key):
            return
        choices = _model_choices(response.get("models"), current_model)
        if not choices:
            app._notice("No usable chat Models are available.", error=True)
            return
    elif operation == "thinking_effort":
        choices = [("Provider default", "")]
        if current_model:
            try:
                response = await app.client.rpc("model.get", {"model": current_model})
            except (ClientError, OSError) as exc:
                if not _current(app, view, key):
                    return
                app._notice(f"Could not read Model reasoning capabilities: {exc}", error=True)
            else:
                if not _current(app, view, key):
                    return
                model = response.get("model")
                if isinstance(model, dict):
                    current_effort = effective_value(app, view, "thinking_effort")
                    choices = _reasoning_choices(
                        model,
                        current_model,
                        current_effort if isinstance(current_effort, str) else None,
                    )
        else:
            app._notice("No effective Model is configured; only Provider default is available.")
    else:
        return

    value = await app.push_screen_wait(Picker(f"Choose {operation.replace('_', ' ')}", choices))
    if value is None or not _current(app, view, key):
        return
    changes: JsonObject = {operation: value}
    if operation == "thinking_effort":
        changes = {
            "thinking_effort": value or None,
            "thinking_effort_default": not bool(value),
        }
    if view.session:
        view.refresh_revision += 1
        result = await app.client.rpc(
            "session.set_agent_overrides",
            {**view.params(), "agent_overrides": changes},
        )
        if view.key != key or not view.available:
            return
        view.overrides = result.get("agent_overrides") or {}
    else:
        for field, replacement in changes.items():
            if replacement is None:
                view.overrides.pop(field, None)
            else:
                view.overrides[field] = replacement
    app._view_changed(view)
    app._notice("Session setting saved" if view.session else "Draft setting selected")
