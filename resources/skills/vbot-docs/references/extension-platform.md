# Extension UI and product integrations

Read this reference to add UI controls or pages, call product services, register a Provider or Channel platform, or supply a local Desktop capability. For server Tools, hooks, managed operations and the startup host, read [extensions.md](extensions.md); for page lifecycle and owned Sessions, read [extension-pages.md](extension-pages.md).

## Pick the declaration

Use a manifest-only Extension for static status text, a button that calls an existing UI command, or a prebuilt page. Add a Python server entry when the Extension needs its own operations, state, Tools, Provider definitions or Channel adapters. Add a separate local Desktop entry only for code that must execute on the user's computer.

Run `vbot home` on the server's computer to find `data_dir` and `vbot_root`; install the server Extension in `<data_dir>/extensions/<name>/`. Give it a unique directory name, which is its identity; `name` in the manifest is the display label. After changing server declarations or assets, run `vbot extensions reload` and inspect `vbot extensions list` for load failures before exercising the behavior.

Declare `"api_version": 31` for these platform APIs and list the features the Extension requires in `required_features`: `ui.pages`, `ui.contributions`, `ui.preferences`, `services`, `observations`, `providers`, `channels`, `desktop.manifest`. The host rejects unknown required features before importing server code. Python server code is trusted and runs without a sandbox; page isolation does not restrict Python privileges.

## UI contributions

For a composer button that inserts text, create `extension.json` without a Python entry:

```json
{
  "name": "Review shortcut",
  "api_version": 31,
  "required_features": ["ui.contributions"],
  "contributions": [{
    "id": "review",
    "surface": "chat.composer.actions",
    "kind": "action",
    "title": "Review changes",
    "action": {
      "command": "composer.insert",
      "params": {"text": "Review the current changes."}
    }
  }]
}
```

Python declares the same contribution with `api.register_contribution("review", surface=..., kind=..., title=..., action=...)`; do not declare the same id twice. Local ids start with a lowercase letter and contain lowercase letters, digits, `_` or `-`, up to 80 characters. The public id is `extension:<directory-name>:<local-id>`.

| Surface | Placement |
|---|---|
| `shell.status`, `shell.sidebar` | Bottom status area and sidebar |
| `chat.header` | Chat header |
| `chat.composer.actions`, `chat.composer.context` | Composer controls and context |
| `chat.composer.footer`, `chat.composer.footer.start`, `chat.composer.footer.end` | Composer footer and its sides |
| `chat.timeline`, `chat.tool-result` | Chat timeline and an individual Tool Result |
| `settings.sections` | Settings content |
| `chat.message`, `chat.message.actions` | An individual Message and its actions |
| `chat.sessions`, `chat.session.item` | Session list and one Session row |
| `agent.editor.sections`, `project.editor.sections` | Agent and Project editors |

Every contribution supplies `surface`, `kind` and `title`; `kind` is `status`, `action` or `view`. Use `text` for display text and `order` for a numeric default order; `when` accepts an exact-match object such as `{"has_session": true}`, or a bounded expression with `all`, `any`, `not`, or `{field, op, value}`. Operators are `eq`, `ne`, `in`, `exists`, `gt`, `gte`, `lt` and `lte`; numeric comparisons require numbers, and a missing field matches only an explicit `exists: false` condition. Context fields depend on the surface; `agent_id`, `session_id`, `project_id`, `run_id`, `run_active`, `has_draft`, `has_session`, `connected`, `view`, `settings_section`, `computer_id`, `tool_name`, `tool_status`, `tool_call_id`, `context_tokens`, `context_window`, `session_cost`, `output_speed`, `message_id`, `message_role`, `session_selected` and `editor_mode` are supported names, not a promise that every value is available. A `text` value or a direct parameter value can bind one field with `{"context": "session_id"}`; a missing parameter binding refuses the action.

An `action` names either an owner operation (`{"operation": "refresh", "params": {}}`) or a UI command (`{"command": "composer.insert", "params": {"text": "..."}}`). Composer surfaces provide `composer.insert`, `composer.replace` and `composer.clear`; insertion and replacement take `text`. Message actions provide `message.copy` for the current Message; Session rows provide `session.open` for that row. Editor sections provide `editor.focus` with `field` set to `name` or `description` for Agents, or `display_name` for Projects. An embedded page receives the available command names in `client.context.ui.commands` and invokes one with `client.command(name, params)`; use only commands available in that context.

