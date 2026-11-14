# Extensions Agent-facing text

Why each sentence of the Agent-facing authoring text the Extensions domain owns exists: the `vbot-docs` references `extensions.md` and `extension-pages.md`. Rules for this file: `.vorch/workflows/domain-map-workflow.md` -> Agent-facing text.

Rows cover the listed additions to the `vbot-docs` authoring references and Extension-owned usage guides; older wording has no recorded reasons yet. Software Factory runtime/usage text has its sentence reasons in `extensions/factory-agent-facing-text.md`.

| Text | Reason |
|---|---|
| API version entry `24 notifications and page badges...` | Authors must declare the version that owns each newly used capability so older hosts refuse before partially loading it. |
| API version entry `27 required owner Tool and Model-request admission...` | Owners using either admission callback or canonical consumption reads declare the version that provides those boundaries. |
| `register_session_runtime declares before_request...` | Lists the optional admission callbacks alongside the existing delivery callbacks so authors choose the correct boundary. |
| `before_model_request(context) runs before each physical Chat Model request...` | A logical-request check occurs before Compaction charges and misses physical retries; owners enforcing settled consumption need the actual admission boundary. |
| `Owned descendants also call it before preparing their request...` | Descendants have no private delivery callback to admit Runs; idempotent admission before their Compaction prevents spending beyond an already exhausted cap. |
| `Return None to admit or raise to stop the request.` | Prevents returning a delivery or refusal string that this no-delivery callback cannot interpret. |
| `This callback does not deliver Notes; use before_request for receipt-backed input.` | Keeps Agent input on the durable delivery and acknowledgment path instead of silently discarding it during admission. |
| `Use await host.notify(...) to request user attention on a declared page; key replaces...` | Gives the supported transient attention channel and keyed replacement rather than a duplicate inbox or an unbounded pile of toasts. |
| `Titles hold up to 200 characters...` | Authors can bound title/body/route/key at the source and send a valid internal page route instead of guessing an external URL. |
| `host.set_page_badge(page_id, count) sets a transient non-negative navigation count...` | The badge is derived presentation; the Extension's database remains its durable inbox. |
| `Both capabilities belong to the current registration...` | A retained callback cannot send attention from an undeclared page or retired owner. |
| `api.on_config_change(handler) calls a sync or async handler(previous, current)...` | Authors can respond to saved live settings without unnecessary reloads. |
| `Use it to re-evaluate idle background admission...` | A cached startup value would leave queued work stalled after a capacity change; live reads preserve current policy. |
| `For a Tool that distinguishes user-authorized work...host.input_authority...` | Old or ancestral human messages do not authorize this Run's autonomous proposal. The current ingress classification must come from the host, not the Model. |
| `await host.pending_approvals(group_id, run_ids) reports...openSession(address)...` | Reuses the owner-scoped normal approval flow without exposing other owners' Runs or inventing a second approval form. |
| `For TemporaryAgentConfig with repository_profile, model="" inherits...` | A Team profile resolves its own defaults; explicit overrides must be applied after that resolution instead of being silently lost. |
| `host.inspect_prompt returns those effective values...` | A setup's same-Model warning needs actual profile resolution, not guessed global defaults. |
| `Raise ExtensionOperationError(code, message)...` | Real RPC otherwise erases domain codes to invalid_request, so a page cannot recover from a deleted Card or stale revision even when its fake bridge tests pass. |
| `A bundled page keeps its English UI text in its own catalog: ...` | The WebUI English catalog is the only source of page text and `t()` takes no English fallback (`webui.md`). Without the paragraph, an Agent authoring a bundled page hard-codes English text or calls `t()` for keys no catalog holds, which render as raw keys; a key that collides with the WebUI catalog makes `registerCatalog` throw before the page mounts. Preventive; no Session evidence yet (2026-09). |
| `The context's route is the page's place in the app's Back/Forward history: ...` | Pages became places in the app-wide Back/Forward history (2026-09-29, `webui/app-shell.md` -> Navigation): `pushRoute` adds a step, `replaceRoute` corrects the current one, and the route arrives through context. Without it, an Agent authoring a page replaces the route on every selection, so Back skips the page's steps, or applies its own selection instead of the route it receives, so Back does nothing visible. Preventive; no Session evidence yet. |
| `A Svelte page calls provideNavigation(...) ...` | Back and Forward close the topmost dialog or menu app-wide before navigating (2026-09-29). A page that does not provide the client's layer registry leaves its dialogs open while Back moves the page's place underneath them. Preventive; no Session evidence yet. |
| `An invalidation callback receives {reason, change}: ...` | Scoped page invalidation (2026-09-28): `publish_change` forwards `{resource, ids, revision}` only to the owning Extension's open page, and `null` marks a full refresh (descriptor reload, reconnect, Run-stream recovery). Without the sentence, an Agent authoring a page reloads every read model on each change; the Swarm page did exactly that under a 23-Agent load (~3.5 operations/s plus a descriptor reload per change). |
| `To let the user enter a path on the vBot server, ... render the shared PathField ...` | Server paths are entered only through the shared `PathField`, never a browser file dialog, because the server may run on another computer (`webui.md` -> Ownership); pages can list the server's folders since API v12 (2026-10, `client.listDirectory`). Without the paragraph, an Agent authoring a page offers a browser file input, which reads the viewer's computer and yields no server path, or a plain text field without completion; `projectShortcuts` would call the app API, which the sandboxed page cannot reach. The trailing-separator sentence comes from the Swarm page: a completed `C:/work/` differed from the profile's `C:/work` and counted as a changed Run directory. The field drops the separator when focus leaves it (2026-10), but a value submitted while the field has focus still carries it. Preventive; no Session evidence yet. |

