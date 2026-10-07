# Extension pages and owned Sessions

Read this reference when an Extension registers a page or owns Sessions. Read [extensions.md](extensions.md) first: it covers installation, `register(api)`, Tools, managed operations, the startup host and verification.

API 6 Extensions can register an isolated HTML page with `register_page` and private Session capabilities with `register_session_tool`, `register_session_prompt_block`, and `register_session_runtime`. Private Tools are absent from public catalogs and require the complete current owner grant set. Their handlers receive exact input types; ordinary Tools keep existing argument normalization. For a complete built page and owned-Session implementation, inspect `resources/extensions/swarm/` in the installed source; the generic page client lives in `webui/src/lib/extensionPageClient.js`.

## Pages

A page does not require temporary Agents. For example, an inventory Extension can declare its own prebuilt dashboard:

```python
def register(api):
    api.register_page("inventory", "Inventory", "web/page.html")
```

Its assets must be relative to that entry and stay beneath the registered build root. Bundled sources use `ui/page.html` and build to `web/` with `cd webui && npm run build`; arbitrary installed Python Extensions ship prebuilt assets. The same build and installer paths include these assets, with no Node requirement at runtime or separate npm dependency tree.

The page runs in an opaque-origin sandbox. Its parent bridge validates the source window, nonce and loaded page epoch, and provides theme, locale, timezone and route. A bundled child imports `createExtensionPageClient` from `$lib/extensionPageClient.js`, calls `operation(name, arguments)` for its own registered operations, and uses `readHistory`, `subscribeRun`, `openLink` and `openMedia` for authorized history and references. Register context/invalidation callbacks, refresh read models on reconnect, and dispose the client on unmount. The context's `route` is the page's place in the app's Back/Forward history: `''` at the page's start, otherwise `/segment/...`. Call `pushRoute(route)` when the user moves to another place of the page (opens a record or a tab) and `replaceRoute(route)` to correct the current place (a default choice, a record that no longer exists); keep filters, searches and expanded sections out of the route. Both return before the route changes: the new route arrives in the next context callback, which is also how Back/Forward and the app's navigation reach the page, so always show the place `route` names. A Svelte page calls `provideNavigation({ registerLayer: client.registerLayer })` from `$lib/navigation.svelte.js` in its root component, so the shared `Modal` and `ContextMenu` register as layers that Back and Forward close first; register any other overlay with `registerLayer({close})` while it is open and call the returned function when it closes. An invalidation callback receives `{reason, change}`: `change` is the `{resource, ids, revision}` your Extension passed to `publish_change`, or `null` when anything can have changed, so refresh everything the page shows. Reconnect never retries a mutation automatically. The child has no arbitrary RPC or application-origin fetch capability; stale page/Run tokens expire on reload or disposal.

A bundled page keeps its English UI text in its own catalog: `ui/i18n.js` default-exports a frozen object that maps keys to text, with every key under the Extension's own prefix (for example `inventory.title`). Before mounting the page, `ui/main.js` passes that object to `registerCatalog` from `$lib/i18n.js`. `registerCatalog` throws, and the page does not start, when a key already exists in the WebUI catalog. Render text with `t('inventory.title')` or `t('inventory.count', { count })`. Every key needs a catalog entry, because `t` renders a missing key as the key itself. For a key built from a code the server sends, use ``tOr(`inventory.state.${code}`, code)``, which renders the code when no entry exists.

Bridge commands allow up to 64 KiB and host replies up to 8 MiB, including their envelopes. Oversized operation replies reject the request; requests without a reply time out after 30 seconds without replay. A timeout does not prove that a mutation failed: refresh its state before deciding whether to repeat it. Bundle fonts and other presentation assets with the page; external font stylesheets are blocked by its CSP.

## Owned Sessions

The startup host's `temporary_agents` creates canonical bound Sessions without Identity workspaces, opens an admission group, starts initial or continuation inputs, closes/drains owned work, and exposes scoped history, Run, receipt and Statistics reads. Handles become invalid when the registration retires. The retained Sessions survive reload; reopening and admitting work is an explicit Extension decision.

While your Extension is installed, even disabled, only it can remove its Sessions: vBot refuses to archive, move or delete them and refuses to remove a Project that contains them. Remove a closed group you no longer need with `await temporary_agents.delete_group(group_id)`. If your own records can lose groups, for example after your database was restored from an older snapshot, reconcile in your startup callback: page your group ids with `await temporary_agents.groups(after=last_id, limit=100)` and call `await temporary_agents.archive_group(group_id)` for each group you no longer know. It moves the group's Sessions into the vBot archive, where they are deleted permanently after the user's retention period (default 30 days); `delete_group` can still remove them earlier. `archive_group` and `delete_group` raise `ValueError("group_not_closed")` for an open group. An archived participant cannot be created again, so give new work a new group id. When the user removes your Extension, vBot moves its Sessions into the archive at the next startup or reload. Declare `"api_version": 10` in `extension.json` to use `groups` and `archive_group`.

The startup host's `catalog()` also returns public System Prompt block metadata. When available,
`await host.inspect_prompt(config, project_id)` previews a `TemporaryAgentConfig`
without creating a Session or Run: `text` is the assembled System Prompt, `blocks`
contains rendered content and enabled/active/included state, and `tools` contains
the separately transmitted Model Tool definitions. Preview applies the ordinary
Project ceilings, Model Tool routing and this Extension's private Tool grants.
The optional configuration field `prompt_blocks` is an exhaustive block selection;
omit it to inherit the normal layout. Include `core:agent_body` to retain the
configuration's editable instructions. An explicit empty list emits no blocks.

`register_session_runtime` declares `before_request`, `run_finished`, and `quiesce`, with optional `acknowledge_delivery` and `reconcile_tool_batch` callbacks. Return `PreparedSessionDelivery` from the request boundary; Chat commits its note and receipt together before acknowledgment. A successful Tool can request a receipt or graceful turn end through its host-installed `ToolContext` callbacks. Reconciliation runs after every sibling Tool Result is durable and returns `ToolBatchDecision`. Required callback errors propagate; quiesce must drain owned work before its capabilities disappear.
