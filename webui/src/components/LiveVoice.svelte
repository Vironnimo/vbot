<script>
  import { onMount, untrack } from 'svelte';
  import { tooltip } from '$lib/tooltip.js';
  import { t } from '$lib/i18n.js';
  import {
    isDesktopAccessor,
    waitForDesktopBridge,
    getWakewordStatus,
  } from '$lib/desktopBridge.js';
  import { createLiveVoice, createLiveVoiceState } from '$lib/liveVoice.js';

  let {
    configured = false,
    uiActions = {},
    serverUnavailable = false,
    wakewordEnabled = false,
    onToast = () => {},
  } = $props();

  let voice = $state(createLiveVoiceState());
  let preparing = $state(false);
  let controller;
  let audioElement;

  const running = $derived(voice.phase !== 'off');
  const caption = $derived(voice.captions.at(-1) ?? null);

  const MESSAGES = {
    not_configured: () =>
      t(
        'live.error.notConfigured',
        'Choose a Live voice Model in Settings → Voice first.',
      ),
    not_usable: () =>
      t(
        'live.error.notUsable',
        'The Live voice Model cannot be used right now. Check its Provider connection in Settings.',
      ),
    invalid_offer: () =>
      t(
        'live.error.invalidOffer',
        'The browser audio setup was rejected. Reload the page and start again.',
      ),
    access_denied: () =>
      t(
        'live.error.access',
        'The Provider rejected access to the Live voice Model. Check your account and its access to this Model.',
      ),
    rate_limited: () =>
      t(
        'live.error.rateLimited',
        'The Provider is limiting requests. Wait a moment, then start again.',
      ),
    outcome_unknown: () =>
      t(
        'live.error.unknown',
        'The Provider may have started the call, but it could not be confirmed. The request was not repeated.',
      ),
    provider_error: () =>
      t(
        'live.error.provider',
        'The Provider could not start or continue the call. Try again later.',
      ),
    control_failed: () =>
      t(
        'live.error.control',
        'vBot could not take control of the call, so it was ended. Start again.',
      ),
    microphone_denied: () =>
      t('live.error.permission', 'Allow microphone access, then start again.'),
    microphone_unavailable: () =>
      t(
        'live.error.microphone',
        'Live voice needs a working microphone and a page opened over HTTPS or localhost.',
      ),
    connection_timeout: () =>
      t(
        'live.error.timeout',
        'Live voice did not connect in time. Check your network and start again.',
      ),
    connection_failed: () =>
      t(
        'live.error.connection',
        'Live voice could not connect. Check your network and start again.',
      ),
    connection_lost: () =>
      t(
        'live.error.connectionLost',
        'The Live voice connection was lost. Start again to continue.',
      ),
    call_failed: () =>
      t(
        'live.error.callFailed',
        'Live voice stopped because of a problem. Start again to continue.',
      ),
    playback_blocked: () =>
      t(
        'live.error.playback',
        'Audio playback was blocked. Allow audio for this app and start Live again.',
      ),
    audio_unsupported: () =>
      t(
        'live.error.audioUnsupported',
        'This browser cannot play Live voice audio for this Model. Use a current browser and start again.',
      ),
    media_mismatch: () =>
      t(
        'live.error.mediaMismatch',
        'The Live voice Model changed while starting. Start again.',
      ),
    wakeword_active: () =>
      t(
        'live.error.wakeword',
        'Turn off wakeword Voice in Settings before starting Live voice.',
      ),
    desktop_unavailable: () =>
      t(
        'live.error.desktop',
        'The Desktop app did not respond. Restart it and try again.',
      ),
    ui_action_failed: () =>
      t(
        'live.error.uiAction',
        'Live voice could not change the view as requested.',
      ),
    notification_failed: () =>
      t(
        'live.error.notification',
        'Live voice could not announce a finished Run. Check the chat for its result.',
      ),
    replaced: () =>
      t('live.notice.replaced', 'Live voice continues in another window.'),
    ended: () => t('live.notice.ended', 'Live voice ended.'),
  };
  const TOAST_VARIANTS = { error: 'error', warn: 'warn', info: 'info' };

  function showNotice({ code, severity = 'error' }) {
    const message =
      MESSAGES[code]?.() ??
      t('live.error.generic', 'Live voice reported a problem ({code}).', {
        code,
      });
    onToast({
      title: t('live.title', 'Live voice'),
      message,
      variant: TOAST_VARIANTS[severity] ?? 'error',
    });
  }

  onMount(() => {
    controller = createLiveVoice({
      state: voice,
      audio: audioElement,
      uiActions,
      onNotice: showNotice,
    });
    return () => controller.destroy();
  });

  // Desktop wakeword Voice and Live voice share the microphone.
  $effect(() => {
    if (wakewordEnabled)
      untrack(() => {
        if (voice.phase === 'off') return;
        controller?.stop();
        showNotice({ code: 'wakeword_active' });
      });
  });

  $effect(() => {
    if (serverUnavailable || !configured)
      untrack(() => {
        if (voice.phase !== 'off') controller?.stop();
      });
  });

  async function startVoice() {
    if (!configured || serverUnavailable || preparing) return;
    if (wakewordEnabled) {
      showNotice({ code: 'wakeword_active' });
      return;
    }
    if (isDesktopAccessor()) {
      preparing = true;
      try {
        if (!(await waitForDesktopBridge())) {
          showNotice({ code: 'desktop_unavailable' });
          return;
        }
        if ((await getWakewordStatus())?.enabled) {
          showNotice({ code: 'wakeword_active' });
          return;
        }
      } catch {
        showNotice({ code: 'desktop_unavailable' });
        return;
      } finally {
        preparing = false;
      }
    }
    // Settings or the connection may have changed during the Desktop check.
    if (configured && !serverUnavailable && !wakewordEnabled)
      await controller.start();
  }

  const toggleLabel = $derived(
    running
      ? t('live.stopButton', 'Stop Live')
      : t('live.startButton', 'Start Live'),
  );
  const muteLabel = $derived(t('live.mute', 'Mute microphone'));
  const busyLabel = $derived(t('live.busy', 'Working…'));
  const captionText = $derived(
    voice.phase === 'connecting'
      ? t('live.state.connecting', 'Connecting…')
      : voice.phase === 'closing'
        ? t('live.state.closing', 'Stopping…')
        : (caption?.text ?? t('live.state.listening', 'Listening…')),
  );
