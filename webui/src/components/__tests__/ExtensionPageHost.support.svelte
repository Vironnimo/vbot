<script>
  import ExtensionPage from '../ExtensionPage.svelte';
  import { provideAutosaveContext } from '../../lib/autosave.js';
  import { provideNavigation } from '../../lib/navigation.svelte.js';
  import { provideAppChats } from '../../lib/appChats.svelte.js';

  let {
    initialDescriptor,
    initialContext = {},
    navigation = undefined,
    // The App navigator whose layers the page registers with.
    shell = null,
    onToast = () => {},
    onOpenSession = () => {},
    onOpenAgent = () => {},
    onUiCommand = null,
    embedded = false,
    autosaveContext = null,
    appChats = null,
  } = $props();
  let descriptor = $state(initialDescriptor);
  let theme = $state(initialContext.theme ?? {});
  let locale = $state(initialContext.locale ?? 'en');
  let timezone = $state(initialContext.timezone ?? 'UTC');
  let uiContext = $state(initialContext.ui ?? {});
  let apiCapabilities = $state(
    initialContext.api ?? { version: null, features: [] },
  );
  const invalidationListeners = [];
  if (autosaveContext) provideAutosaveContext(autosaveContext);
  if (shell) provideNavigation(shell);
  if (appChats) provideAppChats(appChats);

  function subscribeInvalidations(listener) {
    invalidationListeners.push(listener);
    return () =>
      invalidationListeners.splice(invalidationListeners.indexOf(listener), 1);
  }

  // Delivers one App invalidation (`{owner, change, revision}`) to the page.
  export function invalidate(invalidation) {
    for (const listener of [...invalidationListeners]) listener(invalidation);
  }

  export function update(next) {
    if ('descriptor' in next) descriptor = next.descriptor;
    if ('theme' in next) theme = next.theme;
    if ('locale' in next) locale = next.locale;
    if ('timezone' in next) timezone = next.timezone;
    if ('ui' in next) uiContext = next.ui;
    if ('api' in next) apiCapabilities = next.api;
  }
</script>

<ExtensionPage
  {descriptor}
  {navigation}
  {theme}
  {locale}
  {timezone}
  {subscribeInvalidations}
  {onToast}
  {onOpenSession}
  {onOpenAgent}
  {uiContext}
  {apiCapabilities}
  {onUiCommand}
  {embedded}
/>
