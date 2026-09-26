<script>
  import { t } from '$lib/i18n.js';
  import Button from './ui/Button.svelte';
  import { tooltip } from '$lib/tooltip.js';
  import { onMount, tick, untrack } from 'svelte';
  import {
    CONNECTION_STATUS_CONNECTED,
    CONNECTION_STATUS_RECONNECTING,
    CONNECTION_STATUS_DISCONNECTED,
  } from '$lib/connectionState.js';
  import { createDesktopContextMenu } from './shell/menu.svelte.js';
  import { voiceIndicator } from './voice/voiceLabels.js';

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
    desktopContextMenuEnabled = false,
    // Desktop Voice: whether this Desktop offers it, and its status snapshot.
    voiceAvailable = false,
    voiceStatus = null,
    onNavigateToVoiceSettings = () => {},
    onStopVoiceRecording = () => {},
    onToast = () => {},
    sidebarFooter,
    children,
  } = $props();
  const menu = createDesktopContextMenu({
    get desktopContextMenuEnabled() {
      return desktopContextMenuEnabled;
    },
    get onToast() {
      return onToast;
    },
  });

  const SIDEBAR_COLLAPSED_STORAGE_KEY = 'vbot.sidebar.collapsed.v1';

  // Phone layouts show these destinations directly in the bottom bar; every
  // other destination stays one tap away in the More sheet.
  const MOBILE_PRIMARY_VIEW_IDS = new Set([
    'chat',
    'terminals',
    'agents',
    'calendar',
  ]);

  const MOBILE_NAV_MEDIA_QUERY = '(max-width: 640px)';
  // Tablet widths keep the Main menu as the compact rail so content keeps its
  // room; the rail toggle opens the full menu as an overlay instead.
  const TABLET_NAV_MEDIA_QUERY = '(min-width: 641px) and (max-width: 960px)';

  const viewportQuery = (query) =>
    typeof window !== 'undefined' && typeof window.matchMedia === 'function'
      ? window.matchMedia(query)
      : null;

  let sidebarCollapsed = $state(false);
  let mobileNavOpen = $state(false);
  let tabletLayout = $state(
    Boolean(viewportQuery(TABLET_NAV_MEDIA_QUERY)?.matches),
  );
  let tabletMenuOpen = $state(false);
  let shellElement = $state(null);
  let navigationElement = $state(null);
  let moreButton = $state(null);

  // The persisted preference applies only from desktop width upward; tablet
  // widths always show the rail unless its overlay is open.
  const railCompact = $derived(
    tabletLayout ? !tabletMenuOpen : sidebarCollapsed,
  );

  const activeInMobileSheet = $derived(
    Boolean(activeViewId) && !MOBILE_PRIMARY_VIEW_IDS.has(activeViewId),
  );

  const sidebarToggleLabel = $derived(
    railCompact
      ? t('navigation.expandSidebar', 'Expand sidebar')
      : t('navigation.collapseSidebar', 'Collapse sidebar'),
  );

  const setSidebarCollapsed = (collapsed) => {
    sidebarCollapsed = collapsed;
    try {
      localStorage.setItem(
        SIDEBAR_COLLAPSED_STORAGE_KEY,
        collapsed ? 'true' : 'false',
      );
    } catch {
      // A blocked storage area should not prevent navigation from working.
    }
  };

  const handleSelectView = (viewId) => {
    mobileNavOpen = false;
    tabletMenuOpen = false;
    if (onSelectView) {
      onSelectView(viewId);
    }
  };

  const focusCurrentDestination = () => {
    const target =
      navigationElement?.querySelector(
        '.app-shell__nav-item[aria-current="page"]',
      ) ?? navigationElement?.querySelector('.app-shell__nav-item');
    target?.focus();
  };

  // The More sheet moves focus to the current destination (or the first one)
  // when it opens and returns it to More when dismissed from the keyboard.
  const toggleMobileNav = async () => {
    mobileNavOpen = !mobileNavOpen;
    if (!mobileNavOpen) return;
    await tick();
    focusCurrentDestination();
  };

  const sidebarToggleElement = () =>
    shellElement?.querySelector('.app-shell__sidebar-toggle') ?? null;

  // The tablet overlay behaves like the More sheet: focus moves into it on
  // open, and a keyboard dismissal returns it to the rail toggle.
  const openTabletMenu = async () => {
    tabletMenuOpen = true;
    await tick();
    focusCurrentDestination();
  };

  const closeTabletMenu = ({ restoreFocus = false } = {}) => {
    tabletMenuOpen = false;
    if (restoreFocus) sidebarToggleElement()?.focus();
  };

  const toggleSidebar = () => {
    if (!tabletLayout) {
      setSidebarCollapsed(!sidebarCollapsed);
    } else if (tabletMenuOpen) {
      closeTabletMenu();
    } else {
      void openTabletMenu();
    }
  };

  const handleWindowKeydown = (event) => {
    // A Desktop context menu sits above everything, and a floating layer that
    // consumed Escape (a pinned hint, a picker) dismisses only itself.
    if (
      event.key === 'Escape' &&
      !menu.contextMenu &&
      !event.defaultPrevented
    ) {
      if (mobileNavOpen) {
        mobileNavOpen = false;
        moreButton?.focus();
        return;
      }
      if (tabletMenuOpen) {
        closeTabletMenu({ restoreFocus: true });
        return;
      }
    }
    menu.handleWindowKeydown(event);
  };

  // Any navigation - including history and deep links - dismisses the sheet
  // and the tablet overlay.
  $effect(() => {
    void activeViewId;
    untrack(() => {
      mobileNavOpen = false;
      tabletMenuOpen = false;
    });
  });

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

  const statusIconClass = $derived(
    connectionStatus === CONNECTION_STATUS_CONNECTED
      ? 'conn-icon--connected'
      : connectionStatus === CONNECTION_STATUS_DISCONNECTED
        ? 'conn-icon--disconnected'
        : 'conn-icon--placeholder',
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

  // Voice indicator — lives in the sidebar footer so it is visible across
  // every view, not just the Chat tab. Only shown when the Desktop accessor
  // offers Desktop Voice.
  const micIndicator = $derived(voiceIndicator(voiceStatus));

  const handleMicIndicatorClick = () => {
    if (micIndicator.recording) {
      onStopVoiceRecording();
    } else {
      onNavigateToVoiceSettings();
    }
  };

  const serverRestored = $derived(serverNoticeState === 'restored');

  onMount(() => {
    try {
      sidebarCollapsed =
        localStorage.getItem(SIDEBAR_COLLAPSED_STORAGE_KEY) === 'true';
    } catch {
      // Privacy settings can disable storage; use the expanded default then.
    }

    const closeOnCapturedScroll = () => {
      if (menu.contextMenu) menu.closeContextMenu();
    };
    window.addEventListener('scroll', closeOnCapturedScroll, true);

    // Leaving the phone or tablet layout while its navigation overlay is open
    // must not leave hidden state behind (an inert content area or a
    // swallowed Escape).
    const stopWatchingMobile = watchViewport(
      MOBILE_NAV_MEDIA_QUERY,
      (matches) => {
        if (!matches) mobileNavOpen = false;
      },
    );
    const stopWatchingTablet = watchViewport(
      TABLET_NAV_MEDIA_QUERY,
      (matches) => {
        tabletLayout = matches;
        if (!matches) tabletMenuOpen = false;
      },
    );
    return () => {
      window.removeEventListener('scroll', closeOnCapturedScroll, true);
      stopWatchingMobile();
      stopWatchingTablet();
    };
  });

  function watchViewport(query, onChange) {
    const media = viewportQuery(query);
    if (!media) return () => {};
    const handleChange = (event) => onChange(event.matches);
    onChange(media.matches);
    media.addEventListener?.('change', handleChange);
    return () => media.removeEventListener?.('change', handleChange);
  }
</script>

<svelte:window
  oncontextmenu={menu.handleContextMenu}
  onpointerdown={menu.handleWindowPointerDown}
  onkeydown={handleWindowKeydown}
  onresize={() => menu.contextMenu && menu.closeContextMenu()}
  onblur={() => menu.contextMenu && menu.closeContextMenu()}
/>

<div
  bind:this={shellElement}
  class="app-shell"
  data-server-unavailable={serverUnavailable ? 'true' : undefined}
  data-sidebar-collapsed={railCompact ? 'true' : undefined}
  data-mobile-nav-open={mobileNavOpen ? 'true' : undefined}
  data-tablet-menu-open={tabletMenuOpen ? 'true' : undefined}
>
  {#if mobileNavOpen || tabletMenuOpen}
    <div
      class="app-shell__nav-backdrop"
      aria-hidden="true"
      onclick={() => {
        mobileNavOpen = false;
        tabletMenuOpen = false;
      }}
    ></div>
  {/if}

  <aside
    class="app-shell__sidebar"
    aria-label={t('navigation.primary', 'Primary navigation')}
  >
    <div class="app-shell__sidebar-header">
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
      <Button
        variant="tertiary"
        icon={true}
        class="app-shell__sidebar-toggle"
        ariaLabel={sidebarToggleLabel}
        tooltip={sidebarToggleLabel}
        aria-pressed={tabletLayout ? undefined : sidebarCollapsed}
        aria-expanded={tabletLayout ? tabletMenuOpen : undefined}
        onClick={toggleSidebar}
      >
        <svg viewBox="0 0 16 16" aria-hidden="true">
          {#if railCompact}
            <path d="m5 3.75 5.5 4.25L5 12.25" />
          {:else}
            <path d="m11 3.75-5.5 4.25 5.5 4.25" />
          {/if}
        </svg>
      </Button>
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
              class:app-shell__nav-item--mobile-secondary={!MOBILE_PRIMARY_VIEW_IDS.has(
                item.id,
              )}
              class="app-shell__nav-item"
              type="button"
              aria-current={item.id === activeViewId ? 'page' : undefined}
              aria-label={railCompact
                ? t(item.labelKey, item.labelFallback)
                : undefined}
              data-tooltip-placement="right"
              use:tooltip={railCompact
                ? t(item.labelKey, item.labelFallback)
                : ''}
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
                {:else if item.id === 'skills'}
                  <path d="M4 2h8v12l-4-2.5L4 14z" />
                {:else if item.id === 'projects'}
                  <path d="M2 12.5V4h4l1.5 1.5h6.5v7z" />
                {:else if item.id === 'jev'}
                  <path
                    d="M2 5.5V2h3.5m5 0H14v3.5m0 5V14h-3.5m-5 0H2v-3.5"
                  /><path d="m5.5 8 1.75 1.75L10.5 6.5" />
                {:else if item.id === 'calendar'}
                  <rect x="2" y="3" width="12" height="11" rx="1.5" />
                  <path d="M2 6.5h12M5.5 1.5v3m5-3v3" />
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
                {:else}
                  <path d="m7 5-3 5m5-5 3 5M5 12h6" />
                  <circle cx="8" cy="3" r="2" />
                  <circle cx="3" cy="12" r="2" />
                  <circle cx="13" cy="12" r="2" />
                {/if}
              </svg>
              <span class="app-shell__nav-label">
                {t(item.labelKey, item.labelFallback)}
              </span>
            </button>
          {/each}
        </div>
      {/each}
      <button
        bind:this={moreButton}
        class:app-shell__nav-more--active={activeInMobileSheet}
        class="app-shell__nav-more"
        type="button"
        aria-expanded={mobileNavOpen}
        onclick={toggleMobileNav}
      >
        <svg
          class="app-shell__nav-icon"
          viewBox="0 0 16 16"
          aria-hidden="true"
          style="width: 15px; height: 15px; flex-shrink: 0"
        >
          {#if mobileNavOpen}
            <path d="m4 4 8 8m0-8-8 8" />
          {:else}
            <circle cx="3.5" cy="8" r="1" />
            <circle cx="8" cy="8" r="1" />
            <circle cx="12.5" cy="8" r="1" />
          {/if}
        </svg>
        <span class="app-shell__nav-label">
          {mobileNavOpen
            ? t('common.close', 'Close')
            : t('navigation.more', 'More')}
        </span>
      </button>
    </nav>

    <div class="sidebar-footer app-shell__footer">
      {@render sidebarFooter?.()}
      {#if voiceAvailable}
        <div class="sidebar-footer__row">
          <button
            type="button"
            class="sidebar-footer__mic"
            use:tooltip={micIndicator.tooltip}
            aria-label={micIndicator.tooltip}
            onclick={handleMicIndicatorClick}
          >
            <svg
              class="mic-icon mic-icon--{micIndicator.tone}"
              viewBox="0 0 16 16"
              aria-hidden="true"
            >
              <rect x="6" y="2" width="4" height="7.5" rx="2" />
              <path d="M3.5 9.5a4.5 4.5 0 0 0 9 0" />
              <path d="M8 14v1.5" />
            </svg>
            <span class="sidebar-footer__label">{micIndicator.label}</span>
          </button>
        </div>
      {/if}
      <div class="sidebar-footer__row" aria-label={statusAriaLabel}>
        <svg
          class="conn-icon {statusIconClass}"
          viewBox="0 0 16 16"
          aria-hidden="true"
          data-tooltip-placement="right"
          use:tooltip={railCompact ? statusLabel : ''}
        >
          <path d="M5 1.5v3.5M11 1.5v3.5" />
          <rect x="3.5" y="5" width="9" height="5.5" rx="1.2" />
          <path d="M8 10.5V14" />
        </svg>
        <span class="footer-text">
          {statusLabel}
        </span>
      </div>
    </div>
  </aside>

  <main
    class="app-shell__content"
    inert={serverUnavailable || mobileNavOpen || tabletMenuOpen
      ? true
      : undefined}
  >
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

  {#if menu.contextMenu}
    <div
      bind:this={menu.contextMenuElement}
      class="desktop-context-menu"
      role="menu"
      tabindex="-1"
      aria-label={t('desktop.contextMenu.label', 'Context menu')}
      style={`left: ${menu.contextMenu.x}px; top: ${menu.contextMenu.y}px; visibility: ${menu.contextMenu.positioned ? 'visible' : 'hidden'};`}
      onkeydown={menu.handleContextMenuKeydown}
    >
      {#each menu.contextMenu.actions as action, index (action.id)}
        {#if index > 0 && menu.contextMenu.actions[index - 1].group !== action.group}
          <div class="desktop-context-menu__separator" role="separator"></div>
        {/if}
        <Button
          variant="tertiary"
          class="desktop-context-menu__item"
          role="menuitem"
          tabindex="-1"
          onClick={() => menu.handleContextMenuAction(action.id)}
        >
          {#if action.id === 'copy-link'}
            <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">
              <path
                d="M6.5 9.5 9.5 6.5M5.2 11.8l-1 .9a2.3 2.3 0 0 1-3.2-3.2l2.6-2.6a2.3 2.3 0 0 1 3.2 0M10.8 4.2l1-.9A2.3 2.3 0 0 1 15 6.5l-2.6 2.6a2.3 2.3 0 0 1-3.2 0"
              />
            </svg>
          {:else if action.id === 'open-link'}
            <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">
              <path d="M9 2h5v5M14 2 7.5 8.5" />
              <path
                d="M12.5 9.5v3a1.5 1.5 0 0 1-1.5 1.5H3.5A1.5 1.5 0 0 1 2 12.5V5a1.5 1.5 0 0 1 1.5-1.5h3"
              />
            </svg>
          {:else if action.id === 'cut'}
            <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">
              <circle cx="4" cy="12" r="2.2" />
              <circle cx="12" cy="12" r="2.2" />
              <path d="m5.8 10.7 6.4-8.2M10.2 10.7 3.8 2.5M7.1 7.8 8 9" />
            </svg>
          {:else if action.id === 'paste'}
            <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">
              <path d="M5.5 4H3.8A1.3 1.3 0 0 0 2.5 5.3v8.2h9v-2" />
              <rect x="5.5" y="2" width="5" height="3" rx="1" />
              <path d="M8 8h5.5M11 5.5 13.5 8 11 10.5" />
            </svg>
          {:else}
            <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">
              <rect x="5" y="5" width="8" height="9" rx="1.5" />
              <path
                d="M3 11H2.5A1.5 1.5 0 0 1 1 9.5v-7A1.5 1.5 0 0 1 2.5 1h7A1.5 1.5 0 0 1 11 2.5V3"
              />
            </svg>
          {/if}
          <span>{action.label}</span>
        </Button>
      {/each}
    </div>
  {/if}
</div>

<style>
  .sidebar-footer__mic {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 0;
    border: none;
    background: none;
    color: var(--text-lo);
    font-size: var(--fs-label-sm);
    text-align: left;
    cursor: pointer;
    transition: color 0.15s;
  }

  .sidebar-footer__mic:hover .sidebar-footer__label {
    color: var(--text-hi);
  }

  .sidebar-footer__mic:focus-visible {
    outline: none;
    box-shadow: var(--focus-ring);
  }

  .mic-icon {
    width: 14px;
    height: 14px;
    flex-shrink: 0;
  }

  .mic-icon--off {
    color: var(--text-lo);
  }

  .mic-icon--listening {
    animation: mic-pulse 1.6s ease-in-out infinite;
    color: var(--green);
  }

  .mic-icon--recording {
    color: var(--amber);
  }

  .mic-icon--processing {
    animation: mic-spin 1s linear infinite;
    color: var(--amber);
  }

  .mic-icon--warning {
    color: var(--amber);
  }

  .mic-icon--error {
    color: var(--red);
  }

  @keyframes mic-pulse {
    0%,
    100% {
      opacity: 1;
    }
    50% {
      opacity: 0.35;
    }
  }

  @keyframes mic-spin {
    0% {
      opacity: 1;
    }
    25% {
      opacity: 0.5;
    }
    50% {
      opacity: 0.2;
    }
    75% {
      opacity: 0.5;
    }
    100% {
      opacity: 1;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .mic-icon--listening,
    .mic-icon--processing {
      animation: none;
    }
  }
</style>
