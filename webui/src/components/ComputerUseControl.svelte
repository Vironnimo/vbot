<script>
  // Ongoing operator control has its own lifecycle, separate from pending Extension questions.
  import { onMount } from 'svelte';
  import { extensionOperation, listExtensions } from '$lib/api.js';
  import { t } from '$lib/i18n.js';
  import Banner from './ui/Banner.svelte';
  import Button from './ui/Button.svelte';

  let status = $state(null);
  let error = $state('');
  let resuming = $state(false);
  let stopping = $state(false);
  let disposed = false;
  let revision = 0;
  let discovered = false;

  const control = (action) =>
    extensionOperation('computer_use', 'control', {
      action,
      ...(action === 'resume' ? { stop_token: status?.stop_token } : {}),
    });

  onMount(() => {
    let timer;
    async function poll() {
      const requestRevision = revision;
      try {
        if (!discovered) {
          const catalog = await listExtensions();
          if (disposed || requestRevision !== revision) return;
          discovered = catalog.extensions.some(
            (item) => item.name === 'computer_use' && item.status === 'loaded',
          );
        }
        if (discovered) {
          const result = await control('status');
          if (disposed || requestRevision !== revision || resuming || stopping)
            return;
          status = result;
          error = '';
        }
      } catch (failure) {
        if (!disposed && requestRevision === revision) {
          discovered = false;
          if (status) error = failure.message;
        }
      } finally {
        if (!disposed) timer = setTimeout(poll, discovered ? 2000 : 10000);
      }
    }
    void poll();
    return () => {
      disposed = true;
      clearTimeout(timer);
    };
  });

  async function change(action) {
    const requestRevision = ++revision;
    if (action === 'stop') stopping = true;
    else resuming = true;
    error = '';
    try {
      const result = await control(action);
      if (!disposed && requestRevision === revision) status = result;
    } catch (failure) {
      if (!disposed && requestRevision === revision) error = failure.message;
    } finally {
      if (action === 'stop') stopping = false;
      else resuming = false;
    }
  }
</script>

{#if status?.available || status?.paused}
  <Banner variant={status.paused ? 'warn' : 'neutral'}>
    <div class="computer-status">
      <span role="status"
        >{status.paused
          ? status.active
            ? t('computerControl.stopping', 'Stopping computer control…')
            : t('computerControl.paused', 'Computer control is stopped.')
          : status.active
            ? t('computerControl.active', 'Agent is using the computer.')
            : t('computerControl.ready', 'Computer control is allowed.')}</span
      >
      <small
        >{status.hotkey_available
          ? t('computerControl.hotkey', 'Emergency stop: Ctrl+Alt+Pause')
          : t(
              'computerControl.noHotkey',
              'Global shortcut unavailable. Use Stop here.',
            )}</small
      >
      {#if error}<span role="alert">{error}</span>{/if}
    </div>
    <div class="computer-actions">
      {#if status.paused}
        <Button
          variant="secondary"
          disabled={status.active || resuming || stopping}
          onClick={() => change('resume')}
        >
          {t('computerControl.resume', 'Allow computer control')}
        </Button>
      {/if}
      <Button
        variant="danger"
        disabled={stopping}
        onClick={() => change('stop')}
      >
        {t('computerControl.stop', 'Stop computer control')}
      </Button>
    </div>
  </Banner>
{/if}

<style>
  .computer-status {
    display: grid;
    gap: 4px;
    min-width: 0;
    overflow-wrap: anywhere;
  }
  .computer-status small {
    color: var(--text-secondary);
  }
  .computer-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }
</style>
