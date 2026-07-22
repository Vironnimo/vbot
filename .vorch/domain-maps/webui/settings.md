# WebUI Settings

Read this reference only for WebUI Settings, Provider, Extension, Skill, Agent, onboarding, appearance, channel, logs, or Desktop Voice work. Backend policy and persistence remain in the corresponding domain maps.

## Settings shell

`SettingsView.svelte` composes the Settings surface and keeps all sections mounted as one searchable document. Section navigation and search reveal or focus existing panels; they do not create independent settings routes with separate copies of server state.

The App shell retains the Settings document's reading anchor while another main view is active. `SettingsView` restores that section-relative anchor while its independently loaded panels settle; deliberate deep links replace the remembered position. Scrollspy and section navigation share a 32%-from-top reading line, and a scrollport-sized trailing spacer lets the final section reach that line.

`settingsView.js` owns normalization and payload builders shared by the panels. Each panel loads and submits through `api.js`, keeps editable values local until save, and adopts server responses after mutation. Decimal settings remain strings during editing and are normalized only when building the payload.

## Providers and models

- Provider rows distinguish configured connections, usable accounts, reachability, and enabled state. Those values come from backend contracts; the UI must not infer connectivity from the presence of a masked credential or a model result.
- Connect and disconnect payloads preserve Provider id, connection id, and account id. OAuth, device-flow, API-key, environment, local, and keyless connections have different affordances and must not be collapsed into a single credential form.
- Adding a Provider means exposing a backend-advertised connection candidate. The frontend does not maintain its own Provider registry.
- Model pickers share server suitability/filtering results. A picker may retain its visible selection while a refresh is in flight, but saves the canonical model id and reconciles to the returned settings.
- The expanded OpenRouter row owns a routing editor with a global scope and optional full per-Model overrides. `automatic` leaves OpenRouter-managed Sticky Routing eligible while permitting blocks, `allowed` restricts candidates without imposing order, and `ordered` exposes priority controls plus an explicit cache-affinity warning because OpenRouter ignores Sticky Routing when `provider.order` is present. Global blocks remain effective inside Model overrides. An always-visible info banner states that Sticky Routing is best effort and identifies one exact allowed endpoint with fallbacks off as the no-switch configuration.
- Routing Provider choices are backend-advertised through `provider.routing_options`: base slugs globally and exact endpoint tags for the selected Model. The editor remains operable with a validated custom slug when a catalog omits an endpoint, but it only auto-loads catalogs while OpenRouter has a usable Connection. Saves send the complete `providers.openrouter.routing` object and reconcile through a Settings reload.

## Extensions, Skills, Agents, and channels

- Extension management renders the backend-provided capability, status, schema, waiting, and configuration projections. Schema forms preserve unknown/non-secret config through the backend contract; secrets use the dedicated secret operation and are never rehydrated into normal form state.
- Reload, enable/disable, and configuration changes are separate operations. Live capability ownership remains in the Extensions domain even when the panel displays its result.
- Skill and Tool selectors use the shared catalog/chip behavior and explicit scope. Creating or editing Skill content uses the Skill API; selecting an allowed Skill or Tool for an Agent or Project only changes that owner's policy.
- Agent editors preserve inheritance versus explicit override. System Prompt block editing, preview, reset, and layout changes use their dedicated backend contracts rather than treating the composed prompt as one editable blob.
- Channel forms keep platform, DM scope, allowed chat ids, enabled state, and runtime status distinct. A saved channel is not necessarily enabled or currently running.

## Onboarding and appearance

- `onboarding.js` derives operational readiness and the recommended next action from server-backed Settings, Provider connections, and the target Agent's model state. Operational readiness requires at least one Connection whose server-projected `usable` value is true; a configured but disabled keyless Connection such as fresh-install `ollama:local` remains visible in Provider management but does not skip the Provider step or make Chat ask for a Model first. The frontend does not persist a second readiness flag or reconstruct usability from credentials, Account state, enablement, or reachability.
- Automatic onboarding is a one-shot entry into the Settings surface; users can later reopen the flow explicitly. Completing one step refreshes the underlying resources before deciding the next step.
- Provider recommendations and model-search prefills are presentation guidance. Availability and connectability still come from current backend data.
- Appearance saves language and Chat width through Settings. Changing the active language updates i18n immediately while persisted state still reconciles to the save result.

## Desktop app settings and browser boundaries

- `desktopBridge.js` is the only browser-side seam for the native desktop bridge. Web-only accessors must remain functional when no bridge exists.
- Desktop capability discovery independently gates `serverSelection` and `wakeword`. When server selection is available, Settings inserts a Desktop app group between Behavior and System; Connection and Voice live there rather than being presented as server-owned settings.
- `DesktopConnectionSettings.svelte` manages the Desktop-local remembered-server list through `desktopBridge.js`: `listServers` marks the active target, add/remove mutate only the OS per-user Desktop config, and a switch probes through `selectServer` before JavaScript applies the returned URL. The component is reused inside the AppShell outage modal, where it remains interactive outside the otherwise inert server-backed content.
- `wakewordSettings.js` separates saved Voice settings from runtime wakeword status. Payload builders submit the ordered one-to-two `active_model_ids`, keyed `model_sensitivities`, microphone, and routing configuration; structured values are compared and snapshotted by value so status subscriptions cannot overwrite in-progress edits.
- The Voice panel obtains the authoritative model catalog through `listWakewordModels()`. It distinguishes curated Built-ins from imported TFLite models, presents one or two simultaneous active phrases with independent sensitivity sliders, imports finished custom models through `importWakewordModel()`, and gates permanent removal of inactive imported models with the shared `ConfirmDialog`; custom training is not a WebUI responsibility.
- Imported-model paths are Desktop-private. The browser receives only the descriptor fields required for selection and management and transfers file content as base64 only for the explicit import call.
- Bridge discovery, event subscriptions, and media/runtime listeners require explicit timeout and cleanup behavior. UI state must not claim the wakeword runtime is active merely because the saved setting is enabled.
- Log listing and file reads use RPC; live tailing uses the dedicated log WebSocket. Log content is display data and must not be interpreted as HTML.

## Source and tests

- Shared normalization and payloads: `webui/src/lib/settingsView.js`
- Settings composition: `webui/src/components/SettingsView.svelte`, `webui/src/components/settings/`
- Onboarding: `webui/src/lib/onboarding.js` and onboarding components under `webui/src/components/`
- Desktop Voice: `webui/src/lib/desktopBridge.js`, `webui/src/lib/wakewordSettings.js`, and the Voice settings panel
- Focused coverage: the split `webui/src/lib/__tests__/settingsView.test.*.test.js` suites, `onboarding.test.js`, `desktopBridge.test.js`, `wakewordSettings.test.js`, plus Settings and onboarding component tests under `webui/src/components/__tests__/`; OpenRouter routing behavior is covered by `OpenRouterRoutingSettings.test.js`
