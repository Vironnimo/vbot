"""Agent Secret requests and the `/secret` form for the open conversation.

A value the operator types into a Secret form lives only in the form's masked
fields until its RPC carries it to the server: it never enters a draft, the
input history, a notice or a log. Requests are read from `agent_secrets.list`;
an answer or a refusal is never retried on its own.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from cli.tui.dialogs import SecretDialog
from cli.tui.state import Conversation, JsonObject
from cli.tui.transport import ClientError

if TYPE_CHECKING:
    from cli.tui.app import ConversationApp

_UNKNOWN = (
    "The previous answer's outcome is unknown and nothing was retried. The request "
    "is still waiting, so storing again is safe."
)


# How long a Secret is kept, for the notice that its lifetime changed.
_KEPT = {
    "permanent": "permanently",
    "session": "for this Session",
    "once": "for one use",
    "until": "until its expiry",
}


def _address(request: JsonObject) -> str:
    agent = str(request.get("agent_id") or "")
    project = request.get("project_id")
    return f"{agent}@{project}" if project else agent


def requests_for(app: ConversationApp, view: Conversation | None) -> list[JsonObject]:
    """The pending requests of the conversation's Session, oldest first."""
    if view is None or not view.session:
        return []
    return [
        request
        for request in app.secret_requests
        if request.get("session_id") == view.session and _address(request) == view.agent
    ]


def status_text(app: ConversationApp, view: Conversation) -> str:
    """The status row's line while the Agent waits for a Secret, else empty."""
    waiting = requests_for(app, view)
    if not waiting:
        return ""
    if len(waiting) == 1:
        return f"The Agent asks for the Secret {waiting[0].get('name', '')} · F7 answers"
    return f"The Agent asks for {len(waiting)} Secrets · F7 answers"


async def load_requests(app: ConversationApp) -> None:
    """Read the pending requests; close the form of one that went away."""
    response = await app.client.rpc("agent_secrets.list")
    requests = response.get("requests")
    app.secret_requests = [item for item in requests or [] if isinstance(item, dict)]
    pending = {request.get("id") for request in app.secret_requests}
    screen = app.screen
    if (
        isinstance(screen, SecretDialog)
        and screen.request_id is not None
        and screen.request_id not in pending
    ):
        screen.withdraw()
    app._header()


def _still_waiting(app: ConversationApp, request: JsonObject) -> bool:
    return any(item.get("id") == request.get("id") for item in app.secret_requests)


async def answer(app: ConversationApp, request: JsonObject) -> None:
    """Answer one request through its form: Store, Decline, or leave it waiting."""
    name = str(request.get("name") or "")
    values: JsonObject | None = None
    problem = ""
    unknown = False
    while True:
        result = await app.push_screen_wait(
            SecretDialog(request=request, values=values, problem=problem, outcome_unknown=unknown)
        )
        if result is None:
            return
        if result.get("action") == "withdrawn":
            app._notice(f"The Agent stopped asking for {name}. Nothing was stored.")
            return
        try:
            if result.get("action") == "decline":
                await app.client.rpc("agent_secrets.decline", {"id": request["id"]})
                notice = f"Declined {name}. The Agent continues without it."
            else:
                params: JsonObject = {"id": request["id"], "values": result["values"]}
                if result.get("lifetime"):
                    params["lifetime"] = result["lifetime"]
                if result.get("expires_at"):
                    params["expires_at"] = result["expires_at"]
                stored = await app.client.rpc("agent_secrets.answer", params)
                notice = _stored_notice(stored, name, replaced=bool(request.get("replaces")))
        except ClientError as exc:
            # Only the server knows whether the request still waits.
            await load_requests(app)
            if not _still_waiting(app, request):
                app._notice(
                    str(exc)
                    if exc.state == "responded"
                    else f"The request for {name} is no longer waiting. {exc}",
                    error=exc.state == "responded",
                )
                return
            values = result if result.get("action") == "store" else values
            unknown = exc.state == "unknown"
            problem = f"{_UNKNOWN}\n{exc}" if unknown else str(exc)
            continue
        app._notice(notice)
        await load_requests(app)
        return


async def give(app: ConversationApp, view: Conversation) -> bool:
    """Give the conversation's Agent a Secret through the form; True once stored."""
    if not view.session:
        app._notice(
            "The /secret form needs the Session a first message creates. Send a message "
            "first, or write /secret NAME VALUE."
        )
        return False
    agent, _, project = view.agent.partition("@")
    address: JsonObject = {"agent_id": agent, "session_id": view.session}
    if project:
        address["project_id"] = project
    values: JsonObject | None = None
    problem = ""
    unknown = False
    while True:
        result = await app.push_screen_wait(
            SecretDialog(values=values, problem=problem, outcome_unknown=unknown)
        )
        if result is None:
            return False
        params: JsonObject = {
            **address,
            "name": result["name"],
            "kind": result["kind"],
            "values": result["values"],
        }
        # Without a lifetime a replaced Secret keeps its own; a new one lasts for the Session.
        if result.get("lifetime"):
            params["lifetime"] = result["lifetime"]
        if result.get("expires_at"):
            params["expires_at"] = result["expires_at"]
        if result.get("description"):
            params["description"] = result["description"]
        try:
            given = await app.client.rpc("agent_secrets.give", params)
        except ClientError as exc:
            values = result
            unknown = exc.state == "unknown"
            problem = (
                "The previous outcome is unknown and nothing was retried; storing again "
                f"replaces the value with the same one.\n{exc}"
                if unknown
                else str(exc)
            )
            continue
        app._notice(_stored_notice(given, result["name"], replaced=bool(given.get("replaced"))))
        return True


def _stored_notice(response: JsonObject, name: str, *, replaced: bool) -> str:
    secret: Any = response.get("secret")
    secret = secret if isinstance(secret, dict) else {}
    stored = str(secret.get("name") or name)
    if replaced:
        kept = _KEPT.get(str(secret.get("lifetime")))
        if response.get("previous_lifetime") and kept:
            return f"Replaced the value of {stored}. It is now kept {kept}."
        return f"Replaced the value of {stored}."
    reference = secret.get("reference")
    if reference:
        return f"Stored {stored}. The Agent sees only {reference}."
    return f"Stored {stored}."
