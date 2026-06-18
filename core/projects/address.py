"""The one parse/format seam for the ``agent@projekt`` address form.

The address form ``agent@projekt`` is the **outside** spelling of a project
agent (CLI, RPC entry, spawn target, display). Inside the system the project is
an explicit dimension — a bare ``agent_id`` plus an optional ``project_id`` — so
the full address is structured **once**, here, at the system edge, and the rest
of the code threads the two fields separately (plan decision: "eine Parse/Format
-Stelle, danach getrennte Felder").

- :func:`parse_agent_address` turns an outside string into ``(agent_id,
  project_id | None)``. No ``@`` means an identity address (``project_id`` is
  ``None``), exactly as today. Both parts are validated through the shared id
  rules (``is_valid_agent_id`` / ``is_valid_project_id``); anything malformed
  raises :class:`InvalidAgentAddressError` with a clear message.
- :func:`format_agent_address` is the inverse: a ``None`` project gives the bare
  agent id back; a set project gives ``agent@projekt``.

Server and CLI both import these (never re-parse ``@`` locally), so the address
grammar lives in exactly one place.
"""

from __future__ import annotations

from core.settings import is_valid_agent_id, is_valid_project_id

# The single separator between the agent id and the project id in the outside
# address form. Named so no caller hard-codes the literal.
_ADDRESS_SEPARATOR = "@"


class InvalidAgentAddressError(ValueError):
    """Raised when an ``agent@projekt`` address is malformed.

    Expected (handled-locally) failure: an empty agent id, an unknown
    separator count, or an agent/project part that fails the shared id rules.
    Callers at the system edge map it to a clean ``invalid_request``.
    """


def parse_agent_address(address: str) -> tuple[str, str | None]:
    """Parse an outside ``agent@projekt`` address into ``(agent_id, project_id)``.

    No ``@`` → ``(address, None)`` (identity address, unchanged behavior). One
    ``@`` → ``(agent_id, project_id)`` with both parts validated. Anything else
    (empty, more than one ``@``, or an invalid part) raises
    :class:`InvalidAgentAddressError`.
    """
    if not isinstance(address, str) or not address:
        raise InvalidAgentAddressError("agent address must be a non-empty string")

    if _ADDRESS_SEPARATOR not in address:
        if not is_valid_agent_id(address):
            raise InvalidAgentAddressError(f"invalid agent id: {address!r}")
        return address, None

    parts = address.split(_ADDRESS_SEPARATOR)
    if len(parts) != 2:
        raise InvalidAgentAddressError(
            f"agent address must be 'agent' or 'agent@projekt', got: {address!r}"
        )
    agent_id, project_id = parts
    if not is_valid_agent_id(agent_id):
        raise InvalidAgentAddressError(f"invalid agent id in address {address!r}: {agent_id!r}")
    if not is_valid_project_id(project_id):
        raise InvalidAgentAddressError(f"invalid project id in address {address!r}: {project_id!r}")
    return agent_id, project_id


def format_agent_address(agent_id: str, project_id: str | None) -> str:
    """Build the outside address from a bare agent id and an optional project id.

    ``project_id is None`` → the bare ``agent_id`` (identity spelling). A set
    ``project_id`` → ``agent@projekt``. This is the inverse of
    :func:`parse_agent_address`; it does not re-validate (callers hold ids the
    system already accepted).
    """
    if project_id is None:
        return agent_id
    return f"{agent_id}{_ADDRESS_SEPARATOR}{project_id}"