## Extension usage documentation

| Text | Reason |
|---|---|
| Authoring reference routes installed Extension usage and CLI guides to `extension-index.md`. | Keeps authoring separate from using an installed Extension and removes the hard-coded bundled-Extension list. |
| API version `29 usage documentation` and `Declare one or more usage guides in your extension.json...` | Authors opt into the public metadata contract and make guides discoverable through the existing Skill instead of defining another Skill. |
| `Each entry requires a non-empty title and description...` | Gives authors the complete metadata shape and the encoding and root against which paths resolve. |
| `Use / separators and unique .md paths without .., symbolic links or reserved path names.` | Prevents a guide that cannot be safely published as a portable package resource. |
| `The description tells an Agent which task needs that guide; write the guide with the Extension and declare every guide you link to.` | The index must let an Agent choose a useful guide; undeclared files are not copied and would leave broken links. |
| `Links between declared guides keep their relative paths.` | Authors can split instructions across declared documents without referring to internal generated paths. |
| `vBot includes these guides in vbot-docs under...` | Names the public resource location when an author needs to link to it from other app documentation. |
| `The index records the Extension's load status...` | Prevents interpreting readable setup help as evidence that its commands are currently runnable; disabled and failed code still needs setup guidance. |
| `Editing a guide or its declaration takes effect after vbot extensions reload.` | Gives authors the refresh action instead of expecting running Agents to see changed package content immediately. |
| `Explain setup, task workflows and complete CLI examples...` | Places workflows in the guide and current argument schemas in operation help, reducing duplicated command contracts. |
| `These are reference files of vbot-docs, not separately discoverable Skills.` | User correction 2026-11-14: Extension CLI documentation is distinct from Extension-provided Skills. |
| Computer Use: `The computer_use Extension lets an Agent operate the selected Computer's Windows desktop...` / `The Computer can be the vBot server or a connected Desktop app...` | Corrects the moved guide's server-only description to the existing selected-Computer contract; a headless server can use a shared Windows Desktop. |
| Computer Use: `Inspect the configuration with vbot extensions show computer_use...` | Provides the actual CLI settings route, retaining the user's choice of whether per-app approval is enabled. |
| Computer Use: `These CLI commands configure desktop access; desktop actions themselves use the Computer Use Tools.` | Prevents inventing desktop-action CLI operations when configuring the Extension. |
| Computer Use: `Read the active controls, then use the intended Computer's control_id...` and full status/stop examples. | Makes the control workflow executable with the exact id from current state. |
| Computer Use: `When the result includes controls, select the entry...` | The top-level summary may describe another Computer; each desktop has its own stop target. |
| Computer Use: `An entry with active: false has no active control to stop.` | Prevents inventing an id when no active control is reported. |
| Computer Use: `Read the returned active and stopping state...` | Stopping input does not imply an in-flight action has already finished. |
| Computer Use: `respond relays the user's answer to a pending access request shown by vBot...` and help/stdin examples. | Names the actual response operation without inventing a CLI request-list operation. |
| Computer Use: `Pipe a JSON object into the second command...` | Gives the nested response shape and allowed decisions that generic object help cannot explain. |
| Computer Use: `An Agent does not approve its own access request.` | Distinguishes transporting the user's decision from granting oneself access. |
| Computer Use: `A request that no longer exists cannot be answered...` | Directs an expired-response error back to current state rather than retries of a stale request. |
| Home Assistant: `The homeassistant Extension connects vBot to Home Assistant...` / `Configure it through the CLI or Settings...` | Gives an Agent the purpose and supported setup surfaces before it attempts entity work. |
| Home Assistant: `Inspect its connection settings` and `vbot extensions show homeassistant`. | Exposes the live declared fields without copying the complete schema into the guide. |
| Home Assistant: `Set url to the Home Assistant instance's base URL...` | Distinguishes the server root from an API endpoint and identifies the existing default. |
| Home Assistant: `Save its long-lived access token through ... --stdin or the Extension settings form.` | Names the secret-safe configuration path instead of putting a token in arguments or plain config. |
| Home Assistant: `The token stays in the Secret Store...a saved token alone does not verify connectivity.` | Names live readiness without misreporting credential storage as a successful connection test. |
| Home Assistant: `The CLI configures the connection; entity and service work uses the Agent Tools...` / `An Agent also needs access to those Tools.` | Prevents inventing management operations or assuming that setup bypasses Tool policy. |
| Home Assistant: `Use an entity or service id returned by the discovery Tools...` | Prevents guessing targets or repeating a service mutation after an unknown outcome. |
| Shell Review: `The shell_review Extension reviews prepared shell commands...` / `It uses the normal permission approval UI...` | States the feature and its relation to existing Permission Rules without promising a second permission system. |
| Shell Review: `Inspect the selected Model and the Extension settings` and CLI examples. | Gives the read paths for the two independent prerequisites. |
| Shell Review: `The enabled setting activates review...` | The Extension can be loaded while execution review is off; avoids treating load status as activation. |
| Shell Review: `policy supplies...allow_threshold...timeout_seconds...` | Identifies the settings an Agent configuring review needs without guessing their purpose. |
| Shell Review: `These settings apply live; the probability is a Model estimate...` | Avoids unnecessary reloads and prevents treating a threshold as measured accuracy. |
| Shell Review: `The review operation previews a judgment...` / `It neither executes the command nor grants approval.` | Makes the diagnostic operation usable without promising it changes execution authority. |
| Shell Review: `Pass the request with --stdin as a JSON object containing...` | Gives the complete required request shape; operation help owns optional fields. |
| Shell Review: `Preserve what the user actually authorized...` | Prevents rewriting a preview's authority to force the desired answer. |
| Shell Review: `For example, this PowerShell pipeline previews a directory listing on Windows...` and JSON stdin example. | Shows a complete command without putting nested JSON through shell argument parsing. |
| Shell Review: `Replace the example's directory, command and user request...` | The preview evaluates the supplied evidence; the example's authorization is not the actual user's authorization. |
| Shell Review: `The preview uses the configured Decision Model...` | Explains the preview's Model usage despite the automatic-review switch being off. |
| Shell Review: `Read decision and reason_code...` | Uses the real result fields to distinguish a successful favorable judgment from a request for confirmation. |
| Shell Review: `Other ask reasons, such as missing_authority...` | A review failure is not the same as a Model deciding that the command needs confirmation. |
| Shell Review: `A successful Model judgment also reports...` | Fallback results have no probability or Model fields; callers must not assume them. |
| MCP: `Repeat the same target options...before extensions...` | Flags following an operation belong to its dynamic schema; leading target flags keep follow-up calls on the same server. |
| MCP: `To check an existing connection, take its id from list...` and full commands. | Gives an executable discovery/check workflow with ids taken from actual results. |
| MCP: `Replace <job-id> with the job_id returned by test...` | Starting the asynchronous check does not establish its outcome. |
| MCP: `A completed connectivity check verifies catalog access and health...` | Prevents reporting an application task as verified from a protocol health check alone. |
| MCP: `inspect reads the cached catalog without connecting...` | Explains offline catalog inspection and how to reach additional result pages. |

