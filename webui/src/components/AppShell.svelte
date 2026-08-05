<script>
  import Button from './ui/Button.svelte';
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
    serverUnavailable = false,
    serverNoticeState = '',
    showServerNotice = true,
    onRetryConnection = () => {},
    canSwitchServer = false,
    onSwitchServer = () => {},
    children,
  } = $props();

  const MOBILE_NAV_MEDIA_QUERY = '(max-width: 640px)';
  let navigationElement = $state(null);

  const handleSelectView = (viewId) => {
    if (onSelectView) {
      onSelectView(viewId);
    }
  };

  // The sidebar groups navigation by usage cadence. Order and membership come
  // from each item's `section` field (set in App.svelte); a group with no
  // visible items renders neither its label nor its gap.
  const NAV_SECTIONS = [
    { id: 'work', labelKey: 'nav.section.work', labelFallback: 'Work' },
    {
      id: 'configure',
      labelKey: 'nav.section.configure',
      labelFallback: 'Configure',
    },
    {
      id: 'insights',
      labelKey: 'nav.section.insights',
      labelFallback: 'Insights',
    },
  ];

  const navGroups = $derived(
    NAV_SECTIONS.map((section) => ({
      ...section,
      items: items.filter((item) => item.section === section.id),
    })).filter((group) => group.items.length > 0),
  );

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

  const serverRestored = $derived(serverNoticeState === 'restored');

  // A direct mobile deep-link can activate an item outside the initially
  // visible part of the horizontal navigation. Reveal it after Svelte has
  // updated aria-current, without moving the page on wider layouts.
  $effect(() => {
    void activeViewId;
    if (!navigationElement) {
      return undefined;
    }

    const frame = requestAnimationFrame(() => {
      if (
        typeof window.matchMedia !== 'function' ||
        !window.matchMedia(MOBILE_NAV_MEDIA_QUERY).matches
      ) {
        return;
      }

      const activeItem = navigationElement.querySelector(
        '[aria-current="page"]',
      );
      if (typeof activeItem?.scrollIntoView === 'function') {
        activeItem.scrollIntoView({ block: 'nearest', inline: 'nearest' });
      }
    });

    return () => cancelAnimationFrame(frame);
  });
</script>

<div
  class="app-shell"
  data-server-unavailable={serverUnavailable ? 'true' : undefined}
>
  <aside
    class="app-shell__sidebar"
    aria-label={t('navigation.primary', 'Primary navigation')}
  >
    <div class="app-shell__brand" aria-label={t('app.title', 'vBot')}>
      <img
        class="app-shell__brand-mark"
        src="/brand/vbot-mark-transparent.png"
        alt=""
        width="30"
        height="30"
      />
      <div>
        <h1>{t('app.title', 'vBot')}</h1>
      </div>
    </div>

    <nav
      bind:this={navigationElement}
      class="app-shell__navigation"
      aria-label={t('navigation.sections', 'Sections')}
    >
      {#each navGroups as group (group.id)}
        <div
          class="app-shell__nav-group"
          role="group"
          aria-label={t(group.labelKey, group.labelFallback)}
        >
          <span class="app-shell__nav-group-label" aria-hidden="true">
            {t(group.labelKey, group.labelFallback)}
          </span>
          {#each group.items as item (item.id)}
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
                {:else if item.id === 'terminals'}
                  <rect x="1.5" y="2.5" width="13" height="11" rx="1.5" />
                  <path d="m4 6 2 2-2 2m4 0h3.5" />
                {:else if item.id === 'projects'}
                  <path d="M2 12.5V4h4l1.5 1.5h6.5v7z" />
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
                {:else if item.id === 'statistics'}
                  <path d="M2.5 13.5h11" />
                  <path d="M4.5 13.5V10.5M8 13.5V8M11.5 13.5V5" />
                {:else if item.id === 'debug'}
                  <rect x="5.5" y="5" width="5" height="7.5" rx="2.5" />
                  <path d="M6.5 5 5 3m4.5 2L11 3" />
                  <path d="M5.5 7.5H3m2.5 3-2 1.5m7-4.5H13m-2.5 3 2 1.5" />
                {/if}
              </svg>
              <span>{t(item.labelKey, item.labelFallback)}</span>
            </button>
          {/each}
        </div>
      {/each}
    </nav>

    <div class="sidebar-footer app-shell__footer" aria-label={statusAriaLabel}>
      <div class={statusDotClass} aria-hidden="true"></div>
      <span class="footer-text">
        {statusLabel}
      </span>
    </div>
  </aside>

  <main class="app-shell__content" inert={serverUnavailable ? true : undefined}>
    {@render children?.()}
  </main>

  {#if serverNoticeState && showServerNotice}
    <aside
      class:server-availability-notice--restored={serverRestored}
      class="server-availability-notice"
      role={serverRestored ? 'status' : 'alert'}
      aria-live={serverRestored ? 'polite' : 'assertive'}
      aria-atomic="true"
    >
      <span class="server-availability-notice__signal" aria-hidden="true">
        <span></span>
      </span>
      <div class="server-availability-notice__content">
        <p class="server-availability-notice__eyebrow">
          {serverRestored
            ? t('status.connectionRestored', 'Connection restored')
            : t('status.connectionInterrupted', 'Connection interrupted')}
        </p>
        <h2>
          {serverRestored
            ? t('status.serverRestoredTitle', 'Server is reachable again')
            : t('status.serverUnavailableTitle', 'Server is not reachable')}
        </h2>
        <p class="server-availability-notice__message">
          {serverRestored
            ? t(
                'status.serverRestoredMessage',
                'The current view has been refreshed.',
              )
            : t(
                'status.serverUnavailableMessage',
                'vBot is trying to restore the connection automatically.',
              )}
        </p>
        {#if !serverRestored}
          <details class="server-availability-notice__details">
            <summary>{t('common.details', 'Details')}</summary>
            <p>
              {t(
                'status.serverUnavailableDetails',
                'The browser connection to the vBot server was interrupted. Features that need the server are temporarily unavailable.',
              )}
            </p>
          </details>
        {/if}
      </div>
      {#if !serverRestored}
        <div class="server-availability-notice__actions">
          <Button variant="secondary" onClick={onRetryConnection}>
            {t('status.retryNow', 'Retry now')}
          </Button>
          {#if canSwitchServer}
            <Button variant="primary" onClick={onSwitchServer}>
              {t('status.switchServer', 'Switch server')}
            </Button>
          {/if}
        </div>
      {/if}
    </aside>
  {/if}
</div>
