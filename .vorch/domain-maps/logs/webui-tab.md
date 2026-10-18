# WebUI Logs Tab

Supplementary to `logs.md`. Covers the WebUI Logs tab's bounded entry window, paging and mounting (`webui/src/lib/logsView.js`) and the reconnect of its log stream (`webui/src/components/LogsView.svelte`). Its look is `webui/design.md`.

## Bounded window

The WebUI keeps a bounded window. `logsView.js` holds at most `LOGS_WINDOW_MAX_ENTRIES = 5000` entries in a `LogEntryWindow` class instance, which Svelte does not deep-proxy; every change bumps `viewState.revision`, which the view's deriveds read. Appends update the entries, the filtered `visible` list and level counts in place; only filter changes refilter. The view mounts visible entries from `renderFrom` on: the newest `LOGS_RENDER_STEP = 250` at first, plus live arrivals; "Load older entries" mounts 250 more loaded rows before it reads the page before `nextBefore`. Live appends past 5000 + 250 drop the oldest entries back to 5000 and move `nextBefore`; loading older stops at 5000. Older-page results for a replaced, trimmed or switched window are dropped.

## Reconnect

A WebUI log-stream reconnect reloads the catalog before re-reading and re-subscribing, preserving a still-valid selection and discovering files created while disconnected. A catalog or file-read failure during that recovery keeps the view in `reconnecting` and schedules the next bounded-backoff attempt; only selection change, teardown, or a successful replacement stream ends that retry chain.
