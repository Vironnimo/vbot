# WebUI Settings

Read this reference only for WebUI Settings, Provider, Extension, Skill, Agent, onboarding, appearance, channel, logs, or Desktop Voice work. Backend policy and persistence remain in the corresponding domain maps.

## Settings shell

`SettingsView.svelte` composes the Settings surface and keeps all sections mounted as one searchable document. Section navigation and search reveal or focus existing panels; they do not create independent settings routes with separate copies of server state.

`settingsView.js` owns normalization and payload builders shared by the panels. Each panel loads and submits through `api.js`, keeps editable values local until save, and adopts server responses after mutation. Decimal settings remain strings during editing and are normalized only when building the payload.

## Providers and models

- Provider rows distinguish configured connections, usable accounts, reachability, and enabled state. Those values come from backend contracts; the UI must not infer connectivity from the presence of a masked credential or a model result.
- Connect and disconnect payloads preserve Provider id, connection id, and account id. OAuth, device-flow, API-key, environment, local, and keyless connections have different affordances and must not be collapsed into a single credential form.
- Adding a Provider means exposing a backend-advertised connection candidate. The frontend does not maintain its own Provider registry.
- Model pickers share server suitability/filtering results. A picker may retain its visible selection while a refresh is in flight, but saves the canonical model id and reconciles to the returned settings.

## Extensions, Skills, Agents, and channels

- Extension management renders the backend-provided capability, status, schema, waiting, and configuration projections. Schema forms preserve unknown/non-secret config through the backend contract; secrets use the dedicated secret operation and are never rehydrated into normal form state.
- Reload, enable/disable, and configuration changes are separate operations. Live capability ownership remains in the Extensions domain even when the panel displays its result.
- Skill and Tool selectors use the shared catalog/chip behavior and explicit scope. Creating or editing Skill content uses the Skill API; selecting an allowed Skill or Tool for an Agent or Project only changes that owner's policy.
- Agent editors preserve inheritance versus explicit override. System Prompt block editing, preview, reset, and layout changes use their dedicated backend contracts rather than treating the composed prompt as one editable blob.
- Channel forms keep platform, DM scope, allowed chat ids, enabled state, and runtime status distinct. A saved channel is not necessarily enabled or currently running.

## Onboarding and appearance

- `onboarding.js` derives operational readiness and the recommended next action from server-backed Settings, Provider connections, and the target Agent's model state. It does not persist a second readiness flag.
- Automatic onboarding is a one-shot entry into the Settings surface; users can later reopen the flow explicitly. Completing one step refreshes the underlying resources before deciding the next step.
- Provider recommendations and model-search prefills are presentation guidance. Availability and connectability still come from current backend data.
- Appearance saves language and Chat width through Settings. Changing the active language updates i18n immediately while persisted state still reconciles to the save result.

## Desktop Voice and browser boundaries

- `desktopBridge.js` is the only browser-side seam for the native desktop bridge. Web-only accessors must remain functional when no bridge exists.
- `wakewordSettings.js` separates saved Voice settings from runtime wakeword status. Payload builders submit configuration; status subscriptions report what the desktop runtime is currently doing.
- Bridge discovery, event subscriptions, and media/runtime listeners require explicit timeout and cleanup behavior. UI state must not claim the wakeword runtime is active merely because the saved setting is enabled.
- Log listing and file reads use RPC; live tailing uses the dedicated log WebSocket. Log content is display data and must not be interpreted as HTML.

## Source and tests

- Shared normalization and payloads: `webui/src/lib/settingsView.js`
- Settings composition: `webui/src/components/SettingsView.svelte`, `webui/src/components/settings/`
- Onboarding: `webui/src/lib/onboarding.js` and onboarding components under `webui/src/components/`
- Desktop Voice: `webui/src/lib/desktopBridge.js`, `webui/src/lib/wakewordSettings.js`, and the Voice settings panel
- Focused coverage: the split `webui/src/lib/__tests__/settingsView.test.*.test.js` suites, `onboarding.test.js`, `desktopBridge.test.js`, `wakewordSettings.test.js`, plus Settings and onboarding component tests under `webui/src/components/__tests__/`
