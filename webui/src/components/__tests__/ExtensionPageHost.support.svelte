<script>
  import ExtensionPage from '../ExtensionPage.svelte';
  import { provideAutosaveContext } from '../../lib/autosave.js';
  import { provideNavigation } from '../../lib/navigation.svelte.js';

  let {
    initialDescriptor,
    initialContext = {},
    navigation = undefined,
    // The App navigator whose layers the page registers with.
    shell = null,
    onToast = () => {},
    autosaveContext = null,
  } = $props();
  let descriptor = $state(initialDescriptor);
  let theme = $state(initialContext.theme ?? {});
  let locale = $state(initialContext.locale ?? 'en');
  let timezone = $state(initialContext.timezone ?? 'UTC');
  const invalidationListeners = [];
  if (autosaveContext) provideAutosaveContext(autosaveContext);
  if (shell) provideNavigation(shell);

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
/>
