# vBot Extension SDK

`@vbot/extension-sdk` is the public browser bridge and presentation library for
Extension pages and embedded views. It uses the same implementations as vBot's
built-in UI. An Extension builds its assets independently and ships the resulting
`web/` directory; users need no vBot source checkout or frontend build toolchain.

## Install the SDK artifact

Packaged vBot servers include the SDK at
`<vbot_root>/resources/sdk/vbot-extension-sdk-1.3.0.tgz`. Install that artifact in
the Extension's own directory; no vBot checkout is needed. The standard WebUI
build creates the tarball before application packaging copies `resources/`.

The tarball also contains `example/`, a complete UI-only Extension with a page
and a composer action. Extract the tarball to a temporary directory, copy
`package/example/` into your Extension directory first, then install the SDK
tarball there and run `npm run build`. Copying the template before installation
lets npm record the dependency in its `package.json`. The Extension consists of its manifest
and the resulting `web/` assets; the Python entry point is optional.

## Build the SDK artifact from source

From a vBot source checkout with WebUI dependencies installed:

```sh
cd webui
node scripts/extension-sdk.mjs
npm pack ./extension-sdk --pack-destination .
```

This produces `vbot-extension-sdk-1.3.0.tgz`. The build follows only the explicit
public exports and their dependencies. The tarball includes source components,
local fonts, their license, and the page build helper. It contains no imports
back into the vBot repository. Publishing to a registry is a separate operator
action; creating this artifact does not publish it.

In the Extension's own directory:

```sh
npm install --save-exact /path/to/vbot-extension-sdk-1.3.0.tgz
npm install --save-dev vite@^8 @sveltejs/vite-plugin-svelte@^7 svelte@^5 terser@^5
```

Keep the resulting `package-lock.json` with the Extension and use `npm ci` for
repeat builds. An Extension can also use plain HTML and JavaScript and import
only the client; the UI components require Svelte 5.

## Build a page

Create `vite.config.js`:

```js
import { extensionPageConfig } from '@vbot/extension-sdk/build';

export default extensionPageConfig({ entry: 'ui/page.html', outDir: 'web' });
```

Use a normal HTML entry loading `ui/main.js`. The main module can mount the page
and load shared styles:

```js
import '@vbot/extension-sdk/styles';
import { mount } from 'svelte';
import Page from './Page.svelte';

mount(Page, { target: document.getElementById('app') });
```

Run `npx vite build`. The output uses relative, hashed assets, so it can be served
from vBot's owner-bound asset route. Register `web/page.html` as the page entry.

## Public imports and lifecycle

```js
import { createExtensionPageClient } from '@vbot/extension-sdk/client';
import Button from '@vbot/extension-sdk/ui/Button.svelte';
import { init, registerCatalog, t } from '@vbot/extension-sdk/lib/i18n.js';
import { provideNavigation } from '@vbot/extension-sdk/lib/navigation.svelte.js';
```

`package.json` lists every supported import. Imports below `dist/` or into a vBot
checkout are private implementation details. Shared component props remain their
documented Svelte callback/snippet contracts; the SDK does not wrap components or
keep a second state model.

Create one client per page lifetime, subscribe through `onContext` and
`onInvalidation`, and call `dispose()` when the page unmounts. Initialize locale
and application timezone from the host context. A Svelte root supplies
`provideNavigation({ registerLayer: client.registerLayer })` so shared dialogs
participate in the application's Back behavior. Register the Extension's own
translation catalog before mounting; use a key namespace owned by the Extension.

Use the client's owner operation method for Extension state, and the declared
host capabilities for application actions. Shared controls that access host
services must receive the matching bridge callback: for example, give `PathField`
the client's `listDirectory` function. Direct RPC, filesystem addresses, parent
DOM access, and native Desktop internals are not page capabilities.

The package build refuses the App transport (`lib/api.js` and its API directory),
including transitive imports. `createChatController` requires an `operations`
object; `createSettingsDraft` requires `submit` and `reload` callbacks, including
its conflict re-read. Bind them to the declared page bridge. Transient PCM speech
playback requires an explicit `readAudio` callback; ordinary media uses its URL.
Operator approval controls, live Portal capabilities and App Contribution
renderers come from the App context and are absent from an isolated page.

The SDK's semantic version describes its public JavaScript imports and component
contracts. The Extension manifest's `api_version` describes required host
capabilities; this SDK release targets API 40. UI-only contributions can declare
host features without creating a Python module. Optional capabilities must be
checked against the host's advertised feature set before use. A reload retires
the old registration and outstanding bridge requests; recreate state from the
next context instead of replaying a mutation.

The page context exposes `api.version` and `api.features`. Check optional features
inside `onContext`, for example `context.api.features.includes('services')`.
Required features belong in the manifest's `required_features`; declared page
services still restrict which calls that page may make.

## Session views and specialist chats

With API 37 and feature `ui.session-chats`, a page can place an owned participant's
Session alongside its domain controls. The host resolves the exact group and
participant under the current page registration and renders the shared Session
view at the size and position of the component. Bound its container's height.

```svelte
<script>
  import SessionEmbed from '@vbot/extension-sdk/ui/SessionEmbed.svelte';
  let { client, groupId, worker } = $props();
</script>

<div style="height: 420px">
  <SessionEmbed {client} {groupId} participantId={worker.id} presentation="full" />
</div>
```

Choose `conversation` (default), `compact`, or `full` for the amount of detail
shown inline. Each offers the user the full Session in Chat. The frame receives
neither its DOM nor its content through the embed.

If a sticky toolbar scrolls over the slot, pass its element ID as `clipBelow`.
The toolbar stays visible and clickable while the outer page scrolls.

