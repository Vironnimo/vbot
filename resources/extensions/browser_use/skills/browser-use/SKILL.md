---
name: browser-use
description: "Complete website tasks with the browser Tool: navigate, read, fill forms, handle tabs and dialogs, retrieve files, and debug console, network, JavaScript, and performance problems."
---

# Browser Use

Use the browser Tool to carry out the user's website task and verify the result. Required browser components are prepared automatically on first use. Use the browser Tool for browser control; its permissions apply throughout the task.

## Working loop

1. Start with `open` and the task's URL. To work in an already connected, logged-in page, use `tabs` and select the matching id with `switch_tab`.
2. Read the returned snapshot. Use its exact element refs for interaction. After page changes, obtain fresh refs with `snapshot` or request `observe: true` on the action. Refs from an earlier Run are no longer valid.
3. Fill related text and selection fields together in one `fill` call. Use `{target, text}` for text inputs and `{target, text, kind: "select"}` for option values in the `fields` array. An empty text clears a text input. Use `press` for a key combination. Request one observation after the group when you need a new target ref or confirmation. You can submit a focused field with `press` without an intervening snapshot. Limit requested observations with `selector` and `limit` when only one page section matters.
4. Verify the requested outcome using visible confirmation or page content. Continue until the task is complete or a concrete blocker remains; opening a page alone does not complete a task that asks for an action.

## Reading and visual work

Use `read` for page text and follow `next_offset` when more is needed. A targeted snapshot with `selector` keeps large pages manageable; `full: true` includes noninteractive content. Use `screenshot` when layout or visual content matters. The image is returned directly. Use `scroll` to reveal more content and `wait` for expected visible text.

## Tabs, dialogs, and files

Use ids returned by `tabs` when switching or closing tabs. Use `new_tab` for separate work. Handle a page dialog with `dialog`; accept only when appropriate to the task.

For downloads, activate the website's download control and use `downloads` to obtain completed file paths. For uploads, pass absolute paths on the browser's computer to `upload`. In a connected browser, downloads remain on that computer.

## Recovery and completion

If refs are stale, take a new snapshot and continue. If input failed or its effect is uncertain, inspect the page before repeating it. A partial fill reports how many fields completed; continue from the observed state. An observation error does not mean the preceding action failed.

After submitting or navigating, check the returned URL and content before another input. If the page is still changing, use `wait` with expected visible `text` or an exact destination `url`; omit both when no specific condition is known. Wait returns a fresh observation. A quiet page or a completed input command does not prove that a form was submitted or the task succeeded.

Use `status` to inspect connection mode and `headed` without starting a browser. A managed browser window appears on the vBot server computer, which may differ from the user's computer. If a visible managed browser is needed, enable Show managed browser in the Browser Use Extension settings and reconnect; closing a managed connection discards its temporary browser profile. Attached browsers use their existing host and profile.

When a website shows an error, inspect its text and the reported HTTP status when available. Report that evidence; do not invent a cause such as automation detection. Avoid repeatedly submitting the same request to an unchanged error page. Preserve the user's requested website and method; report the concrete blocker when that required route remains unavailable.

Use existing logins when available. If the site requires a user-only login or Chrome displays a connection-consent dialog, identify that specific blocker. Summarize the verified result and provide relevant downloaded files. Use `close` when the browser connection is no longer needed; it disconnects a user-owned browser and closes a browser started for this Session.

## Debugging a website

1. Open the page and reproduce the problem once. Request capture starts before the first navigation. Use `console`, `errors`, and `requests` to inspect evidence; narrow noisy output with `filter`. Logs describe captured activity across this connection's tabs, not only the visible page.
2. Copy a `requestId` from `requests` into `request` as `request_id` to inspect headers, status, posted data, and an available response body. A missing body means it was unavailable, not empty. Use `har_start` before a reproduction and `har_stop` afterward when you need an HTTP archive with captured text bodies.
3. Large diagnostics return JSON text with `result_id`, `next_offset`, and a server file path. Continue with `result`, passing that id and offset. This reads the same captured output; never repeat `eval` or another mutating operation just to retrieve its remaining output. The newest eight large outputs remain available until the connection closes.
4. For performance work, call `trace_start`, reproduce the slowdown, then `trace_stop`. The returned JSON is a Chromium performance trace for Chrome DevTools or Perfetto. It is not a Playwright Trace Viewer archive with DOM snapshots and an action timeline. Trace capture is available only in a managed browser because it can record browser-wide activity. HAR start/stop also requires a managed browser. Save screenshots separately for visual evidence. Stop recordings before closing the connection.
5. Inspect a blocking JavaScript dialog with `dialog_status`, then use `dialog` to accept or dismiss it as appropriate. An action can complete before its requested snapshot fails because a dialog opened; resolve the dialog without replaying the action.

## JavaScript, mocks, and state

Use `eval` with `script` for DOM attributes, computed styles, page data, localStorage/sessionStorage, or browser APIs. It executes once in the selected page and awaits promises. For example, `{"action":"eval","script":"({title: document.title, theme: localStorage.getItem('theme')})"}` returns JSON data. For several statements use an async IIFE such as `(async () => { const r = await fetch('/api/items'); return {status: r.status, data: await r.json()}; })()`. This is page JavaScript: `page`, Playwright locators, Node imports, and server filesystem APIs are unavailable. A thrown script may already have changed the page; inspect before retrying. Eval invalidates element refs even when used only to read.

For request mocking, first open a blank tab with `new_tab`. Install `route` with `pattern` and either a JSON `body` or `abort:true`, then navigate to the test page. For example, `{"action":"route","pattern":"**/api/items","body":"{\"items\":[]}"}` supplies an HTTP 200 JSON response. Remove a route with `unroute` and the same pattern, or omit the pattern to remove all routes installed through this connection. Routes apply across this connection's tabs; mock only as part of the user's test and remove mocks when finished. Custom response status/headers, conditional interception, and a Playwright test runner are not exposed by this Tool.

Use `cookies` to inspect browser cookies. Use `state_save` to export cookies and localStorage to a server JSON file, and `state_load` with its absolute `path` to restore it. State import and cookie inspection are available only in a managed browser because they span the browser context. Treat saved authentication state as sensitive. Use page JavaScript for individual localStorage/sessionStorage keys. SessionStorage is not part of the state export.

Use `check`/`uncheck` for an explicit checkbox state, `dblclick` for a double click, `drag` with source `target` and `destination` refs, and `type` to insert text into the focused element. Use `resize` with `width` and `height` for viewport checks, and `pdf` for a PDF file on the server. Request `observe:true` when the next step needs new refs. This Tool controls Chromium; Firefox/WebKit runs, Playwright test generation, interactive locator picking, and video recording require a separate testing setup.
