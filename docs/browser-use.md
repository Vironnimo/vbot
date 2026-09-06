# Browser Use

Browser Use is a bundled vBot Extension that gives selected Agents the `browser` Tool. It replaces the old `browser-use` Skill. No Chrome, Edge, or other browser add-on is required.

## Enable it

1. Install [agent-browser](https://agent-browser.dev/installation) version **0.36.0 or newer** on the computer running the vBot server, with its executable on the server's PATH. For the version verified here: `npm install -g agent-browser@0.36.0`.
2. For a managed browser, install Chrome/Chromium on that computer or run `agent-browser install`. On Linux ARM64, use the distribution's Chromium package; the backend discovers `chromium` or `chromium-browser` on PATH. See the official [Chrome discovery rules](https://agent-browser.dev/engines/chrome).
3. Reload Extensions in vBot after installing the executable. Enable **Browser Use** and choose a connection mode in its settings.
4. Explicitly grant the **browser** Tool to each Agent that should use it. An Agent with all ordinary Tools does not automatically receive Browser Use; a Project whitelist also does not grant it. Existing Skill grants are not converted into browser Tool grants.

The Tool's permission controls access through vBot. An Agent with unrestricted host Bash access can independently launch host programs; Extension grants do not sandbox Bash.

## Choose a connection

| Mode | Browser used | Logins and files |
| --- | --- | --- |
| `managed` (default) | vBot starts its own Chrome/Chromium on the server. The optional **Show managed browser** setting displays its window there. | Starts with a fresh profile; cookies remain while the connection is alive. Downloads are available to vBot. |
| `existing` | Connects to a running local Chrome with its debugging access enabled. | Uses the user's existing logins and available tabs. Downloads stay on that computer. |
| `remote` | Connects to the **Remote CDP URL** configured in Extension settings. | Uses the profile and logins of that browser. Upload paths and downloads belong to its computer. |

The connection mode is an Extension setting shared by Agents. It is not a Tool argument the Agent can override. The remote endpoint is stored through vBot's secret settings and may be an HTTP(S) discovery endpoint or a WebSocket CDP URL.

### Use an already logged-in Chrome

For Chrome 144 or newer, open `chrome://inspect/#remote-debugging` in the Chrome you want to share and enable remote debugging. Select `existing` in vBot, then approve Chrome's connection dialog when the Agent first uses the Tool. The backend connects to that browser without restarting it or copying its profile. This is Chrome's built-in consent flow; see [Google's setup guide](https://developer.chrome.com/blog/chrome-devtools-mcp-debug-your-browser-session?hl=en) and [agent-browser CDP mode](https://agent-browser.dev/cdp-mode).

The connection initially uses a dedicated tab. The Agent can list tabs and select an existing one when the task requires it. Closing the connection leaves the user's browser and tabs running. Explicitly closing a tab still closes that tab.

CDP support here targets Chromium-based browsers. The Chrome consent flow above is specific to Chrome; it does not establish equivalent Firefox or Safari support. A compatible CDP endpoint can also be configured directly in `remote` mode.

### Server and browser on different computers

`existing` discovers a browser on the **server computer**. Opening the vBot Desktop Client on Windows does not move browser execution there when the server runs on a Pi.

To use Windows Chrome from such a server, provide a CDP endpoint reachable by the server in `remote` mode, for example through an authenticated browser service or a private tunnel to a browser configured for CDP. Treat this endpoint as access to the browser's logged-in accounts. Chrome's local consent discovery is not automatically forwarded over the network; this Extension does not add a Desktop Client browser bridge.

## How the Tool works

The Agent starts with `open` or `tabs`. Navigation returns a compact snapshot with element refs. Refs belong to the current snapshot and Run; the Agent refreshes the snapshot after page changes. `read` retrieves bounded, paginated page text. `screenshot` returns an image directly to the Model and a preview to the UI, without a separate file-reading Tool call.

The Agent can fill several fields with one `fill` call. Every field is validated before input begins; if a later field fails, earlier completed fields are reported and remaining fields are skipped. Input operations do not automatically return another snapshot unless `observe` is requested. This avoids repeating the whole form after each field. Errors never silently replay input.

Managed downloads use the website's normal download controls, followed by `downloads` to list completed files and their paths. Files still downloading are omitted. For attached browsers, downloads remain on that browser's computer. Upload paths must likewise refer to files on the browser's computer.

Each Project/Agent/Session owns a separate backend connection. It stays alive across Runs, with a 15-minute idle timeout. Closing a managed connection ends its browser and temporary profile; closing an attached connection disconnects vBot. Extension shutdown also performs cleanup. Browser authentication is not persisted as vBot Session data, and there is no automatic profile migration.

## Verification and measured efficiency

Verified on Windows with `agent-browser 0.36.0` on 2026-09-06:

- 70 of 70 structural Model calls using `gpt-5.6-luna` produced the expected Tool arguments, covering every action, optional fields, and deliberately malformed inputs. These probes did not execute browser actions; runtime tests separately validate their results.
- A deterministic local website exercised 38 successful real-browser Tool calls, including forms, dialogs, uploads, downloads, screenshots, navigation, tab selection, and remote CDP attachment. Disconnecting the attached connection preserved the original browser and its page.
- In five warm-browser trials on the same three-field form and backend, the old wrapper required three Agent calls and six backend commands; the Extension required one Agent call and five backend commands, including tab verification and a final snapshot. Median returned JSON size was **765 vs 278 tokens** with `o200k_base` (64% fewer). Median local execution time was **169 vs 132 ms**. Initial navigation, Tool definitions, and Model/network latency were excluded; this is not an end-to-end task-speed claim.

The automatic suite covers permissions, stale refs, selection drift, cancellation, partial failures, bounded media, and cleanup. The private-browser Chrome consent dialog, Linux ARM64 runtime, and a real cross-machine connection were not exercised locally; setup guidance for those follows the upstream documentation.

The existing generic Extension settings and Agent Tool-grant UI provide configuration; no frontend changes or browser add-on are involved. See [Extension development](extensions.md) for the shared lifecycle and permission mechanisms.
