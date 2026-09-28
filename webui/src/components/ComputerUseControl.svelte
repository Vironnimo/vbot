<script>
  // Contextual controls inside the existing composer, with no app-wide banner.
  import { onMount } from 'svelte';
  import { extensionOperation, listExtensions } from '$lib/api.js';
  import { t } from '$lib/i18n.js';
  import Button from './ui/Button.svelte';

  const EXTENSION = 'computer_use';

  let { onError = () => {}, subscribeInvalidations = null } = $props();
  let status = $state(null);
  let error = $state('');
  let stopping = $state(false);
  let disposed = false;
  let revision = 0;
  let discovered = false;
  // One refresh runs at a time; a request arriving meanwhile runs once more
  // afterwards, so the last read always follows the newest change.
  let refreshing = false;
  let refreshQueued = false;
  let rediscover = false;

  const control = (action) =>
    extensionOperation(EXTENSION, 'control', {
      action,
      ...(action === 'stop' ? { call_id: status.call_id } : {}),
    });

  // Computer Use publishes a change whenever its control status changes, so
  // the status is read only then. An invalidation without an owner (reconnect,
  // Extension reload, enable or disable) also checks that it is still loaded.
  function onInvalidation({ owner }) {
    if (owner == null) refresh({ discover: true });
    else if (owner === EXTENSION) refresh();
  }

  function refresh({ discover = false } = {}) {
    if (discover) rediscover = true;
    refreshQueued = true;
    if (!refreshing && !stopping && !disposed) void drain();
  }

  async function drain() {
    refreshing = true;
    try {
      while (refreshQueued && !stopping && !disposed) {
        refreshQueued = false;
        await load();
      }
    } finally {
      refreshing = false;
    }
  }

  async function load() {
    const requestRevision = revision;
    try {
      if (rediscover || !discovered) {
        rediscover = false;
        const catalog = await listExtensions();
        if (disposed || requestRevision !== revision) return;
        discovered = catalog.extensions.some(
          (item) => item.name === EXTENSION && item.status === 'loaded',
        );
        if (!discovered) {
          // An Extension that is not loaded runs no call.
          status = null;
          error = '';
          return;
        }
      }
      const result = await control('status');
      if (disposed || requestRevision !== revision || stopping) return;
      status = result;
      error = '';
    } catch (failure) {
      if (!disposed && requestRevision === revision) {
        discovered = false;
        if (status) error = failure.message;
      }
    }
  }

  $effect(() => subscribeInvalidations?.(onInvalidation));

  onMount(() => {
    refresh({ discover: true });
    return () => {
      disposed = true;
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
      // Read the changes published while the stop was in flight.
      if (refreshQueued && !refreshing && !disposed) void drain();
    }
  }
</script>

{#if status?.active || stopping}
  <Button
    variant="danger"
    icon
    disabled={stopping || status?.stopping}
    ariaLabel={t('computerControl.stop')}
    tooltip={error ||
      (stopping || status?.stopping
        ? t('computerControl.stopping')
        : status.hotkey_available
          ? t('computerControl.hotkey')
          : t('computerControl.noHotkey'))}
    onClick={stop}
  >
    <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
      <rect x="1.5" y="2" width="13" height="9" rx="1" />
      <path d="M5 14h6M8 11v3M6 4.5l4 4M10 4.5l-4 4" />
    </svg>
  </Button>
{/if}