For dynamic controls, declare `source: {"operation": "list_controls", "params": {}, "resource": "controls"}` and return `{"items": [{"id": "refresh", "label": "Refresh", "action": {"operation": "refresh"}}]}` from that managed operation. List any additional operation named by returned items in the contribution's `operations` array; the declared source and fixed action operations are included automatically. Publish changes with `host.publish_change("controls", [...], revision)` so the controls reread their source; the source `resource` restricts which owner changes refresh them. Results display at most 100 items; use the Extension's page for larger collections.

Use `kind: "view"` and `page: "panel"` to embed the Extension's registered page; set that page's `navigation` to `false` when it needs no sidebar entry. The page receives `client.context.ui.surface`, the surface's scalar `context`, bounded `data`, and available `commands`; handle changes through `client.onContext`. Message data contains id, role, timestamp and up to 32,768 text characters with a `truncated` flag; Session data contains id, Agent/Project ids, title and activity state. Editor data exposes bounded name/description values for Agents and display name for Projects, without the full form or credentials. Use native status or action contributions for small controls, and a page for a custom layout.

An icon is one of `network`, `panel`, `grid`, `sparkles`, `factory`, or an object with SVG `paths` and a `view_box`; it is geometry, not markup or a remote URL. Users hide, order and select replacements under Settings > Appearance; these preferences are saved on the server and shared by its browsers. An existing browser-local layout can be explicitly imported when no shared layout has been saved. Set `replaces` to the exact public id offered as the replacement target; the replacement is inactive until the user selects it, and the original returns when the replacement is unavailable.

## Build a page with the SDK

Declare pages in the manifest or Python, with asset paths relative to the Extension directory:

```json
{
  "api_version": 31,
  "required_features": ["ui.pages", "services"],
  "pages": [{
    "page_id": "panel",
    "title": "My panel",
    "entry": "web/page.html",
    "navigation": false,
    "services": ["agent.list"]
  }]
}
```

The equivalent Python declaration is `api.register_page("panel", "My panel", "web/page.html", navigation=False, services=("agent.list",))`. `navigation` defaults to true; `services` defaults to an empty list. Assets stay under the page's build root and use relative URLs.

The installed SDK artifact is `<vbot_root>/resources/sdk/vbot-extension-sdk-1.2.0.tgz`; its `example/` directory contains a complete manifest-only page and composer action. Extract the artifact into a temporary directory and copy its `package/example/` contents into the Extension's build directory; from there, install the SDK and build:

```bash
npm install --save-exact <vbot_root>/resources/sdk/vbot-extension-sdk-1.2.0.tgz
npm install --save-dev vite@^8 @sveltejs/vite-plugin-svelte@^7 svelte@^5 terser@^5
npm run build
```

Replace `<vbot_root>` with the path from `vbot home`, quoting paths with spaces. Keep `package-lock.json` and use `npm ci` for repeat builds; install the manifest and resulting `web/` directory on the server, which needs no Node build process at runtime. The SDK's packaged `README.md` and `package.json` describe its build helper and public component exports.

Import `createExtensionPageClient` from `@vbot/extension-sdk/client`, shared styles from `@vbot/extension-sdk/styles`, and shared controls such as `@vbot/extension-sdk/ui/PathField.svelte`. Create one client per page lifetime and call `dispose()` on unmount. Apply the host's theme, locale, timezone and route from `onContext`, using `applyExtensionTheme(context.theme)` from `@vbot/extension-sdk/lib/extensionTheme.js` for theme variables; follow [extension-pages.md](extension-pages.md) for navigation, invalidation, server path input and translation catalogs.

## Python SDK and API 32

Import server contracts from `vbot_extension_sdk`, Provider contracts from `vbot_extension_sdk.providers`, Channel contracts from `vbot_extension_sdk.channels`, and native contracts from `vbot_extension_sdk.desktop`. These are the same host classes used by built-ins; existing `core.extensions` imports continue to work.

For an external Python 3.14 authoring environment, install `<vbot_root>/resources/sdk/vbot_extension_sdk-39.0.0-py3-none-any.whl` with `python -m pip install --no-deps`, substituting and quoting the actual path. The wheel supplies dependency-free type declarations; runtime classes execute inside an installed vBot host, not in the standalone authoring environment. The package metadata contains its complete build and migration guide. The host supplies `api.operations` and command-handler contexts; use these provided objects instead of constructing `ExtensionOperations` or `ExtensionCommandContext`.

Declare API 31 when using `services.exports`, `ui.conditions`, `ui.shared-preferences`, `ui.message` or `ui.editors`; native Desktop manifests use their independent API 2. API 30 manifests remain supported; upgrade the declared version when adopting these capabilities.

## Actual Identity Agents in owned Sessions

