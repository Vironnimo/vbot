<script>
  import { onDestroy } from 'svelte';
  import { createExtensionPageClient } from '@vbot/extension-sdk/client';
  import { init, t } from '@vbot/extension-sdk/lib/i18n.js';
  import { applyExtensionTheme } from '@vbot/extension-sdk/lib/extensionTheme.js';
  import { setApplicationTimeZone } from '@vbot/extension-sdk/lib/dateTimePrefs.svelte.js';
  import { provideNavigation } from '@vbot/extension-sdk/lib/navigation.svelte.js';
  import Banner from '@vbot/extension-sdk/ui/Banner.svelte';
  import Button from '@vbot/extension-sdk/ui/Button.svelte';

  const client = createExtensionPageClient();
  provideNavigation({ registerLayer: client.registerLayer });
  let connected = $state(false);
  let error = $state('');
  const unsubscribe = client.onContext((context) => {
    init(context.locale);
    setApplicationTimeZone(context.timezone);
    applyExtensionTheme(context.theme);
    connected = true;
  });
  onDestroy(() => {
    unsubscribe();
    client.dispose();
  });

  async function notify() {
    error = '';
    try {
      await client.toast(t('sdkExample.notification'));
    } catch (failure) {
      error = failure.message;
    }
  }
</script>

<main>
  <h1>{t('sdkExample.title')}</h1>
  <p>{t('sdkExample.description')}</p>
  <Button disabled={!connected} onClick={notify}
    >{t('sdkExample.notify')}</Button
  >
  {#if error}<Banner>{error}</Banner>{/if}
</main>

<style>
  main {
    height: 100vh;
    overflow: auto;
    padding: 24px;
  }
</style>
