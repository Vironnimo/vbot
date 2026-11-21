# Security review, 21 November 2026

The 30 numbers below correspond to the submitted review. The source baseline was `a279df56c`. A "partial" finding had a real defect alongside a claim about reachability, identity or product behavior that the source did not support.

The five items from 17 November were rechecked separately. POSIX Debug and Speech creation modes and the startup repair were already in place. MCP configuration already rejected credential-shaped plaintext environment values, used an owner-only connection file and included substituted values in event redaction. Continuing MCP issues appear under 3.2–3.6. Desktop exposure, remaining Cron semantics and Extension-owned Permission Profile selections are covered below. The unrelated concurrent Debug trace-writer concern remains separate.

## Access and permissions

| Finding | Disposition |
| --- | --- |
| 1.1 | Fixed. A Portal admitted through a trusted HTTPS proxy uses the admitted scheme for its Secure cookie, public origin and upstream forwarding. The standard reverse-proxy configuration does not publish Portal ports. |
| 1.2 | Fixed. Unsafe browser HTTP requests and WebSocket connections must match the Portal origin. Missing Origin remains valid for clients that omit it. Browser cookies still belong to hosts across ports. |
| 1.3 | Fixed. One-time allow and reject require the same operator principal as a remembered decision. The defect concerned in-process dispatch. The normal Extension Service path already restricted this RPC. |
| 2.1 | Fixed. The instance CLI classifies Extension lifecycle, settings, management handlers and execution-related configuration as access changes. Schema inspection and help remain readable. |
| 2.2 | Fixed. Windows stream paths are checked against denies and protected locations on the underlying file, including resolved aliases. |
| 2.3 | Fixed. An ordinary local path that resolves to an unsafe network or device destination is subject to the unsafe-path rule. |
| 2.4 | Fixed. Approved-folder checks apply hard denies and protected locations to the resolved parent of a new output. |
| 2.5 | Fixed. A restricted shell receives instance CLI credentials only for direct CLI commands with literal arguments and literal file redirections. Shell expressions and other programs need separate calls. |
| 2.6 | Partial, fixed for live selections. Deletion and assignment coordinate across temporary bindings, saved Swarm profiles and resumable Swarms, and Factory configurations, including disabled Extensions. Archived Extension-owned groups cannot be restored. Their history and the repository request used for idempotency do not select a live profile. |

## Secrets and MCP

| Finding | Disposition |
| --- | --- |
| 3.1 | Fixed. A passed Secret is a use grant. Giving a replacement in the receiving Session preserves the source record and other recipients. A local replacement retires that branch of the delegation. |
| 3.2 | Fixed for new calls. Substituted short values and generated codes are masked in Tool results and display data. The original call's masking information also protects MCP previews, stored result attachments, background completions and later reads after restart. An old six-digit value stored without its original mapping cannot reliably be identified retrospectively. |
| 3.3 | Fixed. Sampling is sanitized before the approval preview and Provider call, including Tool schemas. Elicitation protects the operator input surface and refuses a request if masking would invalidate its actionable structure. |
| 3.4 | Fixed. MCP encrypts background and Task recovery payloads using Extension-, receipt- and field-bound identities. The first activation migrates existing rows. The original Session receipt continues to control reads. Previously created snapshots, external backups and old SQLite remnants have no retroactive erasure guarantee. An older vBot needs a pre-migration snapshot to use the old MCP database. |
| 3.5 | Fixed. Background completion uses the receiving Session's grant scope for Secret capture. The follow-up extends recipient-aware masking to Agent and Model output surfaces. Shared surfaces use a neutral marker. |
| 3.6 | Partial, fixed for call diagnostics. Agents see events attributable to their Session. Unattributed diagnostics remain operator-only. Recognized credentials and their supported encodings are masked. Catalog changes and explicit resource subscriptions retain their documented connection-wide scope. |
| 3.7 | Fixed. Making a field public takes effect immediately. An independently protected occurrence of the same value still masks it. |

## Desktop and Computers

| Finding | Disposition |
| --- | --- |
| 4.1 | Fixed. A remote page needs native consent to configure dictation. Permission to capture for one server does not carry across to another. |
| 4.2 | Fixed. Native speech enablement belongs to the recording destination. A previous global preference applies only at the installation's own server. Remote Voice configuration and model changes are consent-gated. |
| 4.3 | Fixed. WebView2 automatically grants microphone access only when the selected installation server, requesting origin and top-level document agree. Other servers use WebView2's normal permission handling. |
| 4.4 | Fixed on the verified Windows backend. A restricted dispatcher binds calls and replies to a document, checks the native WebView2 message source and revokes document authority on navigation. Existing WebUI clients keep their method signatures. |
| 4.5 | Fixed. Changes to Computer Sharing calculate newly enabled capabilities under the same lock that covers consent and persistence. A queued re-enable after a revocation needs fresh consent. |
| 4.6 | Partial. Remote mutation of native settings, remembered server selection/list access, global renaming and local model deletion have narrower permissions. The installation server's reserved Computer name is also refused locally. Other secure origins are omitted. Full local sharing and Extension diagnostics, including pushed updates, require an origin-specific grant. |
| 4.7 | Fixed. New registrations receive server-assigned routing IDs. A forgotten ID cannot reclaim its Session selections. Existing registrations and retries after a lost first welcome keep working. |
| 4.8 | Partial, fixed for registration lifetime. The Extension Host checks that the registration is current. Agent-initiated calls must resolve the Session's Computer policy, which bundled Computer Use already did. Trusted Extensions can still perform legitimate operator stop and cleanup operations without a Session. |
| 4.9 | Partial, fixed for loader integrity. Entry points and package-relative imports follow the fingerprint's path and link rules. Fingerprinted native modules remain supported. An installed native Extension is trusted Python code. |

