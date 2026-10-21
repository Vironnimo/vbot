# Agent widgets (`show_widget`)

An Agent shows an interactive HTML widget inline in the Chat timeline (controls to tune a design, variants to choose from, a chart); values the user changes travel with their next message. The return path, not display alone, is the point: the user sets sliders and says "take it like this". User decisions of 2026-10-21: the return path below (composer chips, a quoted note before the User message, a visible row) and free HTML from the Agent, so it picks the fitting control per value.

## Interfaces

- Tool `show_widget` (`core/tools/show_widget.py`, registered in `core/runtime/_bootstrap.py`, in `PROJECT_DEFAULT_ALLOWED_TOOLS`, family `media`, on-demand summary): required `title` (whitespace collapsed, cut to 120 characters) and `widget_code` (HTML, at most 1 MiB UTF-8). Aliases: `html`, `code`, `content`, `widget_html`, `markup`, `body` -> `widget_code`; `name`, `label`, `heading` -> `title`. An empty title or code and oversized code are refused in the argument normalizer (`show_widget was not run: ...`, `invalid_arguments`). Outside a Session it fails with `session_required`, since nothing could keep the widget.
- Storage: the handler attaches `{title, widget_code}` as the call's Tool result payload under owner `tool:show_widget` (`builtin_payload_owner`, `sessions/runs.md` -> Tool result payloads), so the widget lives exactly as long as its Tool Result: deleted with it, inherited by forks, copied on materialization. The payload id is the widget id; Tool-call ids repeat within a Session and cannot serve.
- Display: `ToolContext.show_display_widget(id, title)` records an internal `widget_reference`; public projection (`server/file_delivery.py::project_message`) replaces it with `widget: {id, title, url}`, the URL minted per projection like media URLs. `widget_code` is hidden from the raw arguments and shown as a `content` detail block.
- Serving: `GET /api/widgets/{token}` (`server/_widget_routes.py`, capability domain `widget:` over the payload id, `server/browser-capabilities.md`). The route loads the payload by id from any Session (`tool_result_payload_by_id_async`) and answers `server/widget_document.py`'s document, or a 404 "no longer available" page.
- Return path: `chat.send`/`chat.stream` accept `widget_states: [{widget_id, state}]` (`server/rpc/validation.py::_optional_widget_states`: 1-8 entries, unique safe ids, each state a JSON object of at most 8 KiB compact JSON). `server/rpc/chat_methods.py` resolves each id through the Session's current view (`tool_result_payload_async`, title from the payload), drops ids it no longer shows (an edit removed the widget's Run), ignores the list for a command, and refuses it for a new Session. The remaining widgets become the Run request's `context_note`: `core/chat/widget_states.py::widget_state_note` builds one note with a `widget_states` display record (`WidgetState(widget_id, title, state)`, table `note_widget_states`), stored right before the User message. A request with a context note is not steerable, so a send during an active Run queues and keeps the note.
- History and live: the note shows as public role `widget_state` (`server/chat-rpcs.md`), timeline item `widget_state` (`chat/timeline.md`), live event `widget_state_recorded`; the TUI renders "Widget values sent". Tests: `tests/core/tools/test_show_widget.py`, `tests/server/test_widgets.py`, plus the record, storage, timeline and History cases next to goal and Sub-Agent rows.

## Widget document and bridge

The server wraps the stored HTML on every request, so all widgets get the current base styles and bridge: a fragment becomes `<!doctype html><html><head>...</head><body>FRAGMENT</body></html>`; a fragment that is already a document gets both inserted after its `<head>` or `<html>` tag. The base stylesheet matches the dark theme and defines `--bg`, `--surface`, `--surface-2`, `--text`, `--muted`, `--border`, `--accent`.

Headers: the shared active-document CSP with `sandbox allow-scripts` (no downloads), HTTPS scripts, styles, fonts and images, `connect-src 'none'`, `form-action 'none'`, plus `frame-ancestors 'self'`; `no-store`, `nosniff`, `no-referrer`.

