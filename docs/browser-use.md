# Browser Use

Browser Use is a bundled vBot Extension that gives selected Agents the `browser` Tool and includes a `browser-use` Skill explaining its workflow. The Extension replaces the former standalone Skill and owns browser execution and dependency preparation. No Chrome, Edge, or other browser add-on is required.

## Enable it

1. Enable **Browser Use** in Extensions. The default connection mode starts a managed browser.
2. Explicitly grant the **browser** Tool to each Agent that should use it. An Agent with all ordinary Tools does not automatically receive Browser Use; a Project whitelist also does not grant it. Existing Skill grants are not converted into browser Tool grants.
3. Give the Agent a website task. Its Extension-provided Skill explains navigation, observations, forms, files, and recovery. Normal Skill visibility settings still apply when an Agent uses a restricted Skill list.

The first opening operation automatically prepares the native client and, in managed mode, a browser. Subsequent operations and Extension reloads reuse the installation. The user does not need to install `agent-browser`, Node, npm, or a separate Skill, or run a setup command. Loading the Extension itself does not perform network access; an authorized Tool call triggers preparation.

The Tool's permission controls access through vBot. An Agent with unrestricted host Bash access can independently launch host programs; Extension grants do not sandbox Bash.

## Automatic preparation

The Extension downloads the pinned native **agent-browser 0.36.0** release, checks its SHA-256 against the bundled release digest, and validates its version before execution. The client lives under `<data-dir>/artifacts/browser-use/0.36.0/`; global executables and npm shims are not used. First use requires access to GitHub Releases and, if a browser is needed, the browser download or distribution package servers. See the upstream [native installation](https://agent-browser.dev/installation) and [Chrome engine](https://agent-browser.dev/engines/chrome) documentation.

On Windows and macOS, the client prepares Chrome for Testing in its standard `~/.agent-browser/browsers` cache. On Linux, an existing distribution Chromium/Chrome is reused. The Debian/Raspberry Pi OS server Installer includes Chromium and its shared libraries; an existing apt-based installation can also prepare missing Chromium automatically when the server has noninteractive package-install rights. Other Linux ARM64 hosts require a working system Chromium because this backend's Chrome for Testing download does not support that platform. A desktop-only client installation does not install a server browser.

Setup is serialized across concurrent calls and processes, including the shared browser cache. Permission revocation, Extension shutdown, and cancellation stop preparation before browser input can begin. Failed preparation reports a bounded setup stage and permits one retry, then avoids repeatedly downloading until the Extension is reloaded. Attached-browser modes prepare only the client.

The implementation was compared with the local Hermes Agent snapshot `165c889e5`, particularly `tools/browser_tool.py` and its Chromium setup tests. It adopts lazy preparation, cached reuse, bounded failure retries, and cancellation-aware setup. vBot uses a pinned, verified native client instead of Hermes's npx fallback and keeps the Tool grant as the execution boundary.

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

The Agent can fill text inputs and selection fields together with one `fill` call. Fields use `{target, text}` for text inputs or `{target, text, kind: "select"}` for option values. Every field is validated before input begins; if a later field fails, earlier completed fields are reported and remaining fields are skipped. Input operations do not automatically return another snapshot unless `observe` is requested. This avoids repeating the whole form after each field. Errors never silently replay input. Invalid argument errors identify the affected field or action's accepted fields and the valid correction, without echoing form values.

Element refs use short numeric labels such as `r12`. They remain valid only in their owning Session's current snapshot and Run. A durable counter reserves numbers across concurrent services and Extension reloads, preventing an old label from referring to a newly observed element. The counter under `<data-dir>/artifacts/browser-use/refs.db` belongs to the Extension and should not be removed during routine cache cleanup.

Managed downloads use the website's normal download controls, followed by `downloads` to list completed files and their paths. Files still downloading are omitted. For attached browsers, downloads remain on that browser's computer. Upload paths must likewise refer to files on the browser's computer.

Each Project/Agent/Session owns a separate backend connection. It stays alive across Runs, with a 15-minute idle timeout. Closing a managed connection ends its browser and temporary profile; closing an attached connection disconnects vBot. Extension shutdown also performs cleanup. Browser authentication is not persisted as vBot Session data, and there is no automatic profile migration.

## Verification and measured efficiency

Verified on Windows with `agent-browser 0.36.0` on 2026-09-06:

- 70 of 70 structural Model calls using `gpt-5.6-luna` produced the expected Tool arguments, covering every action, optional fields, and deliberately malformed inputs. These probes did not execute browser actions; runtime tests separately validate their results.
- A deterministic local website exercised 38 successful real-browser Tool calls, including forms, dialogs, uploads, downloads, screenshots, navigation, tab selection, and remote CDP attachment. Disconnecting the attached connection preserved the original browser and its page.
- A free-form website task through `gpt-5.6-luna` activated the bundled Skill, filled and submitted a registration, downloaded its confirmation, read that file, and returned the correct reference. The final run used eight successful Tool calls with no failed calls, including Skill activation and file reading. The fixture independently checked exactly one correct submission and the downloaded bytes. The native client was prepared automatically in a fresh data directory; the host Chrome cache was reused. An earlier cold-host check also downloaded Chrome automatically. This is one successful workflow observation, not a guarantee that every Model run avoids recovery.
- In five warm-browser trials on the same three-field form and backend, the old wrapper required three Agent calls and six backend commands; the Extension required one Agent call and five backend commands, including tab verification and a final snapshot. Median returned JSON size was **765 vs 278 tokens** with `o200k_base` (64% fewer). Median local execution time was **169 vs 132 ms**. Initial navigation, Tool definitions, and Model/network latency were excluded; this is not an end-to-end task-speed claim.

The automatic suite additionally covers cold dependency preparation, cached reloads, corrupt-client replacement, simultaneous setup, platform selection, noninteractive Linux package preparation, and cancellation/revocation during setup. Runtime tests verify that the Skill follows the loaded Extension and disappears when it is disabled. The private-browser Chrome consent dialog, Linux ARM64 runtime, and a real cross-machine connection were not exercised locally; those remain platform verification limits.

The workflow fixture is reproducible through `python -m scripts.probe_provider_tool_call --scenario browser_workflow` with the usual Provider, connection, Model, and data-directory options. It executes a real browser against a local test website, prepares dependencies automatically, and reports observed outcomes rather than trusting the Model's completion claim. It is an opt-in live probe, separate from the network-free automatic tests.

Follow-up efficiency verification on 2026-09-06:

- The free-form registration/download workflow used **seven successful Tool calls**, down from eight in the previous observation. The Model independently loaded the Skill and combined text inputs and the plan selection in one fill; the fixture reports `mixed_form_call: true`. This demonstrates one saved call in that task, not a general task-time improvement.
- On the same 100-element fixture and complete minified Tool-result envelope, the former snapshot implementation returned a median **2033 tokens** over 20 deterministic UUID-prefix samples; the new implementation returned **1333 tokens**, a **34.4% reduction** with `o200k_base`. Browser content and all other response fields were held constant. Short-id token cost grows with the counter's digit count; savings vary with page content.
- The updated Luna matrix covers 76 scenarios, including mixed fields, explicit/omitted kind, empty option values, and malformed kinds/fields. 74 calls reproduced the requested shape exactly. In two deliberately malformed cases, Luna removed unsupported fields; those corrected calls validated, while direct runtime tests rejected the original malformed inputs. These are distinct outcomes, not 76 literal argument reproductions.
- Ref tests cover reload/range rollover, concurrent allocation, current-context isolation, hidden/truncated refs, and allocation failure. Mixed-form tests verify validation before input, cancellation/revocation/config changes between fields, and partial failure without replay.

The existing generic Extension settings and Agent Tool-grant UI provide configuration; no frontend changes or browser add-on are involved. See [Extension development](extensions.md) for the shared lifecycle and permission mechanisms.
