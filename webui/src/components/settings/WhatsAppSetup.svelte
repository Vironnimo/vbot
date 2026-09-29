<script>
  import { onMount, onDestroy } from 'svelte';
  import { getWhatsAppStatus, setupWhatsApp, pairWhatsApp } from '$lib/api.js';
  import { t } from '$lib/i18n.js';
  import Button from '../ui/Button.svelte';
  import Banner from '../ui/Banner.svelte';
  import InfoHint from '../ui/InfoHint.svelte';

  // `hideWhenConnected` lets the Channel row drop a finished setup from its
  // collapsed view; errors and unfinished steps always show.
  let { channelId, onChanged = () => {}, hideWhenConnected = false } = $props();
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
  let connected = $derived(
    Boolean(status?.installed) && status?.state === 'connected',
  );
  let failure = $derived(error || status?.error || '');
  let visible = $derived(
    Boolean(failure) || (status !== null && !(hideWhenConnected && connected)),
  );

  onMount(() => {
    refresh();
  });
  onDestroy(() => {
    disposed = true;
    ++revision;
    clearTimeout(timer);
  });
</script>

{#if visible}
  <div class="whatsapp-setup">
    {#if failure}<Banner variant="error">{failure}</Banner>{/if}
    {#if status}
      <div class="whatsapp-setup__status">
        {#if status.setup === 'installing'}
          <p role="status">{t('settings.channels.whatsapp.installing')}</p>
        {:else if !status.installed}
          <p>{t('settings.channels.whatsapp.notInstalled')}</p>
        {:else if status.state === 'connected'}
          <p role="status">{t('settings.channels.whatsapp.connected')}</p>
        {:else if status.qr_image}
          <p>{t('settings.channels.whatsapp.scan')}</p>
        {:else}
          <p role="status">{t('settings.channels.whatsapp.waiting')}</p>
        {/if}
        <InfoHint
          text={t('settings.channels.whatsapp.help')}
          ariaLabel={t('settings.channels.whatsapp.helpAria')}
        />
      </div>
      {#if !connected}
        <p class="whatsapp-setup__risk">
          {t('settings.channels.whatsapp.risk')}
        </p>
      {/if}
      <!-- While installing, polling reports the end; once connected, nothing
           is left to do. -->
      {#if status.setup !== 'installing' && !connected}
        {#if !status.installed}
          <Button disabled={busy} onClick={() => act('setup')}
            >{t('settings.channels.whatsapp.install')}</Button
          >
        {:else if status.qr_image}
          <img
            src={status.qr_image}
            alt={t('settings.channels.whatsapp.qr')}
            width="256"
            height="256"
          />
        {:else}
          <div class="whatsapp-setup__actions">
            <Button disabled={busy} onClick={() => act('pair')}
              >{t('settings.channels.whatsapp.connect')}</Button
            >
            <Button
              variant="tertiary"
              disabled={busy}
              onClick={() => act('pair', true)}
              >{t('settings.channels.whatsapp.repair')}</Button
            >
          </div>
        {/if}
      {/if}
    {/if}
  </div>
{/if}

<style>
  /* Setup steps for a WhatsApp Channel, shown under its row head in the
     Channel list: one status line with its help, the account risk while not
     connected, then the next action. */
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
  .whatsapp-setup__status {
    display: flex;
    align-items: center;
    gap: 6px;
  }
  .whatsapp-setup p.whatsapp-setup__risk {
    color: var(--text-lo);
    font-size: var(--fs-label-sm);
  }
  .whatsapp-setup__actions {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-sm);
  }
  .whatsapp-setup img {
    max-width: 100%;
    height: auto;
    background: white;
  }
</style>
