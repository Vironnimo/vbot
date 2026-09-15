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
    {t(
      'settings.channels.whatsapp.help',
      'Link your existing WhatsApp account and talk to vBot in your self chat. Other conversations cannot trigger Runs. This uses an unofficial connection; WhatsApp may restrict the account. Node.js 22 or newer is required on the vBot server.',
    )}
  </p>
  {#if error || status?.error}<Banner variant="error"
      >{error || status.error}</Banner
    >{/if}
  {#if status?.setup === 'installing'}
    <p role="status">
      {t(
        'settings.channels.whatsapp.installing',
        'Installing WhatsApp support…',
      )}
    </p>
  {:else if status && !status.installed}
    <Button disabled={busy} onClick={() => act('setup')}
      >{t(
        'settings.channels.whatsapp.install',
        'Install WhatsApp support',
      )}</Button
    >
  {:else if status?.state === 'connected'}
    <p role="status">
      {t(
        'settings.channels.whatsapp.connected',
        'WhatsApp connected. Send a message to yourself to talk to your Agent.',
      )}
    </p>
  {:else if status?.qr_image}
    <p>
      {t(
        'settings.channels.whatsapp.scan',
        'In WhatsApp, open Settings → Linked devices → Link a device, then scan this QR code.',
      )}
    </p>
    <img
      src={status.qr_image}
      alt={t(
        'settings.channels.whatsapp.qr',
        'WhatsApp device linking QR code',
      )}
      width="256"
      height="256"
    />
  {:else if status?.installed}
    <p role="status">
      {t(
        'settings.channels.whatsapp.waiting',
        'Connect to show a QR code or restore your linked device.',
      )}
    </p>
    <Button disabled={busy} onClick={() => act('pair')}
      >{t('settings.channels.whatsapp.connect', 'Connect WhatsApp')}</Button
    >
    <Button variant="tertiary" disabled={busy} onClick={() => act('pair', true)}
      >{t(
        'settings.channels.whatsapp.repair',
        'Link again with a new QR code',
      )}</Button
    >
  {/if}
</div>

<style>
  .whatsapp-setup {
    display: grid;
    gap: 0.75rem;
    justify-items: start;
    padding-top: 0.75rem;
  }
  .whatsapp-setup p {
    margin: 0;
  }
  .whatsapp-setup img {
    max-width: 100%;
    height: auto;
    background: white;
  }
</style>
