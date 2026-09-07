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
| `managed` (default) | vBot starts its own Chrome/Chromium on the server. **Show managed browser** defaults to enabled when the server process has a desktop, and disabled without one. | Starts with a fresh profile; cookies remain while the connection is alive. Downloads are available to vBot. |
| `existing` | Connects to a running local Chrome with its debugging access enabled. | Uses the user's existing logins and available tabs. Downloads stay on that computer. |
| `remote` | Connects to the **Remote CDP URL** configured in Extension settings. | Uses the profile and logins of that browser. Upload paths and downloads belong to its computer. |

The connection mode is an Extension setting shared by Agents. It is not a Tool argument the Agent can override. The remote endpoint is stored through vBot's secret settings and may be an HTTP(S) discovery endpoint or a WebSocket CDP URL.

Saved **Show managed browser** values take precedence over the desktop-aware default. On Windows, the default checks the process's visible window station; macOS checks the console owner; Linux checks its display environment. A Pi server without a desktop remains headless. Changing a connection setting takes effect when the current connection is retired and the Agent reconnects.

### Use an already logged-in Chrome

For Chrome 144 or newer, open `chrome://inspect/#remote-debugging` in the Chrome you want to share and enable remote debugging. Select `existing` in vBot, then approve Chrome's connection dialog when the Agent first uses the Tool. The backend connects to that browser without restarting it or copying its profile. This is Chrome's built-in consent flow; see [Google's setup guide](https://developer.chrome.com/blog/chrome-devtools-mcp-debug-your-browser-session?hl=en) and [agent-browser CDP mode](https://agent-browser.dev/cdp-mode).

The connection initially uses a dedicated tab. The Agent can list tabs and select an existing one when the task requires it. Closing the connection leaves the user's browser and tabs running. Explicitly closing a tab still closes that tab.

CDP support here targets Chromium-based browsers. The Chrome consent flow above is specific to Chrome; it does not establish equivalent Firefox or Safari support. A compatible CDP endpoint can also be configured directly in `remote` mode.

### Server and browser on different computers

`existing` discovers a browser on the **server computer**. Opening the vBot Desktop Client on Windows does not move browser execution there when the server runs on a Pi.

To use Windows Chrome from such a server, provide a CDP endpoint reachable by the server in `remote` mode, for example through an authenticated browser service or a private tunnel to a browser configured for CDP. Treat this endpoint as access to the browser's logged-in accounts. Chrome's local consent discovery is not automatically forwarded over the network; this Extension does not add a Desktop Client browser bridge.

## How the Tool works

The Agent starts with `open` or `tabs`. It can first use `status` to inspect the connection mode, managed window setting, and browser host without installing anything or starting a browser. Status reports vBot's tracked connection state; it does not probe external browser liveness. A pending settings change appears separately as `next_connection` without closing the current browser. The first opening result also identifies the connection mode and host, without disclosing its endpoint.

Navigation returns a compact snapshot with element refs, capped at 4000 characters by default. `selector` focuses a section and `limit` expands a snapshot up to 16000 characters; both also apply to an action's requested observation. Truncation preserves complete lines and publishes only refs included in the returned content. Refs belong to the current snapshot and Run; the Agent refreshes the snapshot after page changes. `read` retrieves bounded, paginated page text. `screenshot` returns an image directly to the Model and a preview to the UI, without a separate file-reading Tool call.

Snapshots and reads briefly observe document readiness and changes before collecting content, with bounded retries for navigation races. Results identify the current URL, title, observation state, and the actual HTTP response status when the browser exposes it. Snapshots also include a short visible-text excerpt, so a website error is visible even without interactive elements. A still-changing snapshot withholds actionable refs and directs the Agent to `wait`. A quiet interval is only a momentary observation: delayed site work can start later, so Agents must check the destination and outcome.

`wait` accepts expected visible `text`, an exact destination `url`, or neither for a bounded settling observation. It returns a fresh snapshot by default. An unmet condition after five seconds returns `condition_met: false` alongside the current observation, allowing a decision from the page's actual state. Browser errors distinguish navigation races, timeouts, connection loss, and unavailable elements without returning raw native diagnostics or claiming a website's detection mechanism.

The Agent can fill text inputs and selection fields together with one `fill` call. Fields use `{target, text}` for text inputs or `{target, text, kind: "select"}` for option values. Every field is validated before input begins; if a later field fails, earlier completed fields are reported and remaining fields are skipped. Input operations do not automatically return another snapshot unless `observe` is requested. This avoids repeating the whole form after each field. The Extension does not replay input during error recovery. The pinned native client's transport has its own resend behavior, described below. Invalid argument errors identify the affected field or action's accepted fields and the valid correction, without echoing form values.

If `open` loses its command reply, times out, or encounters a navigation-context error, it checks the same owned tab and observes the current page. When that observation succeeds, the Tool returns it with `navigation_confirmed: false` and `navigation_error`; it does not claim that the missing command result was successful. Agents can verify the destination from its actual URL and content, including redirects, and continue without another `open`. `observe: false` returns only page metadata in this recovery path. If the page cannot be observed, the error directs the Agent to `wait` or `tabs`. `retryable: false` prevents recommending blind repetition of the uncertain command; it does not prohibit those inspection actions.

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

### Live website audit, 2026-09-07

A Windows comparison executed the production `BrowserService.handle` and native client directly, with separate managed connections and an isolated data directory. DuckDuckGo search failed in both headless trials with a redirect to its `/static-pages/418.html` error page. Both headed trials returned search results including a news section, verified through page text and a screenshot. The second comparison reversed the mode order. This isolates an observed mode-dependent difference on this host; it does not establish DuckDuckGo's exact detection rule or guarantee that headed browsing works on every site or network.

The audit also reconstructed a real Agent Run that loaded the bundled Skill and attempted the same task. Its Tool results confirmed the error page, but the Model's claim that all search submissions were blocked by automation detection was stronger than the returned evidence. Three repeated submit actions still observed the homepage. Four observations after form input returned 10,362–10,530 snapshot characters, mostly unrelated homepage content.

The pre-change headed probe reproduced an immediate post-Enter observation of the old page, followed by a failed read and an empty interactive snapshot during navigation. A subsequent navigation/read returned real results. At that time, observations had no settling phase, generic `browser_failed` results did not distinguish navigation races from connection failures, and `status` omitted the headed setting. The changes and follow-up checks below address these interface defects; a bounded observation still cannot prove that all future site activity has completed.

### Recovery follow-up, 2026-09-07

- The updated production Tool searched DuckDuckGo successfully in a visible managed browser. In headless mode, the post-Enter observation directly exposed the error-page URL and visible error text. That page's actual HTTP response status was **200**; the `418.html` filename is not an HTTP status or proof of automation detection.
- A live local form with a delayed redirect completed after one submission, and an intentionally unmet wait condition returned the current confirmation page with `condition_met: false`.
- The free-form Luna workflow now includes 200 unrelated footer links and a 3.5-second response delay with a page busy indicator. The Agent loaded the Skill, filled the mixed form, received a still-changing observation after submission, used `wait`, and completed the download. All eight Tool calls succeeded; the fixture independently verified exactly one correct submission, the downloaded bytes, and the returned reference. Snapshot content remained below the 4000-character default. This exercises recovery after the settling deadline, rather than only an immediately completed form.
- The expanded 86-case Luna matrix produced 84 exact argument matches. In two deliberately invalid cases the Model removed unsupported fields. Executing the captured arguments against the runtime fixture produced 80 valid successes and six expected validation rejections, with no unexpected runtime failures. These invocation checks remain distinct from free-form workflow understanding.
- A continuation probe supplied the updated Skill and captured headless DuckDuckGo Tool history to Luna, then executed its next browser calls. It tried three alternative DuckDuckGo URLs and reported the unavailable route and website errors without asserting an automation-detection cause. This is one observed recovery decision, not a guarantee that Models never make unsupported diagnoses.

Automatic regressions cover side-effect-free status, desktop defaults and explicit overrides, bounded observation retries, cancellation and permission changes between retries, invalidated refs, compact and scoped observations, unmet wait conditions, and uncertain input without replay. Platform defaults are tested through controlled OS boundaries; the live browser checks above ran on Windows.

### Lost navigation replies, 2026-09-07

A captured Agent Run opened Reddit's `/r/artificial/` and `/r/ChatGPT/` successfully, then received two generic failures for `/r/claudeAI/` after 7.45 and 6.52 seconds. Tab inspection showed the requested URL with an empty title; `wait` returned the loaded `/r/ClaudeAI/` page with HTTP 200. These were failures of the native navigation call, not vBot's subsequent snapshot: that observation stage already preserves successful input through `observation_error`.

A separate visible-browser reproduction produced the same failure twice and exposed the native diagnostic: `Invalid response: EOF while parsing a value at line 1 column 0 (after 5 retries - daemon may be busy or unresponsive)`. The page still reached the redirected destination; opening the final spelling directly succeeded. A subsequent trial also had one successful lowercase navigation, so this is a reproducible redirect-associated failure, not a rule that all such redirects fail.

With reconciliation enabled, the same native failure returned the current stable ClaudeAI page, its snapshot, and the explicit unconfirmed-command fields. The Extension issued no second navigation. A fresh Luna continuation answered the requested page title from this result; when supplied only the lost-response error, it chose `tabs` and `wait` instead of another `open`.

The repeated 86-case Luna matrix produced 83 exact argument matches; the three differences corrected deliberately invalid inputs. Executing the captured calls against the runtime fixture yielded 81 valid successes and five expected validation rejections, with no unexpected runtime failures. The full backend gate passed all 10,899 tests.

The pinned native client itself retries transient socket failures up to five times by resending the command (`cli/src/connection.rs::send_command` in agent-browser 0.36.0). Its public CLI has no retry-disable option. That dependency behavior remains a limit: vBot's recovery does not add retries, but cannot promise exactly-once browser input across a lost native reply. Regression tests cover the Extension boundary, including old/error/changing pages, scoped observations, failed recovery, cancellation, permission revocation, configuration changes, and closed target tabs.

The existing generic Extension settings and Agent Tool-grant UI provide configuration; no frontend changes or browser add-on are involved. See [Extension development](extensions.md) for the shared lifecycle and permission mechanisms.