Declare `"api_version": 32` to use `host.identity_agents` or actual Identity execution. From the startup host, `identity = await host.identity_agents.get(agent_id)` returns `agent_id`, `incarnation`, `name`, `model` and `created_by_owner`; retain the exact incarnation with your membership record so a replaced Agent cannot inherit it. Create a specialist with `await host.identity_agents.create(agent_id, request_id=request_id, name=name, model=model)`; persist your generated Agent id and request id before the call, then reuse that pair and the same configuration after an interrupted creation. An exact retry returns the original identity; a different request or configuration refuses the occupied id. Configure an original specialist with `await host.identity_agents.update(agent_id, expected_incarnation=incarnation, **changes)`; updating an attached existing Agent or another Extension's specialist is refused. These are trusted server capabilities; enforce your Extension's caller, remit and configuration limits before invoking them.

Import `TemporaryAgentConfig` and `TemporaryRunInput` from `vbot_extension_sdk`. To run the actual identity, set `identity_agent=identity["agent_id"]` and `identity_incarnation=identity["incarnation"]` on `TemporaryAgentConfig`, together with its required `model=""`, absolute `cwd`, `tool_access`, `allowed_skills`, `tools` and `name`; an empty Model inherits the identity's live Model. Optional `instructions`, explicit Model/effort overrides and `permission_ceiling` apply to this owned Session. The actual Agent retains its Workspace, personal Memory, private Skills and SOUL; its ordinary Sessions receive none of your private Tools or instructions. Tool selections intersect with the identity's policy; to narrow filesystem or command access, use `permission_ceiling`, since `permission_rules` is refused in this mode. The configuration's `allowed_skills` is a hard ceiling for this owned Session and its owned Sub-Agents, including private and Project Skills. The existing `identity_profile` remains a temporary snapshot without personal context and cannot be combined with `identity_agent`.

Use `binding = await host.temporary_agents.create(group_id, participant_id, config, project_id=project_id)`, then `handle = await host.temporary_agents.open_group(group_id)` and `await host.temporary_agents.start(handle, participant_id, TemporaryRunInput("initial", task_text, request_id))`. The selected Project becomes the new Session's Working Project; `binding.address` retains the actual Identity Agent and its identity anchor, so use the returned address rather than constructing a Project-Agent address. Give each independent task or topic a new group id; continuing the same participant reuses its Session with `TemporaryRunInput("continuation", note_text, request_id)`. Keep each continuation's request id and content stable across retries; close/drain a group before adding participants, and retain the normal owned-Session receipt, shutdown and recovery handling described in [extension-pages.md](extension-pages.md).

## Product services and observations

Use the startup host's `host.services` for supported product operations. `host.services.describe()` returns `available`, service descriptors (`name`, `kind`, `parameters`, `resources`) observable resource names, declared resource schemas and documented service exclusions. Choose a returned name and its parameter schema, then call `await host.services.query(name, params)` for a query or `await host.services.execute(name, params)` for a mutation; a kind mismatch is refused. A standalone Runtime can have no application service catalog, so check `available` before using it.

For pages, declare the needed service names on the page and call `await client.services.describe()`, `query(name, params)` or `execute(name, params)` through the bridge. An undeclared service is refused; adding a service to one page does not grant it to another. These services are trusted operator integrations; a Tool implementation keeps its `ToolContext` and sanctioned Tool APIs instead of using product services to bypass Agent policy.

Subscribe with `subscription = host.services.subscribe([resource_name], callback, on_error=report_error)` to resource names returned by `describe()`. The callback receives an invalidation with `kind`, `revision` and optional `scope`; reread current state because multiple changes can coalesce and no event history is retained. Call `subscription.close()` or `subscription()` when observation is no longer needed; retirement also releases the subscription and invalidates the old host. A callback failure or timeout leaves the subscription active for later invalidations; `subscription.status` reports active/degraded/closed state, failure counts and the last sanitized diagnostic, and optional `on_error` receives that diagnostic. Observers run with their own Extension identity and can issue subsequent service calls. Keep callbacks short and move continuing work into an owned task with shutdown cleanup.

## Share services with other Extensions

During `register(api)`, declare resources with `api.register_resource("entries", description="Current entries", scope_schema={"type": "object", "properties": {"id": {"type": "string"}}, "additionalProperties": False})` and services with `api.register_service("list", "query", {"type": "object", "properties": {}, "additionalProperties": False}, list_entries, resources=("entries",))`. The handler receives validated parameters and returns a JSON object, synchronously or asynchronously; each named resource belongs to the same Extension.

