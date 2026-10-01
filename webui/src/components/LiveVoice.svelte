<script>
  import { onMount, untrack } from 'svelte';
  import { tooltip } from '$lib/tooltip.js';
  import { t } from '$lib/i18n.js';
  import {
    desktopMicrophoneAccess,
    isDesktopAccessor,
    onDesktopLiveRequest,
  } from '$lib/desktopBridge.js';
  import { createLiveVoice, createLiveVoiceState } from '$lib/liveVoice.js';
  import { microphoneInUse, onMicrophoneUse } from '$lib/microphoneUse.js';
  import { liveWakePhrases } from '$lib/wakewordSettings.js';
  import LiveActivityPanel from './voice/LiveActivityPanel.svelte';

  // The hold reasons while Desktop Voice records a spoken command and while
  // a recording in this page (dictation) uses the microphone.
  const VOICE_COMMAND_HOLD = 'wakeword';
  const RECORDING_HOLD = 'recording';
  // The provider's session limit is announced this long before it ends the call.
  const EXPIRY_WARNING_MS = 120_000;

  let {
    configured = false,
    uiActions = {},
    serverUnavailable = false,
    // The Desktop Voice status snapshot, or null without Desktop Voice.
    voiceStatus = null,
    onToast = () => {},
    // Called with whether a call is starting, live or stopping.
    onRunningChange = () => {},
  } = $props();

  let voice = $state(createLiveVoiceState());
  let controller;
  let audioElement;
  let activityButton = $state();
  let activityOpen = $state(false);
  let recording = $state(microphoneInUse());
  // Ticks each second while a call runs, for its time and warnings.
  let clock = $state(Date.now());

  const running = $derived(voice.phase !== 'off');
  const caption = $derived(voice.captions.at(-1) ?? null);
  const hasActivity = $derived(
    voice.captions.length > 0 || voice.actions.length > 0,
  );
  const callTime = $derived(
    voice.phase === 'live' && voice.liveSince
      ? formatDuration(clock - voice.liveSince)
      : '',
  );
  const idleLeft = $derived(
    voice.phase === 'live' && voice.idleEndsAt
      ? formatDuration(voice.idleEndsAt - clock)
      : '',
  );
  const expiryLeft = $derived(
    voice.phase === 'live' &&
      voice.expiresAt &&
      voice.expiresAt - clock <= EXPIRY_WARNING_MS
      ? formatDuration(voice.expiresAt - clock)
      : '',
  );

  function formatDuration(milliseconds) {
    const seconds = Math.max(0, Math.round(milliseconds / 1000));
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const rest = String(seconds % 60).padStart(2, '0');
    return hours
      ? `${hours}:${String(minutes).padStart(2, '0')}:${rest}`
      : `${minutes}:${rest}`;
  }

  $effect(() => {
    if (!running) return;
    clock = Date.now();
    const timer = setInterval(() => {
      clock = Date.now();
    }, 1000);
    return () => clearInterval(timer);
  });

  // Leaving the page would end the call; the browser asks first. The Desktop
  // app closes its window without asking.
  $effect(() => {
    if (!running || isDesktopAccessor()) return;
    const warn = (event) => {
      event.preventDefault();
      event.returnValue = '';
    };
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  });

  $effect(() => {
    const isRunning = running;
    untrack(() => onRunningChange(isRunning));
  });

  const MESSAGES = {
    not_configured: () => t('live.error.notConfigured'),
    provider_unavailable: () => t('live.error.providerUnavailable'),
    backend_unavailable: () => t('live.error.backendUnavailable'),
    not_usable: () => t('live.error.notUsable'),
    invalid_offer: () => t('live.error.invalidOffer'),
    access_denied: () => t('live.error.access'),
    rate_limited: () => t('live.error.rateLimited'),
    outcome_unknown: () => t('live.error.unknown'),
    provider_error: () => t('live.error.provider'),
    control_failed: () => t('live.error.control'),
    microphone_denied: () => t('live.error.permission'),
    microphone_unavailable: () => t('live.error.microphone'),
    connection_timeout: () => t('live.error.timeout'),
    connection_failed: () => t('live.error.connection'),
    connection_lost: () => t('live.error.connectionLost'),
    call_failed: () => t('live.error.callFailed'),
    playback_blocked: () => t('live.error.playback'),
    audio_unsupported: () => t('live.error.audioUnsupported'),
    media_mismatch: () => t('live.error.mediaMismatch'),
    desktop_restart_required: () => t('live.error.desktopRestart'),
    ui_action_failed: () => t('live.error.uiAction'),
    notification_failed: () => t('live.error.notification'),
    link_failed: () => t('live.error.link'),
    replaced: () => t('live.notice.replaced'),
    ended: () => t('live.notice.ended'),
    hung_up: () => t('live.notice.hungUp'),
    idle: () => t('live.notice.idle'),
    expired: () => t('live.notice.expired'),
    idle_warning: () => t('live.notice.idleWarning'),
  };
  const TOAST_VARIANTS = { error: 'error', warn: 'warn', info: 'info' };

  function showNotice({ code, severity = 'error' }) {
    const message =
      MESSAGES[code]?.() ??
      t('live.error.generic', {
        code,
      });
    onToast({
      title: t('live.title'),
      message,
      variant: TOAST_VARIANTS[severity] ?? 'error',
    });
  }

  onMount(() => {
    // In the Desktop app, a wake phrase or the global shortcut can start Live,
    // and the call learns which wake phrases address other Agents.
    const desktop = isDesktopAccessor();
    controller = createLiveVoice({
      state: voice,
      audio: audioElement,
      uiActions,
      onNotice: showNotice,
      checkMicrophoneAccess: desktop ? () => desktopMicrophoneAccess() : null,
      wakePhrases: () => liveWakePhrases(voiceStatus),
    });
    controller.reportContext(untrack(() => uiActions.context?.() ?? null));
    const stopDesktopRequests = desktop
      ? onDesktopLiveRequest(handleDesktopRequest)
      : () => {};
    const stopMicrophoneUse = onMicrophoneUse((inUse) => {
      recording = inUse;
    });
    return () => {
      stopDesktopRequests();
      stopMicrophoneUse();
      controller.destroy();
    };
  });

  // `start` starts a call when none runs; `toggle` also stops a running one.
  // Both come from a Live voice wake phrase or the global shortcut.
  function handleDesktopRequest({ action }) {
    if (serverUnavailable) return false;
    if (!configured) {
      showNotice({ code: 'not_configured' });
      return true;
    }
    if (voice.phase === 'off') void startVoice();
    else if (action === 'toggle' && voice.phase !== 'closing')
      controller.stop();
    return true;
  }

  // A running call learns what the app shows as it changes, without asking.
  $effect(() => {
    const context = uiActions.context?.() ?? null;
    untrack(() => controller?.reportContext(context));
  });

  // A lost app connection does not end the call: the call has its own
  // socket, which reattaches or fails the call by itself.
  $effect(() => {
    if (!configured)
      untrack(() => {
        if (voice.phase !== 'off') controller?.stop();
      });
  });

  // While Desktop Voice records a spoken command, the running call is held:
  // the command does not reach the Live voice Model and the assistant stays
  // silent. A snapshot without a recording releases it, also after a missed
  // event.
  $effect(() => {
    syncHold(VOICE_COMMAND_HOLD, Boolean(voiceStatus?.recording));
  });

  // Likewise while a recording in this page, such as a dictation in the chat
  // composer, uses the microphone.
  $effect(() => {
    syncHold(RECORDING_HOLD, recording);
  });

  function syncHold(reason, wanted) {
    const active = voice.phase !== 'off' && voice.phase !== 'closing';
    untrack(() => {
      if (!controller) return;
      const holding = controller.held(reason);
      if (wanted && active && !holding) controller.hold(reason);
      else if (!wanted && holding) controller.release(reason);
    });
  }

  async function startVoice() {
    if (!configured || serverUnavailable) return;
    await controller.start();
  }

  const toggleLabel = $derived(
    running ? t('live.stopButton') : t('live.startButton'),
  );
  const muteLabel = $derived(t('live.mute'));
  const speakerLabel = $derived(
    voice.speakerMuted ? t('live.speakerUnmute') : t('live.speakerMute'),
  );
  const activityLabel = $derived(
    activityOpen ? t('live.activity.hide') : t('live.activity.show'),
  );
  const busyLabel = $derived(t('live.busy'));
  // A warning outranks the caption; `status` text is the app's, not speech.
  const captionText = $derived(
    voice.phase === 'connecting'
      ? t('live.state.connecting')
      : voice.phase === 'closing'
        ? t('live.state.closing')
        : idleLeft
          ? t('live.state.idle', { time: idleLeft })
          : expiryLeft
            ? t('live.state.expiring', { time: expiryLeft })
            : voice.held
              ? recording && !voiceStatus?.recording
                ? t('live.state.heldRecording')
                : t('live.state.held')
              : (caption?.text ?? t('live.state.listening')),
  );
  const captionRole = $derived(
    idleLeft || expiryLeft
      ? 'warning'
      : voice.phase === 'live' && caption && !voice.held
        ? caption.role
        : 'status',
  );
