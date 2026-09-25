"""Agent-facing Wiki capability, shared with management and probes."""

from typing import Any

WIKI_DESCRIPTION = (
    "Create, find, read, and edit your group's shared Markdown Wiki pages. Every participant "
    "can read and change every page. Each change saves a revision; history lists them and "
    "restore brings one back, also for a deleted page. Wiki changes send no Board messages: "
    "post a page's link on the Board when others should notice it."
)

WIKI_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["list", "read", "create", "update", "delete", "history", "restore"],
            "description": (
                "list finds pages, history lists a page's revisions, and restore brings "
                "back one of them."
            ),
        },
        "page_id": {
            "type": "string",
            "description": "Page ID, such as wpg_abc123. Required except for list and create.",
        },
        "title": {
            "type": "string",
            "description": "Page title. Required for create; with update, renames the page.",
        },
        "content": {
            "type": "string",
            "description": (
                "The page's complete Markdown. Required for create; with update, replaces "
                "the whole page and needs expected_revision."
            ),
        },
        "old_text": {
            "type": "string",
            "description": (
                "With update: the passage to replace, copied from the current page with "
                "enough surrounding text to occur only once."
            ),
        },
        "new_text": {
            "type": "string",
            "description": "With update: the text replacing old_text; an empty string removes it.",
        },
        "expected_revision": {
            "type": "integer",
            "description": (
                "Revision your change is based on; required with content. A passage edit "
                "also applies to a newer revision while old_text still matches."
            ),
        },
        "revision": {
            "type": "integer",
            "description": "Revision to read or to restore. Omit to read the current revision.",
        },
        "query": {
            "type": "string",
            "description": "With list: text to find in titles and content, ignoring case.",
        },
        "include_deleted": {
            "type": "boolean",
            "description": "With list: also show deleted pages.",
        },
        "offset": {
            "type": "integer",
            "description": "With read: character position to start from.",
        },
        "limit": {
            "type": "integer",
            "description": (
                "With read: characters to show (default 12000, at most 20000). With list or "
                "history: entries (default 20, at most 100)."
            ),
        },
        "cursor": {
            "type": "string",
            "description": "Continuation from a previous list or history result.",
        },
    },
    "required": ["action"],
}

# Management operation failures, shown by the Swarm page.
WIKI_ERRORS = {
    "wiki_page_not_found": "This page is not in the Swarm's Wiki.",
    "wiki_revision_not_found": "That revision does not exist for this page.",
    "wiki_deleted": "This page is deleted. Restore a saved revision to recover it.",
    "wiki_revision_conflict": (
        "The page changed since the revision you edited, and your change could not be "
        "applied to the current revision. Nothing changed. Reload the page and retry."
    ),
    "wiki_edit_conflict": (
        "The passage to replace was not found exactly once in the page. Nothing changed."
    ),
}

# Results.
WIKI_STATUS = {
    "created": "Created.",
    "passage": "Replaced the passage at line {line}.",
    "content": "Replaced the whole content.",
    "title": "Renamed the page.",
    "deleted": "Deleted. Restore it with {call}",
    "restored": "Restored revision {revision}.",
    "unchanged": "Nothing changed: the page already holds this change.",
    "unchanged_create": "Nothing changed: an identical page already exists.",
    "unchanged_delete": "Nothing changed: the page is already deleted.",
}
WIKI_REVISION_CURRENT = "{revision} (current), saved by {author}"
WIKI_REVISION_OLDER = "{revision} (the current revision is {current}), saved by {author}"
WIKI_READ_DELETED = "This revision deleted the page. Restore it with {call}"
WIKI_READ_SHOWN = "Characters {start} to {end} of {total}."
WIKI_READ_EMPTY = "This revision has no content."
WIKI_READ_PAST_END = "Nothing to show after character {offset}; the page has {total} characters."
WIKI_READ_MORE = "Continue with {call}"
WIKI_LIST_PAGES = "Pages, most recently changed first ({count} shown)."
WIKI_LIST_MATCHES = 'Pages containing "{query}", most recently changed first ({count} shown).'
WIKI_LIST_NONE = "The Wiki has no pages yet."
WIKI_LIST_ONLY_DELETED = "The Wiki has only deleted pages. List them with {call}"
WIKI_LIST_NO_MATCH = 'No page contains "{query}".'
WIKI_LIST_MORE = "More pages exist. Continue with {call}"
WIKI_LIST_ENTRY = '- "{title}" ({page_id}), revision {revision} by {author}{deleted}'
WIKI_ENTRY_DELETED = ", deleted"
WIKI_HISTORY = 'Revisions of "{title}" ({page_id}), newest first ({count} shown).'
WIKI_HISTORY_MORE = "Older revisions exist. Continue with {call}"
WIKI_HISTORY_ENTRY = "- revision {revision}, {time}, by {author}{title}{deleted}"
WIKI_HISTORY_TITLE = ', titled "{title}"'
WIKI_HISTORY_DELETED = ", deleted the page"