Factory, MCP and Swarm usage guides originated in the central `vbot-docs` references and now include owner-specific CLI workflows. KeePassXC and 1Password retain their previous setup guidance in separate owner files, with each shared introduction naming just its owner. Existing Computer Use Tool instructions and its separate workflow Skill are unchanged apart from the usage guide's setup and CLI management guidance above.

## Swarm CLI recipes

Sentence reasons for the CLI sections added to `resources/extensions/swarm/docs/usage.md` (2026-11-14). The operation catalog, parser and schema probe cover the command shapes; no operation handler or Model ran.

| Text | Reason |
|---|---|
| `Every abbreviated command below starts with vbot extensions run swarm.` | Gives every compact inline recipe its complete executable prefix. |
| `Use named --profile-id, --swarm-id and --page-id flags: these ids are not positional arguments.` | Generic positional parsing fills id, request_id or job_id, not these resource fields; a bare resource id can become a receipt instead. |
| `Replace placeholders with ids from the corresponding read results...` | Prevents invented identities and keeps the complete current schema discoverable without copying it into prose. |
| `profiles.list returns profile summaries; read a selected profile in full...` | A replacement must start from the full profile rather than a list summary. |
| `To create one, prepare a JSON argument object...` and the JSON example. | Supplies the profile wrapper, minimum usable structure and explicit null creation revision; those cannot be inferred from command names. |
| `Replace the name, Model id and server directory...` | The illustrative Model and directory are placeholders, not installed choices. |
| `The empty allowed list grants no ordinary Tools...` | Prevents silently creating a profile without the Tools its requested work needs. |
| `To use a Project, replace working_directory...` | A Project id supplies Project Context; merely copying its filesystem path does not. |
| `Write the argument object to a UTF-8 file...` and the PowerShell/Bash stdin examples. | Sends complete JSON without shell-quoting loss and locates the input on the CLI's computer. |
| `For an edit, put the complete profile from profiles.get in profile...` | Protects unrelated settings and uses the returned revision to reject competing edits. |
| `A successful save returns the normalized profile with its id and revision.` | The Agent can take the definitive identity and revision from the result instead of inventing them or rereading. |
| `profiles.preview --stdin takes...` | Names the preview wrapper and participant-row selection while making clear that no Run starts. |
| `profiles.delete --profile-id...removes a saved profile; retained Swarms keep their snapshots.` | Distinguishes deletion of a reusable definition from removal of its retained executions. |
| `For each new mutation that takes request_id, generate a fresh UUID...` | Receipt ids distinguish intentional new mutations from exact retries; reusing an id with changed content conflicts and changing it on retry repeats effects. |
| `Start with the chosen profile's id and revision and the user's goal:` and the start/read examples. | Gives a complete start call, including its required receipt, with the reads that expose execution state and usage. |
| `Take swarm-id from the start result or swarms.list.` | The started Swarm identity differs from the reusable profile identity. |
| `swarms.get returns its participants and saved profile snapshot...` | States the read's content and the bounded read option when the snapshot is unnecessary. |
| `swarms.events --swarm-id...reads lifecycle events.` | Makes event diagnosis reachable without direct database access. |
| `Lists and event pages return has_more and a cursor...` | Without continuation, the Agent can incorrectly report that an omitted profile or Swarm does not exist. |
| `Stop with swarms.stop...and resume with swarms.resume...` | Both lifecycle actions require swarm_id and a request_id even though they change no text. |
| `Resume can name --participant-id...; omission resumes inactive participants.` | Names the identity source and separates one-participant recovery from the whole inactive group. |
| `The same participant filter works with swarms.usage.` | Allows a focused usage query instead of reading or attributing the whole group's consumption. |
| `swarms.delete --swarm-id...permanently removes a stopped Swarm.` | Stop retains the conversation; deletion is a distinct operation with a stopped-state requirement. |
| `To change delivery for retained work, swarms.settings --stdin takes...` | Supplies the live-settings wrapper rather than directing an Agent to edit a profile that only affects future work. |
| `Read the current delivery and settings_revision with swarms.get...` | The live settings revision differs from the profile revision; preserving untouched routes avoids unrequested replacement. |
| `A profile edit affects future Swarms, not this snapshot.` | Makes the retained execution boundary explicit at the live-settings recipe. |
| `Read discussions and the requested discussion's posts:` and Board read examples. | Gives the two actual operations instead of making an Agent guess a discussion's identity. |
| `Take discussion-id from board.list; omitting it reads the main discussion.` | Explains both explicit targeting and the useful default without guessing a main-discussion id. |
| `--message-id...reads one exact post.` | A post id is not a discussion id or numeric paging position. |
| `A returned cursor continues with older posts; --after...reads newer ones...` | The two paging directions have different continuation values; confusing them skips or rereads posts. |
| `Post the user's message with board.post...; --reply-to...answers a particular post.` | Provides all required write inputs and distinguishes a new post from an answer. |
| `For multiline text or a recipients array, use the same JSON stdin method...` | Preserves multiline content and typed recipient arrays without shell escaping. |
| `These are operator posts: a fresh post can resume a stopped or failed Swarm...` | Posting can start work; it cannot serve as a harmless status probe. |
| `Find and read Wiki pages:` and Wiki examples. | Exposes Wiki operations through the management surface rather than the participants' private Tools. |
| `Take page-id and its current revision from Wiki results.` | Both are scoped recorded values needed for later reads and changes. |
| `--query...searches pages with list, or text in one page with read.` | Defines the search scope so a no-match in one page is not taken as a whole-Wiki result. |
| `For create, send action, swarm_id, title, complete Markdown content and request_id...` | Supplies the full create wrapper including the receipt and preserves Markdown through stdin. |
| `update, delete and restore also take page_id and expected_revision...` | Distinguishes the current edit baseline from restore's historical revision and explains whole-content versus passage edits. |
| `On a conflict, inspect the changed page before reapplying the requested edit.` | A competing edit changes the basis for the mutation; blindly sending a new revision can erase that work. |
| `Wiki edits send no Board messages; post the returned page link when participants need to notice it.` | A saved knowledge page does not itself deliver a message or wake participants. |
