<script>
  import { t } from '$lib/i18n.js';
  import Button from './ui/Button.svelte';
  import { tooltip } from '$lib/tooltip.js';
  import { onMount } from 'svelte';
  import {
    CONNECTION_STATUS_CONNECTED,
    CONNECTION_STATUS_RECONNECTING,
    CONNECTION_STATUS_DISCONNECTED,
  } from '$lib/connectionState.js';
  import { createDesktopContextMenu } from './shell/menu.svelte.js';

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
    wakewordStatus = { enabled: false, state: 'off' },
    desktopCapabilities = null,
    onNavigateToVoiceSettings = () => {},
    onStopWakewordRecording = () => {},
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

  const MOBILE_NAV_MEDIA_QUERY = '(max-width: 640px)';

  const SIDEBAR_COLLAPSED_STORAGE_KEY = 'vbot.sidebar.collapsed.v1';

  let navigationElement = $state(null);

  let sidebarCollapsed = $state(false);

  const sidebarToggleLabel = $derived(
    sidebarCollapsed
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

  // Wakeword indicator — lives in the sidebar footer so it is visible across
  // every view, not just the Chat tab.  Only shown when the Desktop accessor
  // advertises wakeword capability.
  const micVisible = $derived(Boolean(desktopCapabilities?.wakeword));
  const micIconClass = $derived(computeMicIconClass(wakewordStatus));
  const micTooltip = $derived(computeMicTooltip(wakewordStatus));
  const micStatusLabel = $derived(computeMicStatusLabel(wakewordStatus));
  const micRecording = $derived(wakewordStatus?.state === 'recording');

  const handleMicIndicatorClick = () => {
    if (micRecording) {
      onStopWakewordRecording();
    } else {
      onNavigateToVoiceSettings();
    }
  };

  function computeMicIconClass(status) {
    if (status?.state === 'error') {
      return 'mic-icon--error';
    }
    if (!status?.enabled) {
      return 'mic-icon--off';
    }
    switch (status.state) {
      case 'starting':
        return 'mic-icon--processing';
      case 'listening':
      case 'wakeword_detected':
        return 'mic-icon--listening';
      case 'recording':
        return 'mic-icon--recording';
      case 'transcribing':
      case 'sending':
        return 'mic-icon--processing';
      case 'sent':
        return 'mic-icon--listening';
      case 'cancelled':
      case 'no_speech':
      case 'transcription_failed':
      case 'microphone_disconnected':
        return 'mic-icon--warning';
      case 'error':
        return 'mic-icon--error';
      default:
        return 'mic-icon--off';
    }
  }

  function computeMicTooltip(status) {
    if (status?.state === 'error') {
      return t('voice.mic.tooltip.error', 'Voice error');
    }
    if (!status?.enabled) {
      return t('voice.mic.tooltip.off', 'Wakeword disabled');
    }
    switch (status.state) {
      case 'starting':
        return t('voice.mic.tooltip.starting', 'Starting wakeword listening');
      case 'listening':
        return t('voice.mic.tooltip.listening', 'Listening for wakeword');
      case 'wakeword_detected':
        return t('voice.mic.tooltip.detected', 'Wakeword detected');
      case 'recording':
        return t(
          'voice.mic.tooltip.recording',
          'Recording — click to stop and send',
        );
      case 'transcribing':
      case 'sending':
        return t('voice.mic.tooltip.processing', 'Processing voice command');
      case 'sent':
        return t('voice.mic.tooltip.sent', 'Voice command sent');
      case 'cancelled':
        return t('voice.mic.tooltip.cancelled', 'Voice command cancelled');
      case 'no_speech':
        return t('voice.mic.tooltip.noSpeech', 'No speech heard');
      case 'transcription_failed':
        return t(
          'voice.mic.tooltip.transcriptionFailed',
          'Voice command was not understood',
        );
      case 'microphone_disconnected':
        return t(
          'voice.mic.tooltip.microphoneDisconnected',
          'Microphone disconnected',
        );
      case 'error':
        return t('voice.mic.tooltip.error', 'Voice error');
      default:
        return t('voice.mic.tooltip.off', 'Wakeword disabled');
    }
  }

  function computeMicStatusLabel(status) {
    if (status?.state === 'error') {
      return t('voice.state.error', 'Voice error');
    }
    if (!status?.enabled) {
      return t('voice.state.off', 'Disabled');
    }
    switch (status.state) {
      case 'starting':
        return t('voice.state.starting', 'Starting');
      case 'listening':
        return t('voice.state.listening', 'Listening');
      case 'wakeword_detected':
        return t('voice.state.wakewordDetected', 'Wakeword detected');
      case 'recording':
        return t('voice.state.recording', 'Recording');
      case 'transcribing':
        return t('voice.state.transcribing', 'Transcribing');
      case 'sending':
        return t('voice.state.sending', 'Sending');
      case 'sent':
        return t('voice.state.sent', 'Sent');
      case 'cancelled':
        return t('voice.state.cancelled', 'Cancelled');
      case 'no_speech':
        return t('voice.state.no_speech', 'No speech heard');
      case 'transcription_failed':
        return t('voice.state.transcription_failed', 'Not understood');
      case 'microphone_disconnected':
        return t(
          'voice.state.microphone_disconnected',
          'Microphone disconnected',
        );
      case 'error':
        return t('voice.state.error', 'Voice error');
      default:
        return t('voice.state.off', 'Disabled');
    }
  }

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
    return () =>
      window.removeEventListener('scroll', closeOnCapturedScroll, true);
  });

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