# Notes on calls that ran after a repair.
WIKI_LISTED_INSTEAD = "read needs page_id, so this lists the pages instead."
WIKI_PAGE_CLOSE = 'page_id {value} matched no page; used {page_id} ("{title}").'
WIKI_PAGE_TITLE = "page_id {value} is a page title; used {page_id}."
WIKI_CREATE_IGNORED_ID = "create makes a new page, so page_id {value} was ignored."
WIKI_TITLE_FROM_HEADING = 'create needs a title; used the first heading, "{title}".'

# Failures.
NOTHING_CHANGED = "Nothing changed."
WIKI_PAGE_NOT_FOUND = "No page {value} exists in your group's Wiki."
WIKI_PAGE_SUGGESTION = 'The closest page is "{title}" ({page_id}).'
WIKI_PAGE_RETRY = 'Repeat the call with page_id "{page_id}" if you meant it.'
WIKI_PAGE_FIND = 'Find pages with {"action": "list"}.'
WIKI_CREATE_EXISTING = (
    'create makes a new page, but page_id names the existing page "{title}" ({page_id}). '
    "To change that page, use update; to add a separate page, omit page_id."
)
WIKI_NEEDS = {
    "page_id": '{action} needs page_id. Find pages with {{"action": "list"}}.',
    "change": (
        "update needs old_text and new_text to replace one passage, content to replace the "
        "whole page, or title to rename it."
    ),
    "old_text": (
        "new_text replaces old_text, which is missing. Add old_text: the passage of the "
        "current page to replace. To insert, include neighboring text in both old_text and "
        "new_text. To replace the whole page, send content instead."
    ),
    "new_text": "old_text needs new_text: its replacement, or an empty string to remove it.",
    "one_change": (
        "Send either old_text with new_text to change one passage, or content to replace the "
        "whole page, not both."
    ),
    "expected_revision": (
        "Replacing the whole content needs expected_revision: the revision your content is "
        "based on. The current revision is {current}; read it first if you have not, so no "
        "newer change is lost."
    ),
    "content": "create needs content: the page's Markdown.",
    "title": "create needs a title.",
    "revision": "restore needs revision: the saved revision to bring back. See them with {call}.",
}
WIKI_FIELD_ELSEWHERE = "{field} is used by {users}, not by {action}. Repeat the call without it."
WIKI_FIELD_UNKNOWN = "swarm_wiki has no field {field}. Repeat the call without it."
WIKI_READ_QUERY = (
    "read shows one page and has no query. To search pages, use {call}; to read the page, "
    "repeat the call without query."
)
WIKI_NOT_FOUND = "old_text does not occur in the current page (revision {current})."
WIKI_NOT_FOUND_STALE = (
    "The page changed since revision {expected} (now {current}), and old_text no longer "
    "occurs in it."
)
WIKI_CLOSEST = "Closest passage, at line {line}:"
WIKI_CLOSEST_SEVERAL = "Closest passages:"
WIKI_PASSAGE_LINE = "line {line}:"
WIKI_COPY_EXACTLY = "Copy old_text exactly from the page, or read it with {call}."
WIKI_AMBIGUOUS = (
    "old_text occurs {count} times, {where}. Include neighboring text so it occurs only once."
)
WIKI_AT_LINES = "at lines {lines}"
WIKI_IN_LINE = "all in line {line}"
WIKI_MORE_LINES = "{count} more lines"
WIKI_TRUNCATED = "[passage shortened]"
WIKI_CONFLICT = "The page changed since revision {expected}; the current revision is {current}."
WIKI_CONFLICT_FUTURE = "Revision {expected} does not exist yet; the current revision is {current}."
WIKI_CONFLICT_DIFF = "Changes since revision {expected}:"
WIKI_CONFLICT_DIFF_CUT = "[changes shortened; read the page for the rest]"
WIKI_CONFLICT_CONTENT = (
    "Apply your change to the current page: replace one passage with old_text and new_text, "
    "or send the whole content with expected_revision {current}."
)
WIKI_CONFLICT_RETRY = "Repeat the call with expected_revision {current} if it still applies."
WIKI_DELETED = "The page is deleted (revision {current}). Restore it first with {call}."
WIKI_REVISION_MISSING = (
    "Revision {revision} does not exist; the page has revisions 1 to {current}. "
    "See them with {call}."
)
WIKI_CURSOR = (
    "This cursor only continues the call that returned it, with the same other arguments. "
    "Omit cursor to start from the first page."
)
