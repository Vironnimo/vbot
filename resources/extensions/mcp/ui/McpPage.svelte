<script>
  import { onMount, untrack } from 'svelte';
  import { createExtensionPageClient } from '@vbot/extension-sdk/client';
  import { init, t } from '@vbot/extension-sdk/lib/i18n.js';
  import { setApplicationTimeZone } from '@vbot/extension-sdk/lib/dateTimePrefs.svelte.js';
  import { applyExtensionTheme } from '@vbot/extension-sdk/lib/extensionTheme.js';
  import { provideNavigation } from '@vbot/extension-sdk/lib/navigation.svelte.js';
  import EmbeddedSettingsPage from '@vbot/extension-sdk/ui/EmbeddedSettingsPage.svelte';
  import SettingsMcpPanel from './SettingsMcpPanel.svelte';

  let { bridgeClient = null } = $props();
  const client = untrack(() => bridgeClient ?? createExtensionPageClient());
  let ready = $state(false);
  let agentsRefreshToken = $state(0);
  provideNavigation({ registerLayer: client.registerLayer });

  function applyContext(context) {
    if (!context) return;
    document.documentElement.lang = init(context.locale);
    setApplicationTimeZone(context.timezone);
    applyExtensionTheme(context.theme);
    ready = true;
  }

  const subscribeInvalidations = (listener) =>
    client.onInvalidation((event) => {
      listener({ owner: event.change ? 'mcp' : null, change: event.change });
    });

  onMount(() => {
    const stopContext = client.onContext(applyContext);
    const stopChanges = client.onInvalidation((event) => {
      if (!event.change || event.change.resource === 'agents')
        agentsRefreshToken += 1;
    });
    applyContext(client.context);
    return () => {
      stopContext();
      stopChanges();
      client.dispose();
    };
  });
</script>

<svelte:head><title>{t('mcp.title')}</title></svelte:head>
<EmbeddedSettingsPage {client}>
  {#if ready}
    <SettingsMcpPanel {client} {subscribeInvalidations} {agentsRefreshToken} />
  {/if}
</EmbeddedSettingsPage>
