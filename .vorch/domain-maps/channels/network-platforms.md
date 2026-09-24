# Slack, Mattermost and WhatsApp transport details

Read with the parent `channels.md`. Paths below are repository-relative.

## Ownership and shared behavior

`core/channels/slack.py`, `mattermost.py` and `whatsapp.py` compose the existing conversation engine through private `NetworkChannelAdapter` in `_network_adapter.py`. This is transport implementation inside the Channels owner, not another conversation layer. It owns lazy HTTP clients, bounded downloads, transport facts, recent receipts and cleanup; the engine retains authorization, Queue admission, Session routing, Commands and Run relay. Channel I/O uses `BoundedWorkerPool`: cancellation drains admitted disk operations before releasing ingress/setup ownership, so a restart cannot race an abandoned receipt write or bridge installation mutation.

`received.json` stores the last 4096 accepted transport message IDs, written atomically on the bounded Channel I/O pool after engine admission. It suppresses recent replay across adapter restarts; it is not transactional exactly-once delivery or a durable inbox. Slack/Mattermost connections do not backfill messages missed while offline. Allowlist rejection occurs before media downloads. Files in one platform message enter the engine as separate media items, so a failed attachment does not discard its siblings. HTTP downloads enforce both advertised and actual byte limits. Request errors hide URLs and credentials; uncertain writes are non-retryable. Platform rate limits retain the shared delivery retry behavior.

Outbound text for all three platforms is chunked at 3500 characters through `NetworkChannelAdapter.message_chunks` (the shared Markdown-aware splitter, see `channels.md`). Slack and Mattermost outbound retries run at each individual post/upload step through the shared bounded policy. Exhaustion is non-retryable to callers, so an engine retry cannot duplicate previously acknowledged chunks or files. Uncertain write failures, including upload HTTP 5xx responses, remain non-retryable.

Slack and Mattermost threads use the parent chat's Session and return replies to the supplied thread/root. Mention-only groups require an explicit bot mention or configured wake regex; these adapters do not resolve whether a thread reply addresses a bot. Neither supplies typing indicators, interactive buttons, history backfill or fetched quoted-message content. Their current upstream contracts have mocked wire tests; live account delivery remains unverified.

## Slack

The bot token comes from `token_env_var`; the Socket Mode app token comes from a distinct `app_token_env_var`. `auth.test` establishes the bot identity. `apps.connections.open` uses only the app token and yields a validated Slack WebSocket URL. Envelopes are acknowledged before message dispatch. Bot/self messages and edited/deleted subtypes are ignored. Text/file-share events use stable string conversation IDs.

Outbound files use `files.getUploadURLExternal`, raw-byte upload, then `files.completeUploadExternal`; the retired `files.upload` endpoint is never used. Only HTTPS `files.slack.com` attachment URLs receive the bot token, and redirects are disabled. `conversations.info` validates outbound chat metadata before recording Session context. Required app scopes/events and installation steps are in `USAGE.md` -> Slack.

## Mattermost

`server_url` is the operator-selected HTTP(S) server root, including any deployment subpath, without embedded credentials/query/fragment. The bot token comes from `token_env_var`. `/api/v4/users/me` establishes identity; the WebSocket authentication challenge must succeed within 15 seconds before `connected` becomes true. Posted events supply sender, channel type, mention IDs, root and file IDs. Bot/self/system posts are ignored. REST handles Channel metadata, bounded authenticated file download, multipart file upload and posts.

## WhatsApp self chat

This integration is an explicitly selected unofficial linked-device connection to an existing account, limited to the self chat. It does not use the Business Cloud API or require a separate number. Both inbound allowlist and outbound target accept only literal `self`; empty inbound allowlist still denies all. No other chat may trigger a Run.

`_whatsapp_setup.py` installs the exact `whatsapp_bridge/package-lock.json` into `<data-dir>/channels/<id>/bridge` using an operator-installed Node.js >=22 and npm. Setup is explicit, requires a disabled and stopped Channel (including while automatic recovery is pending), uses `npm ci --ignore-scripts`, verifies imports, and publishes a source-digest readiness stamp. Bundled source/release files are never mutated. `ChannelService.setup_whatsapp` owns the cancellable installation task; repeated setup calls reuse it. Setup and pairing serialize per Channel and reject conflicting config changes, deletion, enable, disable and restart requests; pairing itself enables within its existing ownership. Service shutdown cancels setup. A changed bridge digest requires disable/setup again.

`whatsapp.py` launches the bridge as a hidden child with private JSONL stdin/stdout, no HTTP listener. Its reader resolves command responses independently of the bounded inbound worker, avoiding media/Command deadlocks; either task's failure tears down the connection. Secrets and upstream library logs never enter Channel diagnostics. QR PNG data URLs are transient, expire from the status projection after 45 seconds, and clear on connection/shutdown. They appear only on dedicated authenticated WhatsApp RPC responses, never generic Channel status or CLI output.

Baileys auth persists under `<data-dir>/channels/<id>/whatsapp/auth`. Treat that directory like account credentials. Explicit re-pair archives auth under a generated `revoked_*` sibling after stopping the adapter; it does not revoke the remote linked device. Remove old devices from WhatsApp itself. The bridge's `delivery.json` preserves the initial message-time boundary and 4096 sent IDs. It rejects pre-setup history, non-self destinations, messages not marked `fromMe`, and its own sent IDs. Phone and LID identities are normalized, including device suffixes; both `notify` and `append` upserts are accepted for user-authored self-chat messages. Outbound IDs persist before transmission to prevent echo Runs across restart.

Logged-out bridges remain idle for explicit re-pairing; ordinary connection failures use service backoff. Disconnects immediately fail pending bridge calls without retrying an uncertain delivery, including while the adapter remains idle after logout. Private media requests are bounded in both processes. No history sync is requested. Upstream compatibility and personal-account restrictions remain operational limitations; local tests cannot establish account eligibility.

## Verification owners

- `tests/core/channels/test_network_channels.py`: real engine ingress, access gating, receipts, media limits, outbound wire contracts and JavaScript gate execution.
- `tests/core/channels/test_network_lifecycle.py`: handshakes, acknowledgements, worker/reader concurrency, cancellation and uncertain-write errors.
- `tests/core/channels/whatsapp_gate.test.mjs`: self identity, other-chat/history rejection and echo suppression.
- RPC/CLI and mounted WebUI tests cover configuration, credential boundaries and QR setup.