With API 38, a page can make references to its records clickable in the
embedded conversation. Pass the current ranges or names to `references` and
route the activated local fragment in `onReference`. For example:

```svelte
<SessionEmbed
  {client} {groupId} participantId={worker.id}
  references={[
    { prefix: '#', fragment: '#post/', label: 'Board post', first: 0, last: newestPost },
    { prefix: '@', fragment: '#participant/', label: 'Participant', names: [['Ada', 'p1']] },
  ]}
  onReference={openRecord}
/>
```

In this example, clicking `#3` calls `openRecord('#post/3')` and clicking
`@Ada` calls `openRecord('#participant/p1')`. Number ranges include both
ends. A page can update the rules while the Session stays open. The host
accepts up to 8 rules, with up to 512 name/ID pairs per rule, within the
bridge's 64 KiB message limit. Prefixes are 1–8 ASCII letters or characters
from `@#~!+`. Fragments start with `#` and an ASCII letter, followed by up
to 63 ASCII letters, digits, underscores, slashes or hyphens.

For a conversation with vBot's built-in App Agent, call
`await client.openAppChat()` from a user click. This takes no arguments.
Register an Extension specialist with `api.register_app_chat_profile` in Python.
From a user click, `await client.openSpecialist('assistant')` opens the host chat
for that owner-local profile. The optional second argument accepts the same
`presentation` choices. Both calls open the host composer. For a new conversation the host creates the Session when the user sends the first message.
After the visible selection changes, publish non-secret labels such as
`client.setChatContext({ section: 'Jobs', field: 'Status', options: ['Queued', 'Done'] })`.
Use `selection` for the selected record's label and `{}` to clear the labels.
Both entry points accept an exact bundled vbot-docs reference in the context,
such as `docs: 'references/configuration.md'`.
The host captures the latest labels, page and route when the user sends.
The frame cannot supply a Tool selection, instructions or credentials.

## Python Extensions

The installed vBot package supplies the Python SDK through `core.extensions`.
Its exported classes are the canonical host contracts, including `ExtensionAPI`,
`ExtensionHost`, `ExtensionOperations`, and `Database`. There is no duplicate
standalone model package that can diverge from the running host. Declare the
required host `api_version` and use only documented public domain interfaces and
the owner-bound host; importing Runtime internals is not an SDK contract.

Python Extensions execute trusted code in the server process. Page isolation and
owner-bound APIs provide predictable application boundaries; they do not sandbox
the Python code.

## UI contributions

Core controls and Extension contributions use the same composition, ordering,
visibility and user-selected replacement rules. The host supports these areas:

- `shell.status`, `shell.sidebar`, `chat.header`
- `chat.composer.actions`, `chat.composer.context`, `chat.composer.footer`,
  `chat.composer.footer.start`, `chat.composer.footer.end`
- `chat.timeline`, `chat.tool-result`, `chat.message`, `chat.message.actions`
- `chat.sessions`, `chat.session.item`
- `agent.editor.sections`, `project.editor.sections`, `settings.sections`

Each embedded page receives `context.ui.surface`, scalar `context.ui.context`,
bounded `context.ui.data`, and the available `context.ui.commands`. Use only
commands advertised in the current context. Local commands are tied to that
Message, Session row, editor or composer. `message.copy` copies the current text;
`session.open` opens the current row; `editor.focus({field})` focuses `name` or
`description` in an Agent editor and `display_name` in a Project editor.
Composer commands edit the draft; starting a Run requires the declared nonhuman
service path, never the human Send path. Message projections exclude attachments
and Provider payloads and mark text truncation. A scope change retires the page
handshake, so retained requests cannot act on the next Message or editor.

`when` accepts the original flat equality map or a structured expression:

```json
{
  "all": [
    {"field": "message_role", "op": "in", "value": ["assistant", "user"]},
    {"field": "context_tokens", "op": "gte", "value": 1000},
    {"not": {"run_active": true}}
  ]
}
```

`all` and `any` require nonempty arrays; `not` contains one expression. Predicates
use a declared context field, `op` (`eq`, `ne`, `in`, `exists`, `gt`, `gte`, `lt`,
`lte`) and `value`. Existence takes a boolean, membership takes scalar values,
and ordered comparisons require finite numbers without coercion. Missing fields
fail comparisons; `{ "field": "project_id", "op": "exists", "value": false }`
tests absence explicitly. Expressions are limited to depth 8 and 64 nodes.
`ui/PermissionProfileSelect.svelte` selects an optional Permission Profile from the names in `host.catalog().permission_profiles`. Supply `id`, `label`, `emptyLabel`, `names`, `value` and `onChange`. Clearing the selection returns an empty string.

Use `client.openAgent(agentId)` to open the standard Agent profile editor (API 35). Declare `"api_version": 39` to open the editor over your page with `client.openAgent(agentId, { expectedIncarnation, relatedScope })`. Pass the stored incarnation to bind edits to that identity. The optional scope identifies the team's delegation targets. Register `agent.profile_targets({scope, after?})` to return pages of `{agents: [{id, name, placement?}], next_after}` with a null cursor on the last page.

Message scopes add `message_id` and `message_role`, Session rows add
`session_selected`, and editor scopes add `editor_mode` (`create` or `edit`).

Interface layout preferences are shared by browsers connected to the same
server. Settings offers explicit migration of an older browser-local layout;
a saved server layout always wins. Unknown or temporarily unavailable
contribution IDs remain saved, and unavailable selected replacements restore the
native control. Extensions declare replacement eligibility; the user chooses
whether to activate it.