Bridge protocol (plain objects; the parent posts only to the frame's window, the frame only to `window.parent`):
- frame -> parent `{type: "vbot-widget-hello"}` once parsed, repeated every 500 ms (at most 20 times) until an init arrives; only the first init counts.
- parent -> frame `{type: "vbot-widget-init", nonce, state}`: `state` is the stored state to restore or `null`. Named fields get their values and bubbling `input`/`change` events; other keys become custom state; `window` receives a `vbot-restore` event with `detail: state`.
- frame -> parent `{type: "vbot-widget-state", nonce, state, initial}`: one flat object of every named `input`/`select`/`textarea` (range and number as numbers, a single checkbox as boolean, several of one name as the checked values, radio as the checked value, multi-select as an array; buttons, files and passwords skipped) merged with keys passed to `vbot.setState({...})`, which win. `initial` only for the first report; later ones follow `input`/`change` and `setState`, one per animation frame.
- frame -> parent `{type: "vbot-widget-size", nonce, height}` on every height change.
- The WebUI side (validation, nonce, store, chips) is in `webui/chat.md` -> Agent widgets.

## Constraints & Gotchas

- Widget HTML is Agent-authored code and its state comes from the user's browser: titles and values reach the Model only quoted with `quote_external_value` under kernel-authored framing, never as raw text.
- Values become durable only when sent. Before that they live in the browser's origin storage, so another device does not see unsent changes.
- Capability URLs are process-local; after a restart History projection mints new ones and the WebUI follows a new URL only until the frame has connected.
- Channels and the TUI show no widget. The Tool result says so for those reply surfaces, so the Agent can tell the user to open the Session in the app.
- Not built: opening a widget in the second Chat area, updating a widget in place, widgets reading live data, Portals (a dev-server proxy), widgets on Channels.

## Agent-facing text

Reviewed with `vbot-agent-tools` and `vbot-agent-text` on 2026-10-21.

| Text | Reason |
|---|---|
| Description: `Show an interactive HTML widget inline in the user's chat, such as controls to tune a design, variants to choose from, or a chart.` | Names the job and the typical uses, so Agents reach for it when the user wants to try values or compare variants, instead of describing options in prose. |
| `It runs in a sandboxed frame without network access; scripts, styles, fonts and images load only from https URLs.` | Without it Agents write `fetch` calls or relative asset paths that cannot work (F2). |
| `title`: `Short label shown above the widget.` | It is also the name the Agent reads when values come back, so it identifies the widget after many turns or a Compaction. |
| `widget_code`: `HTML for the widget's body.` | Says a fragment is enough; a whole document also works. |
| `Plain form controls already match the app's dark theme; the CSS variables --bg, --surface, --text, --muted, --border and --accent are available.` | Saves Agents from writing a full stylesheet and keeps widgets in the app's look. |
| `When the user changes the widget and sends a message, you receive with it the values of all form fields that have a name attribute, plus what the widget passed to vbot.setState({key: value}), such as a chosen variant.` | The only way an Agent learns which state comes back; without it, unnamed controls send nothing and variant pickers report no choice. |
| Summary: `Show an interactive widget, such as sliders or a chart, in the chat.` | Decides loading on demand; `sliders` names the most common use. |
| Result: `Showed the widget "<title>" in the chat. When the user changes it and sends a message, its values arrive with that message.` | Tells the Agent the turn is done and that it need not ask for the values or poll for them. |
| Result addition: `The user is using <Telegram / the terminal>, where widgets do not show; they see it when they open this Session in the vBot app.` | Without it the Agent assumes the user sees the widget on a surface that cannot show it. |
| `session_required`: `Nothing was shown. Widgets show in a chat Session, and this call runs outside one.` | States the outcome and why, so the Agent stops retrying. |
| Refusals: `show_widget was not run: title is empty; pass a short label.` / `widget_code is empty; pass the HTML.` / `widget_code has <n> KiB; the limit is 1024 KiB. Shorten the HTML, such as by building repeated elements with a script.` | Name the field and the fix; the size refusal offers the usual way to shrink generated markup. |
| Note (`core/chat/widget_states.py`): `The user sent the current values of widgets you showed along with the message that follows. Each line is a widget's title and its values, as JSON:` then `"<title>": {<values>}` per widget | Labels the values as data the user sent with the next message, from widgets the Agent made, so the user's words ("take it like this") resolve to them; quoting keeps widget-supplied text from passing as instructions. |
