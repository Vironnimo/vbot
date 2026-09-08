<script>
  import ExtensionPage from '../ExtensionPage.svelte';

  let {
    initialDescriptor,
    initialContext = {},
    onRouteChange = () => {},
    onToast = () => {},
  } = $props();
  let descriptor = $state(initialDescriptor);
  let route = $state(initialContext.route ?? '');
  let theme = $state(initialContext.theme ?? {});
  let locale = $state(initialContext.locale ?? 'en');
  let timezone = $state(initialContext.timezone ?? 'UTC');
  let invalidation = $state(initialContext.invalidation ?? null);

  export function update(next) {
    if ('descriptor' in next) descriptor = next.descriptor;
    if ('route' in next) route = next.route;
    if ('theme' in next) theme = next.theme;
    if ('locale' in next) locale = next.locale;
    if ('timezone' in next) timezone = next.timezone;
    if ('invalidation' in next) invalidation = next.invalidation;
  }
</script>

<ExtensionPage
  {descriptor}
  {route}
  {theme}
  {locale}
  {timezone}
  {invalidation}
  {onRouteChange}
  {onToast}
/>