Consumers discover `extension:<percent-encoded-directory-name>:list` through `describe()` and call it through their own host, while the handler executes as the service owner. Publish an owned change with `host.services.publish("entries", {"id": entry_id})`; this validates the scope and notifies subscribers of the canonical resource id. Registration validates the complete declaration set, so services and resources can be declared in either order.

Pages receive the canonical resource id in `client.onInvalidation(({change}) => ...)` as `change.resource`; the validated scope is delivered only to Python observers. For a contribution's `source.resource`, use the local resource name for its own Extension or the canonical resource id. A resource data change refreshes matching consumers without reloading the page and contribution catalogs.

Read each returned service's parameter schema before constructing arguments; validation failures identify the offending field paths without returning submitted credential values. The catalog includes product-domain services and explicitly omits transports, native bridges and secret-bearing entry points; available names come from the running host.

## Providers and Channel platforms

Register a Provider during `register(api)` with the same definition schema as `resources/providers/*.json`:

```python
def register(api):
    api.register_provider(
        {
            "id": "my-gateway",
            "name": "My Gateway",
            "adapter": "openai_compatible",
            "base_url": "http://127.0.0.1:8000/v1",
            "models_endpoint": "/models",
            "connections": [{"id": "default", "type": "none", "label": "Default"}],
        }
    )
```

The optional `adapter_type` is a concrete `vbot_extension_sdk.providers.ProviderAdapter` subclass; omit it to reuse the named adapter. Provider ids are shared with built-in and custom Providers; a collision is diagnosed and skipped. Registration and Model discovery belong to the current Runtime, so do not patch a global adapter selector.

Register a Channel platform with `api.register_channel_platform("my_platform", "My Platform", factory, settings_schema=SCHEMA)`. The synchronous `factory(config, context)` returns a `ChannelAdapter`; import `ChannelAdapter`, `ChannelConfig` and `ChannelAdapterContext` from `vbot_extension_sdk.channels`. `config.settings` contains the validated platform JSON object; the context supplies `engine`, `credential_resolver`, `attachment_store` and `state_dir`. Give the settings schema an object root; store credential references, and resolve their values through `credential_resolver` instead of putting secrets in settings. Construct the adapter without I/O, connect in `start()`, and implement the `ChannelAdapter` lifecycle and message transport contract using its existing inbound access and Session routing.

Platform ids share one catalog with built-ins; collisions are refused. Factories start after Extension startup; disable or reload retires the adapter and preserves saved Channel configuration. Configure the resulting platform through the live catalog and `--settings-file` recipe in [channels.md](channels.md).

## Local Desktop capabilities

Install native code on the computer where Desktop runs, under its config directory's `extensions/<name>/`: `%APPDATA%/vbot/extensions/` on Windows, `$XDG_CONFIG_HOME/vbot/extensions/` on Linux, or `~/.config/vbot/extensions/` when that variable is unset. Add `"desktop": {"entry": "desktop.py", "api_version": 2}` to `extension.json`; this version belongs to the Desktop API, independently of the server API version. Use `Reload local Extensions` in local Desktop sharing settings to apply changed code, assets, data files and native modules without restarting Desktop; `vbot extensions reload` updates the server side. Keep mutable state outside the installed Extension package, because changing package files changes its revision; known dependency and cache directories are excluded. Package scanning accepts up to 10,000 entries, 256 MiB of included content and 64 directory levels; a larger package is diagnosed instead of being partly fingerprinted. Reload preserves sharing choices for the same local package, and new Extensions start unshared. Live reload requires a server that supports native capability updates; otherwise apply changes at Desktop startup or reload while disconnected.

Import `DesktopExtensionAPI`, `EventSink` and `ExecutionHandler` from `vbot_extension_sdk.desktop`. The synchronous `register(api)` calls `api.register_capability(name, label, factory, description="...", available=True)`. Capability ids are `extension:<percent-encoded-directory-name>:<local-name>`; take the actual id from the capability catalog. Registration only declares the factory; `factory(emit)` creates a handler after the user grants that capability to the connected server locally. Grants start disabled, and the server cannot install native code remotely.

The handler implements async `execute(operation, params) -> dict`, `disconnected() -> None` and `close() -> None`; publish events with `emit(event_name, data_dict)`. Optional async `ready() -> str | None` returns a refusal reason or `None`; `read_only_operations` names operations safe during the native execution fence. Release connections and tasks in `disconnected` and `close`, and exercise revoke, reconnect and shutdown before reporting the capability working.

Local Desktop settings `extensions.disabled` suppress native code import. The local extension folder takes precedence over a bundled Extension with the same identity, even when the local copy is disabled or fails; remove the local override to use the bundled copy again.
