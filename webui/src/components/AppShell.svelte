<script>
  import { t } from '$lib/i18n.js';
  import {
    CONNECTION_STATUS_CONNECTED,
    CONNECTION_STATUS_RECONNECTING,
    CONNECTION_STATUS_DISCONNECTED,
  } from '$lib/connectionState.js';

  let {
    items = [],
    activeViewId,
    onSelectView,
    connectionStatus = CONNECTION_STATUS_RECONNECTING,
    children,
  } = $props();

  const handleSelectView = (viewId) => {
    if (onSelectView) {
      onSelectView(viewId);
    }
  };

  const statusDotClass = $derived(
    connectionStatus === CONNECTION_STATUS_CONNECTED
      ? 'pulse-dot'
      : connectionStatus === CONNECTION_STATUS_DISCONNECTED
        ? 'pulse-dot pulse-dot--disconnected'
        : 'pulse-dot pulse-dot--placeholder',
  );

  const statusLabel = $derived(
    connectionStatus === CONNECTION_STATUS_CONNECTED
      ? t('status.connected', 'Connected')
      : connectionStatus === CONNECTION_STATUS_DISCONNECTED
        ? t('status.notReachable', 'Not reachable')
        : t('status.reconnecting', 'Reconnecting…'),
  );

  const statusAriaLabel = $derived(
    connectionStatus === CONNECTION_STATUS_CONNECTED
      ? t('status.connected', 'Connected')
      : connectionStatus === CONNECTION_STATUS_DISCONNECTED
        ? t('status.notReachable', 'Not reachable')
        : t('status.reconnecting', 'Reconnecting…'),
  );
</script>

<div class="app-shell">
  <aside
    class="app-shell__sidebar"
    aria-label={t('navigation.primary', 'Primary navigation')}
  >
    <div class="app-shell__brand" aria-label={t('app.title', 'vBot')}>
      <span class="app-shell__brand-mark" aria-hidden="true">
        <svg viewBox="0 0 14 14" style="width: 14px; height: 14px"
          ><path d="M7 1L13 4v6l-6 3L1 10V4l6-3z" /></svg
        >
      </span>
      <div>
        <h1>{t('app.title', 'vBot')}</h1>
      </div>
    </div>

    <nav
      class="app-shell__navigation"
      aria-label={t('navigation.sections', 'Sections')}
    >
      {#each items as item (item.id)}
        <button
          class:app-shell__nav-item--active={item.id === activeViewId}
          class="app-shell__nav-item"
          type="button"
          aria-current={item.id === activeViewId ? 'page' : undefined}
          onclick={() => handleSelectView(item.id)}
        >
          <svg
            class="app-shell__nav-icon"
            viewBox="0 0 16 16"
            aria-hidden="true"
            style="width: 15px; height: 15px; flex-shrink: 0"
          >
            {#if item.id === 'chat'}
              <path d="M2 3h12v8H9l-4 3v-3H2z" />
            {:else if item.id === 'agents'}
              <circle cx="8" cy="5" r="2.5" />
              <path d="M2 14c0-3.3 2.7-6 6-6s6 2.7 6 6" />
            {:else if item.id === 'cron'}
              <circle cx="8" cy="8" r="6" />
              <path d="M8 4.5V8l2.5 2.5" />
            {:else if item.id === 'system-prompt'}
              <rect x="2" y="2" width="12" height="12" rx="2" />
              <path d="M5 6h6M5 9h4" />
            {:else if item.id === 'settings'}
              <circle cx="8" cy="8" r="2.5" />
              <path
                d="M8 1v2m0 10v2M1 8h2m10 0h2m-2.6-4.4-1.4 1.4M4 12l1.4-1.4M12 12l-1.4-1.4M4 4l1.4 1.4"
              />
            {:else if item.id === 'logs'}
              <path
                d="M3 2.5h10a.5.5 0 0 1 .5.5v10a.5.5 0 0 1-.5.5H3a.5.5 0 0 1-.5-.5V3a.5.5 0 0 1 .5-.5z"
              />
              <path d="M5 5.5h6M5 8h6M5 10.5h4" />
            {:else if item.id === 'debug'}
              <rect x="5.5" y="5" width="5" height="7.5" rx="2.5" />
              <path d="M6.5 5 5 3m4.5 2L11 3" />
              <path d="M5.5 7.5H3m2.5 3-2 1.5m7-4.5H13m-2.5 3 2 1.5" />
            {/if}
          </svg>
          <span>{t(item.labelKey, item.labelFallback)}</span>
        </button>
      {/each}
    </nav>

    <div class="sidebar-footer app-shell__footer" aria-label={statusAriaLabel}>
      <div class={statusDotClass} aria-hidden="true"></div>
      <span class="footer-text">
        {statusLabel}
      </span>
    </div>
  </aside>

  <main class="app-shell__content">
    {@render children?.()}
  </main>
</div>
