---
name: browser-use
description: "Complete website tasks with the browser Tool: navigate, read, fill forms, handle tabs and dialogs, and retrieve files."
---

# Browser Use

Use the browser Tool to carry out the user's website task and verify the result. Required browser components are prepared automatically on first use. Use the browser Tool for browser control; its permissions apply throughout the task.

## Working loop

1. Start with `open` and the task's URL. To work in an already connected, logged-in page, use `tabs` and select the matching id with `switch_tab`.
2. Read the returned snapshot. Use its exact element refs for interaction. After page changes, obtain fresh refs with `snapshot` or request `observe: true` on the action. Refs from an earlier Run are no longer valid.
3. Fill related text and selection fields together in one `fill` call. Use `{target, text}` for text inputs and `{target, text, kind: "select"}` for option values in the `fields` array. An empty text clears a text input. Use `press` for a key combination. Request one observation after the group, then use the new ref to submit.
4. Verify the requested outcome using visible confirmation or page content. Continue until the task is complete or a concrete blocker remains; opening a page alone does not complete a task that asks for an action.

## Reading and visual work

Use `read` for page text and follow `next_offset` when more is needed. A targeted snapshot with `selector` keeps large pages manageable; `full: true` includes noninteractive content. Use `screenshot` when layout or visual content matters. The image is returned directly. Use `scroll` to reveal more content and `wait` for expected visible text.

## Tabs, dialogs, and files

Use ids returned by `tabs` when switching or closing tabs. Use `new_tab` for separate work. Handle a page dialog with `dialog`; accept only when appropriate to the task.

For downloads, activate the website's download control and use `downloads` to obtain completed file paths. For uploads, pass absolute paths on the browser's computer to `upload`. In a connected browser, downloads remain on that computer.

## Recovery and completion

If refs are stale, take a new snapshot and continue. If input failed or its effect is uncertain, inspect the page before repeating it. A partial fill reports how many fields completed; continue from the observed state. An observation error does not mean the preceding action failed.

Use existing logins when available. If the site requires a user-only login or Chrome displays a connection-consent dialog, identify that specific blocker. Summarize the verified result and provide relevant downloaded files. Use `close` when the browser connection is no longer needed; it disconnects a user-owned browser and closes a browser started for this Session.
