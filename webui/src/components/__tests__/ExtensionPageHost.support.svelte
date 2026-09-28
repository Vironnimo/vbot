<script>
  import ExtensionPage from '../ExtensionPage.svelte';
  import { provideAutosaveContext } from '../../lib/autosave.js';

  let {
    initialDescriptor,
    initialContext = {},
    onRouteChange = () => {},
    onToast = () => {},
    autosaveContext = null,
  } = $props();
  let descriptor = $state(initialDescriptor);
  let route = $state(initialContext.route ?? '');
  let theme = $state(initialContext.theme ?? {});
  let locale = $state(initialContext.locale ?? 'en');
  let timezone = $state(initialContext.timezone ?? 'UTC');
  const invalidationListeners = [];
  if (autosaveContext) provideAutosaveContext(autosaveContext);

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
    if ('route' in next) route = next.route;
    if ('theme' in next) theme = next.theme;
    if ('locale' in next) locale = next.locale;
    if ('timezone' in next) timezone = next.timezone;
  }
</script>

<ExtensionPage
  {descriptor}
  {route}
  {theme}
  {locale}
  {timezone}
  {subscribeInvalidations}
  {onRouteChange}
  {onToast}
/>