## Channels and Cron

| Finding | Disposition |
| --- | --- |
| 5.1 | Fixed. Group ingress attempts deletion of the source message containing a Secret command or recognized API key, including denied commands and quoted context. Platform permissions still determine whether deletion succeeds. |
| 5.2 | Intended. Group members may edit the visible Telegram checklist. Submission and starting a Run require an administrator. Extension interactions apply chat admission and their own handler authorization. No bundled privileged handler was identified on the reported path. |
| 5.3 | Fixed. Group uploads through bundled adapters are checked for recognized keys before storing text. Existing text FileBlocks, filenames and quoted content are checked again before Agent processing. This does not promise credential recognition in arbitrary binary formats. |
| 5.4 | Partial. Rotation now retires receipts for the old secret and preserves queued work. Body-based replay protection for the whole active key lifetime is intentional. The delivery header is outside the GitHub signature. Bearer idempotency would be a separate delivery feature. |
| 5.5 | Intended. The Email setting for authenticated conversations has a documented scope. Cron can accept automatic mail and can filter unverified From addresses when no trusted authentication server is configured. Sender-restricted jobs use verified authentication results when trusted servers are configured. |

## Decisions accepted on 2026-11-21

- **Cron replay and storage.** An accepted signing key retains one receipt per distinct signed body for its lifetime. A sender that needs two independent events to run should include a distinct event ID in each signed body. Rotation retires old receipts and preserves queued work.
- **Secret names.** Only the receiving Session with a current grant may see an actionable Secret reference. Mask known values and stored references as `[redacted]` when access is absent or the output is shared. Permanent Secret names remain unique. An explicit create attempt can therefore reveal that a name is unavailable, while the refusal discloses neither its owner nor its contents.
- **MCP subscriptions.** Catalog changes and explicit resource subscriptions are shared within a connection. Use separate connections for different trust domains. Per-call diagnostics remain scoped to the calling Session.

The current Extension model trusts installed Python code. The Host exposes supported Computer routing and rechecks registration, while Session policy applies at Agent-initiated resolution. Treating third-party code as hostile would require an isolation boundary beyond these fixes.

## Agent-visible changes

- A restricted shell refusal gives the correction at the failed call: `Run vbot in its own shell call with literal arguments and file paths.`
- The MCP events operation formerly said `Read progress, logs, and change events.` It now says `Read events from this Session's calls and shared resource and catalog changes.`
- A Session with a current grant can see a reference such as `{{secret:BUILD_TOKEN}}`. An inaccessible value or stored reference is shown as `[redacted]`. After a failed shell launch that consumed a one-time Secret, the recovery instruction reads `This launch used up its one-time Secrets. Ask the user to provide them again before retrying.`

## Verification of the original review

The independent review included stored data, denial and cancellation paths, previous client versions and asynchronous delivery. Its corrections covered normalized Portal origins, quoted group Secret commands, the full shell syntax before issuing CLI credentials, multi-line diff truncation, delayed MCP reads and restart, connection-wide event metadata, and native replies and pushes during Desktop navigation.

- An isolated native Windows check used the pinned pywebview 6.2.1 and real WebView2. The production Connection screen and a v0.4.45 WebUI client completed connect, cancel, typed error and return handling, zoom changes and server switching. All nine normal calls had native message-source metadata. All three document changes invalidated the previous credential. A native open-Session event arrived once at the intended origin and was rejected after navigation to another origin. The test used local temporary profiles and no physical audio capture. No live Linux GUI check was run.
- For microphone permissions WebView2 supplies both the requesting origin and the current top-level document. The Desktop checks both against its connection to the installation's own server. [Permission request URI](https://learn.microsoft.com/en-us/dotnet/api/microsoft.web.webview2.core.corewebview2permissionrequestedeventargs.uri) and [top-level document URI](https://learn.microsoft.com/en-us/dotnet/api/microsoft.web.webview2.core.corewebview2.source).
- A canonical test MCP database migrated old rows, restored their contents and refused the pre-migration reader. No installation database, existing backup or live third-party Channel was used.
- The three WebUI suites covering the Desktop bridge, Computer Sharing and Live voice passed all 80 tests.
- All changed backend test files were run together again on the final implementation. 1,371 tests passed and two were skipped. The complete Desktop suite passed 1,210 tests with one skip. Static checks covered the Windows and Linux targets.
- The full Windows backend suite passed 25,190 tests with 76 skips. Its 19 warnings came from the duplicate ZIP-member fixture and the Telegram library's `retry_after` deprecation.

## Verification of the accepted changes

The follow-up review traced the receiving Session's grants through Chat, Compaction, Live, MCP recovery, Shell, Recall, and auxiliary Model calls. Tests cover revocation between attempts, replacement with a different record of the same name, one-time Secret consumption, and output boundaries in excerpts and terminal rows. Historical broker records are projected when read. Existing transcripts and backups were not rewritten. In growing output, the partial-value check starts at three matching characters. The existing minimum length for ambient raw-value matching still applies. Explicitly substituted short credentials have a separate per-call mask.

Shell Review grants authority to a Cron input only with the scheduler's proof for that original input and the current approved job. A later or edited input, imported data or a historical entry without that proof cannot inherit the approval. Independent review also confirmed the existing webhook replay and MCP subscription contracts.

The complete Windows backend suite on `cfb55653e` passed 25,213 tests with 76 skips and 19 warnings. Ruff, both mypy platform targets, the domain-map checks and the public Extension SDK contract passed. The independent reviewer closed every finding from the follow-up.