</script>

{#if configured}
  <div class="sidebar-footer__row live-voice">
    <button
      type="button"
      class="live-voice__toggle"
      class:live-voice__toggle--active={running}
      aria-label={toggleLabel}
      use:tooltip={toggleLabel}
      disabled={voice.phase === 'closing' ||
        preparing ||
        (!running && serverUnavailable)}
      onclick={() => (running ? controller.stop() : startVoice())}
    >
      <svg viewBox="0 0 16 16" aria-hidden="true">
        {#if running}<rect x="4" y="4" width="8" height="8" rx="1" />
        {:else}<path d="M5 3.5 12 8l-7 4.5Z" />{/if}
      </svg>
      <span class="sidebar-footer__label">{toggleLabel}</span>
    </button>
    {#if running && voice.busy}
      <span
        class="live-voice__busy"
        role="status"
        aria-label={busyLabel}
        use:tooltip={busyLabel}
      ></span>
    {/if}
    {#if running}
      <button
        type="button"
        class="live-voice__mute"
        aria-label={muteLabel}
        aria-pressed={voice.muted}
        use:tooltip={muteLabel}
        disabled={voice.phase === 'closing'}
        onclick={() => controller.mute()}
      >
        <svg viewBox="0 0 16 16" aria-hidden="true">
          <rect x="6" y="2" width="4" height="7.5" rx="2" />
          <path d="M3.5 9.5a4.5 4.5 0 0 0 9 0" />
          <path d="M8 14v1.5" />
          {#if voice.muted}<path d="M2.5 2.5l11 11" />{/if}
        </svg>
      </button>
    {/if}
  </div>
  {#if running}
    <div class="sidebar-footer__row live-voice__caption-row">
      <span
        class="live-voice__caption"
        data-role={voice.phase === 'live' && caption ? caption.role : 'status'}
        use:tooltip={captionText}>{captionText}</span
      >
    </div>
  {/if}
{/if}
<audio bind:this={audioElement} hidden></audio>

<style>
  .live-voice__toggle {
    display: flex;
    flex: 1 1 auto;
    align-items: center;
    gap: 8px;
    min-width: 0;
    padding: 3px 0;
    border: 0;
    background: transparent;
    color: var(--text-med);
    font: inherit;
    font-size: var(--fs-label-sm);
    text-align: left;
    cursor: pointer;
  }
  .live-voice__toggle svg {
    width: 14px;
    height: 14px;
    flex: 0 0 14px;
    fill: currentColor;
  }
  .live-voice__toggle:hover {
    color: var(--text-hi);
  }
  /* A running Live call is a running state, so it takes amber. */
  .live-voice__toggle--active,
  .live-voice__toggle--active:hover {
    color: var(--amber);
  }
  .live-voice__toggle:disabled,
  .live-voice__mute:disabled {
    cursor: default;
  }
  .live-voice__toggle:focus-visible,
  .live-voice__mute:focus-visible {
    outline: 1px solid var(--accent);
    outline-offset: 2px;
    border-radius: var(--r-sm);
  }
  .live-voice__busy {
    flex: 0 0 6px;
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: var(--amber);
    animation: live-voice-pulse 1.2s ease-in-out infinite;
  }
  .live-voice__mute {
    display: flex;
    flex: 0 0 auto;
    align-items: center;
    justify-content: center;
    width: 20px;
    height: 20px;
    padding: 0;
    border: 0;
    background: transparent;
    color: var(--text-lo);
    cursor: pointer;
  }
  .live-voice__mute svg {
    width: 14px;
    height: 14px;
    fill: none;
    stroke: currentColor;
    stroke-linecap: round;
    stroke-width: 1.3;
  }
  .live-voice__mute:hover {
    color: var(--text-hi);
  }
  .live-voice__mute[aria-pressed='true'] {
    color: var(--red);
  }
  .live-voice__caption-row {
    padding-top: 0;
  }
  .live-voice__caption {
    flex: 1 1 auto;
    min-width: 0;
    overflow: hidden;
    color: var(--text-med);
    font-size: var(--fs-label-sm);
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .live-voice__caption[data-role='user'],
  .live-voice__caption[data-role='status'] {
    color: var(--text-lo);
  }
  :global(.app-shell[data-sidebar-collapsed='true']) .live-voice__toggle {
    flex: 0 0 auto;
    justify-content: center;
  }
  /* The compact rail and the phone status band have no room for a caption. */
  :global(.app-shell[data-sidebar-collapsed='true']) .live-voice__caption-row {
    display: none;
  }
  @media (max-width: 640px) {
    .live-voice__caption-row {
      display: none;
    }
  }
  @keyframes live-voice-pulse {
    0%,
    100% {
      opacity: 1;
    }
    50% {
      opacity: 0.3;
    }
  }
  @media (prefers-reduced-motion: reduce) {
    .live-voice__busy {
      animation: none;
    }
  }
</style>
