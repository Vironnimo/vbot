<script>
  // Contextual controls inside the existing composer, with no app-wide banner.
  import { onMount } from 'svelte';
  import { extensionOperation, listExtensions } from '$lib/api.js';
  import { t } from '$lib/i18n.js';
  import Button from './ui/Button.svelte';

  let { onError = () => {} } = $props();
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
      if (!disposed && requestRevision === revision) {
        error = failure.message;
        onError(error);
      }
    } finally {
      if (action === 'stop') stopping = false;
      else resuming = false;
    }
  }
</script>

{#if status?.paused}
  <Button
    variant="secondary"
    icon
    disabled={status.active || resuming || stopping}
    ariaLabel={t('computerControl.resume', 'Allow computer control')}
    tooltip={error ||
      (status.active
        ? t('computerControl.stopping', 'Stopping computer control…')
        : t(
            'computerControl.resumeHint',
            'Computer control is stopped. Click to allow it again.',
          ))}
    onClick={() => change('resume')}
  >
    <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
      <rect x="1.5" y="2" width="13" height="9" rx="1" />
      <path d="M5 14h6M8 11v3M6.5 4.5l4 2-4 2z" />
    </svg>
  </Button>
{/if}
{#if status?.controlling || stopping || resuming}
  <Button
    variant="danger"
    icon
    disabled={stopping}
    ariaLabel={t('computerControl.stop', 'Stop computer control')}
    tooltip={error ||
      (status.hotkey_available
        ? t(
            'computerControl.hotkey',
            'Stop computer control — press Esc twice in any app',
          )
        : t(
            'computerControl.noHotkey',
            'Global shortcut unavailable. Click to stop computer control.',
          ))}
    onClick={() => change('stop')}
  >
    <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
      <rect x="1.5" y="2" width="13" height="9" rx="1" />
      <path d="M5 14h6M8 11v3M6 4.5l4 4M10 4.5l-4 4" />
    </svg>
  </Button>
{/if}
