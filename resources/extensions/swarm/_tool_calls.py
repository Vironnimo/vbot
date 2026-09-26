"""Owner-selected call repairs for the Swarm Session Tools.

Each normalizer runs before central argument validation. It reads call syntax
only: wrappers, other harnesses' field and action names, an action implied by
the supplied fields, placeholders in optional fields, and fields the Tool derives
itself. Values that name Board or Wiki objects are left for the handlers, which
resolve them against the group and explain any correction.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from core.tools import ToolContractError
from core.tools._argument_repair import normalize_call_arguments
from core.tools._call_vocabulary import SpellingAliases, is_placeholder, spelling
from core.tools.contracts import ToolContract

from .agent_text import BOARD_FOREIGN_ACTIONS, INBOX_ONLY_RECEIVE, STATE_ONLY_STATUS

Json = dict[str, Any]

# Wrapper objects some harnesses put around the argument object.
_WRAPPERS = frozenset({"request", "arguments", "parameters", "params", "input", "args", "payload"})
# Idempotency keys are derived from the Tool Call; an echoed key requests nothing.
_DERIVED = frozenset({"requestid", "idempotencykey"})

_BOARD_FIELDS = SpellingAliases(
    {
        "text": ("body", "content", "message_text", "msg"),
        "title": ("subject", "discussion_title", "thread_title"),
        "message_id": ("post_id", "message_ids", "post_ids"),
        "reply_to": (
            "reply_to_id",
            "reply_to_post",
            "reply_to_post_id",
            "reply_to_message",
            "reply_to_message_id",
            "in_reply_to",
            "parent_id",
            "parent_post_id",
        ),
        "discussion_id": ("discussion", "thread_id", "channel_id", "topic_id"),
        "recipients": (
            "recipient",
            "to",
            "ping",
            "pings",
            "mention",
            "mentions",
            "notify",
            "participant_ids",
            "recipient_ids",
        ),
        "before": ("before_id", "before_post", "before_post_id", "before_message_id", "older_than"),
        "limit": ("count", "max", "max_results", "page_size", "limti"),
    }
)
_BOARD_ACTIONS = SpellingAliases(
    {
        "post": (
            "send",
            "send_message",
            "reply",
            "respond",
            "message",
            "write",
            "comment",
            "say",
            "publish",
            "post_message",
            "create_post",
            "add_post",
        ),
        "read": (
            "get",
            "view",
            "fetch",
            "show",
            "history",
            "messages",
            "posts",
            "read_posts",
            "read_messages",
            "read_discussion",
            "get_posts",
            "get_messages",
        ),
        "list": ("discussions", "list_discussions", "channels", "threads", "list_threads"),
        "create": (
            "create_discussion",
            "new_discussion",
            "start_discussion",
            "open_discussion",
            "create_thread",
            "new_thread",
        ),
        "join": ("subscribe", "follow", "join_discussion"),
        "leave": ("unsubscribe", "unfollow", "leave_discussion"),
    }
)
_BOARD_ELSEWHERE = {
    "status": "swarm_state",
    "state": "swarm_state",
    "roster": "swarm_state",
    "participants": "swarm_state",
    "members": "swarm_state",
    "inbox": "swarm_inbox",
    "receive": "swarm_inbox",
    "checkinbox": "swarm_inbox",
    "wiki": "swarm_wiki",
}

_WIKI_FIELDS = SpellingAliases(
    {
        "page_id": ("id", "page", "wiki_id", "wiki_page_id"),
        "content": ("body", "text", "markdown", "page_content"),
        "old_text": ("old", "old_string", "old_str", "find_text", "search_text"),
        "new_text": ("new", "new_string", "new_str", "replace_text", "replacement", "replace_with"),
        "query": ("q", "search", "search_query", "find", "keyword", "keywords", "term"),
        "expected_revision": ("base_revision", "expected_rev", "if_revision", "current_revision"),
        "revision": ("version", "rev", "revision_number"),
        "offset": ("start", "char_offset", "from"),
        "limit": ("count", "max", "max_chars", "max_results", "length", "page_size", "limti"),
        "include_deleted": ("show_deleted", "with_deleted", "deleted"),
    }
)
_WIKI_ACTIONS = SpellingAliases(
    {
        "list": (
            "search",
            "find",
            "grep",
            "lookup",
            "query",
            "pages",
            "list_pages",
            "search_pages",
            "search_wiki",
            "search_files",
            "find_pages",
            "browse",
            "index",
        ),
        "read": ("get", "view", "open", "fetch", "show", "read_page", "get_page", "cat"),
        "create": ("new", "add", "create_page", "new_page"),
        "update": (
            "edit",
            "modify",
            "change",
            "patch",
            "replace",
            "str_replace",
            "update_page",
            "edit_page",
        ),
        "delete": ("remove", "rm", "delete_page", "trash"),
        "history": ("revisions", "versions", "log", "list_revisions", "changes"),
        "restore": ("revert", "rollback", "undelete", "restore_revision"),
    }
)
# "latest" and "current" ask for whatever revision is current, which omission means.
_REVISION_PLACEHOLDERS = frozenset({"latest", "current", "head"})

_INBOX_FIELDS = SpellingAliases({"limit": ("count", "max", "max_results", "page_size", "limti")})
_INBOX_ACTIONS = frozenset(
    spelling(word)
    for word in (
        "receive",
        "get",
        "read",
        "check",
        "fetch",
        "poll",
        "pull",
        "list",
        "next",
        "messages",
        "inbox",
        "check_inbox",
        "read_inbox",
        "get_messages",
        "receive_messages",
    )
)
_STATE_FIELDS = SpellingAliases(
    {
        "limit": ("count", "max", "max_results", "page_size", "limti"),
        "cursor": ("next", "next_cursor", "page_token"),
    }
)
_STATE_ACTIONS = frozenset(
    spelling(word)
    for word in (
        "status",
        "state",
        "get",
        "show",
        "view",
        "list",
        "roster",
        "participants",
        "members",
        "who",
        "check",
        "info",
    )
)


def normalize_board(contract: ToolContract, arguments: Any) -> Any:
    """Repair a swarm_board call and infer an action that its fields imply."""

    value = _prepare(arguments)
    if not isinstance(value, dict):
        return value
    if "message" in value and "message" not in contract.input_schema["properties"]:
        # A post ID names a post to read; any other text is a message body.
        message = value.pop("message")
        is_post = isinstance(message, str) and message.strip().startswith("pst_")
        value["message_id" if is_post else "text"] = message
    normalized = _normalize(
        contract,
        value,
        fields=_BOARD_FIELDS,
        actions=_action_normalizer(_BOARD_ACTIONS, _BOARD_ELSEWHERE),
        optional=("discussion_id", "message_id", "reply_to", "before", "cursor", "limit"),
    )
    if not isinstance(normalized, dict):
        return normalized
    if "recipients" in normalized:
        recipients = _recipient_list(normalized["recipients"])
        if recipients:
            normalized["recipients"] = recipients
        else:
            del normalized["recipients"]
    if "action" not in normalized:
        implied = _implied_board_action(normalized)
        if implied is not None:
            normalized["action"] = implied
    return normalized


def normalize_wiki(contract: ToolContract, arguments: Any) -> Any:
    """Repair a swarm_wiki call and infer an action that its fields imply."""

    value = _prepare(arguments)
    if not isinstance(value, dict):
        return value
    normalized = _normalize(
        contract,
        value,
        fields=_WIKI_FIELDS,
        actions=_action_normalizer(_WIKI_ACTIONS, {}),
        optional=("page_id", "revision", "offset", "limit", "cursor", "include_deleted"),
    )
    if not isinstance(normalized, dict):
        return normalized
    expected = normalized.get("expected_revision")
    if is_placeholder(expected) or (
        isinstance(expected, str) and spelling(expected) in _REVISION_PLACEHOLDERS
    ):
        normalized.pop("expected_revision", None)
    if normalized.get("query") in (None, ""):
        normalized.pop("query", None)
    if "action" not in normalized:
        implied = _implied_wiki_action(normalized)
        if implied is not None:
            normalized["action"] = implied
    return normalized


def normalize_inbox(contract: ToolContract, arguments: Any) -> Any:
    """Repair a swarm_inbox call; its only action is receive."""

    value = _prepare(arguments)
    if not isinstance(value, dict):
        return value

    def action(item: Any) -> Any:
        if not isinstance(item, str) or is_placeholder(item):
            return item
        if spelling(item) in _INBOX_ACTIONS:
            return "receive"
        raise ToolContractError(INBOX_ONLY_RECEIVE.format(action=item))

    return _normalize(
        contract,
        value,
        fields=_INBOX_FIELDS,
        actions=action,
        optional=("limit",),
    )


def normalize_state(contract: ToolContract, arguments: Any) -> Any:
    """Repair a swarm_state call; every reading of its action is status."""

    value = _prepare(arguments)
    if not isinstance(value, dict):
        return value

    def action(item: Any) -> Any:
        if not isinstance(item, str) or is_placeholder(item):
            return item
        if spelling(item) in _STATE_ACTIONS:
            return "status"
        raise ToolContractError(STATE_ONLY_STATUS.format(action=item))

    return _normalize(
        contract,
        value,
        fields=_STATE_FIELDS,
        actions=action,
        optional=("cursor", "limit"),
    )


def _prepare(arguments: Any) -> Any:
    """Rename wrapper objects to the unwrapped spelling and drop derived keys."""

    if not isinstance(arguments, dict):
        return arguments
    value: Json = {}
    for key, item in arguments.items():
        name = spelling(key) if isinstance(key, str) else key
        if name in _DERIVED:
            continue
        if name in _WRAPPERS and isinstance(item, dict):
            nested = _prepare(item)
            for inner, inner_value in nested.items():
                if inner in value and value[inner] != inner_value:
                    raise ToolContractError(
                        f"Conflicting values for {inner}; provide one intended value."
                    )
                value[inner] = inner_value
            continue
        if key in value and value[key] != item:
            raise ToolContractError(f"Conflicting values for {key}; provide one intended value.")
        value[key] = item
    return value


def _normalize(
    contract: ToolContract,
    value: Json,
    *,
    fields: Mapping[str, str],
    actions: Callable[[Any], Any],
    optional: tuple[str, ...],
) -> Any:
    normalized = normalize_call_arguments(
        contract,
        value,
        enum_fields=("action",) if "action" in contract.input_schema["properties"] else (),
        field_aliases=fields,
        field_normalizers={"action": actions},
    )
    if not isinstance(normalized, dict):
        return normalized
    for field in optional:
        if field in normalized and is_placeholder(normalized[field]):
            del normalized[field]
    if "action" in normalized and is_placeholder(normalized["action"]):
        del normalized["action"]
    return normalized


def _action_normalizer(
    synonyms: Mapping[str, str], elsewhere: Mapping[str, str]
) -> Callable[[Any], Any]:
    def normalize(action: Any) -> Any:
        if not isinstance(action, str):
            return action
        if action in synonyms.values():
            return action
        if action in synonyms:
            return synonyms[action]
        target = elsewhere.get(spelling(action))
        if target is not None:
            raise ToolContractError(BOARD_FOREIGN_ACTIONS[target].format(action=action))
        return action

    return normalize


def _recipient_list(value: Any) -> Any:
    """Return recipients as a list of meaningful strings, splitting listed strings.

    Participant IDs and names contain no commas or semicolons, so either one
    separates recipients, also inside a list item such as ``["Ada, Bob"]``.
    """

    if is_placeholder(value):
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return value
    items: list[Any] = []
    for item in value:
        parts = item.replace(";", ",").split(",") if isinstance(item, str) else [item]
        items.extend(part.strip() if isinstance(part, str) else part for part in parts)
    return [item for item in items if not is_placeholder(item)]


def _implied_board_action(arguments: Json) -> str | None:
    if "title" in arguments:
        return "create"
    if {"text", "reply_to", "recipients"} & arguments.keys():
        return "post"
    if {"message_id", "before"} & arguments.keys():
        return "read"
    return None


def _implied_wiki_action(arguments: Json) -> str | None:
    if "old_text" in arguments or "new_text" in arguments:
        return "update"
    if "page_id" in arguments and ("content" in arguments or "title" in arguments):
        return "update"
    if "content" in arguments and "title" in arguments:
        return "create"
    if "query" in arguments:
        return "list"
    if "page_id" in arguments and not {"expected_revision"} & arguments.keys():
        return "read"
    return None


__all__ = ["normalize_board", "normalize_inbox", "normalize_state", "normalize_wiki"]