<svelte:window
  oncontextmenu={menu.handleContextMenu}
  onpointerdown={menu.handleWindowPointerDown}
  onkeydown={menu.handleWindowKeydown}
  onresize={() => menu.contextMenu && menu.closeContextMenu()}
  onblur={() => menu.contextMenu && menu.closeContextMenu()}
/>

<div
  class="app-shell"
  data-server-unavailable={serverUnavailable ? 'true' : undefined}
  data-sidebar-collapsed={sidebarCollapsed ? 'true' : undefined}
>
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
        aria-pressed={sidebarCollapsed}
        onClick={() => setSidebarCollapsed(!sidebarCollapsed)}
      >
        <svg viewBox="0 0 16 16" aria-hidden="true">
          {#if sidebarCollapsed}
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
              class="app-shell__nav-item"
              type="button"
              aria-current={item.id === activeViewId ? 'page' : undefined}
              aria-label={sidebarCollapsed
                ? t(item.labelKey, item.labelFallback)
                : undefined}
              use:tooltip={sidebarCollapsed
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
    </nav>

    <div class="sidebar-footer app-shell__footer">
      {@render sidebarFooter?.()}
      {#if micVisible}
        <div class="sidebar-footer__row">
          <button
            type="button"
            class="sidebar-footer__mic"
            use:tooltip={micTooltip}
            aria-label={micTooltip}
            onclick={handleMicIndicatorClick}
          >
            <svg
              class="mic-icon {micIconClass}"
              viewBox="0 0 16 16"
              aria-hidden="true"
            >
              <rect x="6" y="2" width="4" height="7.5" rx="2" />
              <path d="M3.5 9.5a4.5 4.5 0 0 0 9 0" />
              <path d="M8 14v1.5" />
            </svg>
            <span class="sidebar-footer__label">{micStatusLabel}</span>
          </button>
        </div>
      {/if}
      <div class="sidebar-footer__row" aria-label={statusAriaLabel}>
        <svg
          class="conn-icon {statusIconClass}"
          viewBox="0 0 16 16"
          aria-hidden="true"
          use:tooltip={sidebarCollapsed ? statusLabel : ''}
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
    font-family: var(--font-mono);
    font-size: 11px;
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
    color: var(--accent);
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
