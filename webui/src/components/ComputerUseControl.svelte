<script>
  // Contextual controls inside the existing composer, with no app-wide banner.
  import { onMount } from 'svelte';
  import { extensionOperation, listExtensions } from '$lib/api.js';
  import { t } from '$lib/i18n.js';
  import Button from './ui/Button.svelte';

  let { onError = () => {} } = $props();
  let status = $state(null);
  let error = $state('');
  let stopping = $state(false);
  let disposed = false;
  let revision = 0;
  let discovered = false;

  const control = (action) =>
    extensionOperation('computer_use', 'control', {
      action,
      ...(action === 'stop' ? { call_id: status.call_id } : {}),
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
          if (disposed || requestRevision !== revision || stopping) return;
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

  async function stop() {
    const requestRevision = ++revision;
    stopping = true;
    error = '';
    try {
      const result = await control('stop');
      if (!disposed && requestRevision === revision) status = result;
    } catch (failure) {
      if (!disposed && requestRevision === revision) {
        error = failure.message;
        onError(error);
      }
    } finally {
      stopping = false;
    }
  }
</script>

{#if status?.active || stopping}
  <Button
    variant="danger"
    icon
    disabled={stopping || status?.stopping}
    ariaLabel={t('computerControl.stop', 'Stop computer control')}
    tooltip={error ||
      (stopping || status?.stopping
        ? t('computerControl.stopping', 'Stopping computer control…')
        : status.hotkey_available
          ? t(
              'computerControl.hotkey',
              'Stop computer control — press Esc twice in any app',
            )
          : t(
              'computerControl.noHotkey',
              'Global shortcut unavailable. Click to stop computer control.',
            ))}
    onClick={stop}
  >
    <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
      <rect x="1.5" y="2" width="13" height="9" rx="1" />
      <path d="M5 14h6M8 11v3M6 4.5l4 4M10 4.5l-4 4" />
    </svg>
  </Button>
{/if}
