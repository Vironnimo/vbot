<script>
  import { onMount, onDestroy } from 'svelte';
  import { getWhatsAppStatus, setupWhatsApp, pairWhatsApp } from '$lib/api.js';
  import { t } from '$lib/i18n.js';
  import Button from '../ui/Button.svelte';
  import Banner from '../ui/Banner.svelte';

  let { channelId, onChanged = () => {} } = $props();
  let status = $state(null);
  let error = $state('');
  let busy = $state(false);
  let timer;
  let disposed = false;
  let revision = 0;

  function schedule() {
    clearTimeout(timer);
    if (!disposed && !busy) timer = setTimeout(refresh, 3000);
  }
  async function refresh() {
    const request = ++revision;
    try {
      const next = await getWhatsAppStatus(channelId);
      if (!disposed && request === revision) {
        status = next;
        error = '';
      }
    } catch (failure) {
      if (!disposed && request === revision) error = failure.message;
    } finally {
      schedule();
    }
  }
  async function act(action, reset = false) {
    busy = true;
    error = '';
    ++revision;
    clearTimeout(timer);
    try {
      const next =
        action === 'setup'
          ? await setupWhatsApp(channelId)
          : await pairWhatsApp(channelId, reset);
      if (!disposed) {
        status = next;
        onChanged();
      }
    } catch (failure) {
      if (!disposed) error = failure.message;
    } finally {
      busy = false;
      schedule();
    }
  }
  onMount(() => {
    refresh();
  });
  onDestroy(() => {
    disposed = true;
    ++revision;
    clearTimeout(timer);
  });
</script>

<div class="whatsapp-setup">
  <p>
    {t('settings.channels.whatsapp.help')}
  </p>
  {#if error || status?.error}<Banner variant="error"
      >{error || status.error}</Banner
    >{/if}
  {#if status?.setup === 'installing'}
    <p role="status">
      {t('settings.channels.whatsapp.installing')}
    </p>
  {:else if status && !status.installed}
    <Button disabled={busy} onClick={() => act('setup')}
      >{t('settings.channels.whatsapp.install')}</Button
    >
  {:else if status?.state === 'connected'}
    <p role="status">
      {t('settings.channels.whatsapp.connected')}
    </p>
  {:else if status?.qr_image}
    <p>
      {t('settings.channels.whatsapp.scan')}
    </p>
    <img
      src={status.qr_image}
      alt={t('settings.channels.whatsapp.qr')}
      width="256"
      height="256"
    />
  {:else if status?.installed}
    <p role="status">
      {t('settings.channels.whatsapp.waiting')}
    </p>
    <Button disabled={busy} onClick={() => act('pair')}
      >{t('settings.channels.whatsapp.connect')}</Button
    >
    <Button variant="tertiary" disabled={busy} onClick={() => act('pair', true)}
      >{t('settings.channels.whatsapp.repair')}</Button
    >
  {/if}
</div>

<style>
  /* Setup steps for a WhatsApp Channel, shown inside its row in the Channel
     list. */
  .whatsapp-setup {
    display: grid;
    gap: var(--space-sm);
    justify-items: start;
  }
  .whatsapp-setup p {
    max-width: 66ch;
    margin: 0;
    color: var(--text-med);
    font-size: var(--fs-body-sm);
    line-height: 1.5;
  }
  .whatsapp-setup img {
    max-width: 100%;
    height: auto;
    background: white;
  }
</style>