</script>

{#if configured}
  <div class="sidebar-footer__row live-voice">
    <button
      type="button"
      class="live-voice__toggle"
      class:live-voice__toggle--active={running}
      aria-label={toggleLabel}
      use:tooltip={{ text: toggleLabel, placement: 'right' }}
      disabled={voice.phase === 'closing' || (!running && serverUnavailable)}
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
        use:tooltip={{ text: busyLabel, placement: 'right' }}
      ></span>
    {/if}
    {#if running || hasActivity}
      <button
        type="button"
        class="live-voice__icon"
        bind:this={activityButton}
        aria-label={activityLabel}
        aria-expanded={activityOpen}
        use:tooltip={{ text: activityLabel, placement: 'right' }}
        onclick={() => (activityOpen = !activityOpen)}
      >
        <svg viewBox="0 0 16 16" aria-hidden="true">
          <path d="M3 4h10M3 8h10M3 12h6" />
        </svg>
      </button>
    {/if}
    {#if running}
      <button
        type="button"
        class="live-voice__icon"
        aria-label={speakerLabel}
        aria-pressed={voice.speakerMuted}
        use:tooltip={{ text: speakerLabel, placement: 'right' }}
        disabled={voice.phase === 'closing'}
        onclick={() => controller.muteSpeaker()}
      >
        <svg viewBox="0 0 16 16" aria-hidden="true">
          <path d="M3 6h2.5L9 3v10L5.5 10H3Z" />
          {#if voice.speakerMuted}<path d="M11 6l3 4M14 6l-3 4" />
          {:else}<path d="M11 6a3 3 0 0 1 0 4" />{/if}
        </svg>
      </button>
      <button
        type="button"
        class="live-voice__icon live-voice__mute"
        aria-label={muteLabel}
        aria-pressed={voice.muted}
        use:tooltip={{
          text: voice.muted ? t('live.unmute') : muteLabel,
          placement: 'right',
        }}
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
        data-role={captionRole}
        use:tooltip={{
          text: captionText,
          placement: 'right',
          whenTruncated: true,
        }}>{captionText}</span
      >
      {#if idleLeft}
        <button
          type="button"
          class="live-voice__stay"
          onclick={() => controller.stay()}>{t('live.stay')}</button
        >
      {:else if callTime}
        <span
          class="live-voice__time"
          aria-label={t('live.callTime', { time: callTime })}>{callTime}</span
        >
      {/if}
    </div>
  {/if}
  {#if activityOpen && (running || hasActivity)}
    <LiveActivityPanel
      {voice}
      anchor={activityButton}
      {uiActions}
      onClose={() => (activityOpen = false)}
      onLinkFailed={() => showNotice({ code: 'link_failed', severity: 'warn' })}
    />
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
  .live-voice__icon:disabled {
    cursor: default;
  }
  .live-voice__toggle:focus-visible,
  .live-voice__icon:focus-visible,
  .live-voice__stay:focus-visible {
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
  .live-voice__icon {
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
  .live-voice__icon svg {
    width: 14px;
    height: 14px;
    fill: none;
    stroke: currentColor;
    stroke-linecap: round;
    stroke-width: 1.3;
  }
  .live-voice__icon:hover,
  .live-voice__icon[aria-expanded='true'] {
    color: var(--text-hi);
  }
  .live-voice__icon[aria-pressed='true'] {
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
  .live-voice__caption[data-role='warning'] {
    color: var(--amber);
  }
  .live-voice__time {
    flex: 0 0 auto;
    color: var(--text-lo);
    font-size: var(--fs-label-sm);
    font-variant-numeric: tabular-nums;
  }
  .live-voice__stay {
    flex: 0 0 auto;
    padding: 0 6px;
    border: 1px solid var(--amber);
    border-radius: var(--r-sm);
    background: transparent;
    color: var(--amber);
    font: inherit;
    font-size: var(--fs-label-sm);
    cursor: pointer;
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
