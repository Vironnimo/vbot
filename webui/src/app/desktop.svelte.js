import { createToastState, dismissToast, addToast } from '$lib/toastState.js';
import { SvelteMap } from 'svelte/reactivity';
import { CONNECTION_STATUS_DISCONNECTED } from '$lib/connectionState.js';
import {
  disabledDesktopCapabilities,
  getDesktopCapabilities,
  getVoiceStatus,
  isDesktopAccessor,
  onDesktopVoicePush,
  playVoiceCue,
  stopVoiceRecording,
  supportsDesktopVoice,
  waitForDesktopBridge,
} from '$lib/desktopBridge.js';
import { t } from '$lib/i18n.js';
import { onMount } from 'svelte';
import {
  commandFailureMessage,
  errorMessage,
} from '../components/voice/voiceLabels.js';

const TOAST_AUTO_DISMISS_MS = 3200;
const DESKTOP_BRIDGE_PROBE_TIMEOUT_MS = 1000;
const DESKTOP_CAPABILITY_RETRY_MS = 1000;
const VOICE_STATUS_RETRY_MS = 1000;

/**
 * The page's copy of the Desktop Voice status, kept current from pushes.
 *
 * Status snapshots and events share one increasing sequence. A snapshot
 * applies unless something newer was already seen; an event runs once, in
 * order, and only when it happened after the first snapshot (older events
 * belong to before this page). An event that skips a sequence number means
 * pushes were missed: the snapshot is read again; missed events are not
 * replayed. Recording events update `recording` at once, so the indicator and
 * the Live voice hold do not wait for the next status push.
 */
function createDesktopVoiceSync({ onEvent, onFirstStatus }) {
  let status = $state(null);
  // Highest sequence of an applied snapshot or processed event; null before
  // the first snapshot.
  let sequence = null;
  // Sequence of the last processed event.
  let eventSequence = null;
  let stopPushes = null;
  let refreshing = null;
  let refreshAgain = false;
  let retryTimer = null;
  let stopped = true;

  function adopt(snapshot) {
    if (stopped || !snapshot) return false;
    if (sequence !== null && snapshot.sequence < sequence) return false;
    const first = sequence === null;
    sequence = snapshot.sequence;
    if (first) eventSequence = snapshot.sequence;
    status = snapshot;
    if (first) onFirstStatus(snapshot);
    return true;
  }

  function patchRecording(event) {
    if (!status || event.sequence <= status.sequence) return;
    if (event.kind === 'recording_started' && event.command_id) {
      status = {
        ...status,
        recording: {
          command_id: event.command_id,
          model_id: event.model_id,
          agent_id: event.agent_id,
        },
      };
    } else if (
      event.kind === 'recording_ended' &&
      status.recording &&
      (!event.command_id || event.command_id === status.recording.command_id)
    ) {
      status = { ...status, recording: null };
    }
  }

  function handleEvent(event) {
    if (sequence === null || event.sequence <= eventSequence) return;
    const gap = event.sequence > sequence + 1;
    eventSequence = event.sequence;
    sequence = Math.max(sequence, event.sequence);
    patchRecording(event);
    onEvent(event);
    if (gap) void refresh();
  }

  function scheduleRetry() {
    if (stopped || retryTimer !== null) return;
    retryTimer = setTimeout(() => {
      retryTimer = null;
      void refresh();
    }, VOICE_STATUS_RETRY_MS);
  }

  /** Read the snapshot again; resolves the current status afterwards. */
  function refresh() {
    if (stopped) return Promise.resolve(status);
    if (refreshing) {
      refreshAgain = true;
      return refreshing;
    }
    refreshing = (async () => {
      try {
        do {
          refreshAgain = false;
          try {
            adopt(await getVoiceStatus());
          } catch {
            // Keep the last snapshot; the first one is retried until it loads.
            if (sequence === null) scheduleRetry();
          }
        } while (refreshAgain && !stopped);
      } finally {
        refreshing = null;
      }
      return status;
    })();
    return refreshing;
  }

  function start() {
    if (!stopped) return;
    stopped = false;
    stopPushes = onDesktopVoicePush((push) => {
      if (push.type === 'status') adopt(push.status);
      else handleEvent(push.event);
    });
    void refresh();
  }

  function stop() {
    stopped = true;
    stopPushes?.();
    stopPushes = null;
    if (retryTimer !== null) {
      clearTimeout(retryTimer);
      retryTimer = null;
    }
  }

  return {
    get status() {
      return status;
    },
    adopt,
    refresh,
    start,
    stop,
  };
}

export function createAppDesktop(context) {
  let toastState = $state(createToastState());

  let desktopCapabilities = $state(null);

  const toastDismissTimers = new SvelteMap();

  const clearToastDismissTimer = (id) => {
    const timer = toastDismissTimers.get(id);
    if (!timer) {
      return;
    }

    clearTimeout(timer);
    toastDismissTimers.delete(id);
  };

  const clearToastDismissTimers = () => {
    for (const timer of toastDismissTimers.values()) {
      clearTimeout(timer);
    }
    toastDismissTimers.clear();
  };

  const dismissAppToast = (id) => {
    clearToastDismissTimer(id);
    dismissToast(toastState, id);
  };

  const showToast = ({
    title,
    message = '',
    variant = 'info',
    autoDismiss,
  }) => {
    // A disconnected server is already represented by the global availability
    // notice. Suppress dependent action/load errors so one transport failure
    // cannot flood the active view with duplicate symptoms.
    if (
      variant === 'error' &&
      context.connectionState.status === CONNECTION_STATUS_DISCONNECTED
    ) {
      return;
    }

    const id = addToast(toastState, { title, message, variant });
    // Error toasts stay until the user dismisses them (a transport/server
    // failure the user must acknowledge); success/info/warn auto-dismiss. An
    // explicit `autoDismiss` from the caller always wins over this default.
    const effectiveAutoDismiss =
      autoDismiss === undefined ? variant !== 'error' : autoDismiss;
    if (!effectiveAutoDismiss) {
      return;
    }

    const timer = setTimeout(() => {
      dismissToast(toastState, id);
      toastDismissTimers.delete(id);
    }, TOAST_AUTO_DISMISS_MS);
    toastDismissTimers.set(id, timer);
  };

  const handleStopVoiceRecording = () => {
    // Fire-and-forget: status pushes reconcile the indicator, and a failed
    // bridge call leaves the recording running rather than losing it.
    void stopVoiceRecording().catch(() => {});
  };

  const showVoiceErrorToast = (errorCode) => {
    showToast({
      title: t('settings.voice.errorTitle', 'Voice needs attention'),
      message: errorMessage(
        errorCode,
        t(
          'voice.toast.errorMessage',
          'Open Voice settings for details. The failure was written to the Desktop log.',
        ),
      ),
      variant: 'error',
    });
  };

  // Feedback for one Voice event that happened while this page was open.
  const handleVoiceEvent = (event) => {
    void playVoiceCue(event.kind);
    switch (event.kind) {
      case 'sent':
        showToast({
          title: t('voice.toast.sentTitle', 'Voice command sent'),
          variant: 'success',
        });
        break;
      case 'no_speech':
        showToast({
          title: t('voice.toast.noSpeechTitle', 'No speech heard'),
          message: t(
            'voice.toast.noSpeechMessage',
            'No command followed the wake phrase. Try again and speak after the cue.',
          ),
          variant: 'warn',
        });
        break;
      case 'transcription_failed':
        showToast({
          title: t(
            'voice.toast.transcriptionFailedTitle',
            'Voice command could not be transcribed',
          ),
          message: t(
            'voice.toast.transcriptionFailedMessage',
            'Check the Speech-to-text Model and the Desktop log, then try again.',
          ),
          variant: 'error',
        });
        break;
      case 'command_failed':
        showToast({
          title: t('voice.toast.commandFailedTitle', 'Voice command not sent'),
          message: commandFailureMessage(event.error_code),
          variant: 'error',
        });
        break;
      case 'microphone_disconnected':
        showToast({
          title: t(
            'voice.toast.microphoneDisconnectedTitle',
            'Microphone disconnected',
          ),
          message: t(
            'voice.toast.microphoneDisconnectedMessage',
            'Wake phrases are not heard until the microphone is back. The Desktop keeps trying to reconnect it.',
          ),
          variant: 'warn',
          autoDismiss: true,
        });
        break;
      case 'error':
        showVoiceErrorToast(event.error_code);
        break;
      default:
        break;
    }
  };

  const voice = createDesktopVoiceSync({
    onEvent: handleVoiceEvent,
    // A fatal startup failure is still current, not historical feedback:
    // surface it even when Voice failed before this page finished mounting.
    onFirstStatus: (status) => {
      if (status.state === 'error') showVoiceErrorToast(status.error_code);
    },
  });

  const voiceAvailable = $derived(supportsDesktopVoice(desktopCapabilities));

  // The Voice owner the settings panel edits against: the current snapshot,
  // `adopt(snapshot)` for snapshots a bridge call returned, and `refresh()`.
  const desktopVoice = {
    get available() {
      return voiceAvailable;
    },
    get status() {
      return voiceAvailable ? voice.status : null;
    },
    adopt: (snapshot) => voice.adopt(snapshot),
    refresh: () => voice.refresh(),
  };

  onMount(() => {
    let cancelled = false;
    let desktopCapabilityRetryTimer = null;
    const scheduleDesktopCapabilityRetry = () => {
      if (cancelled || desktopCapabilityRetryTimer !== null) return;
      desktopCapabilityRetryTimer = setTimeout(() => {
        desktopCapabilityRetryTimer = null;
        void initializeDesktopCapabilities();
      }, DESKTOP_CAPABILITY_RETRY_MS);
    };

    const initializeDesktopCapabilities = async () => {
      try {
        const ready = await waitForDesktopBridge(
          DESKTOP_BRIDGE_PROBE_TIMEOUT_MS,
        );
        if (cancelled) return;
        if (!ready) {
          scheduleDesktopCapabilityRetry();
          return;
        }
        const caps = await getDesktopCapabilities();
        if (cancelled) return;
        desktopCapabilities = caps;
        if (supportsDesktopVoice(caps)) voice.start();
      } catch {
        scheduleDesktopCapabilityRetry();
      }
    };

    // Detect desktop capabilities and keep probing while the asynchronously
    // injected bridge is absent or temporarily rejects a capability call.
    if (isDesktopAccessor()) {
      void initializeDesktopCapabilities();
    } else {
      desktopCapabilities = disabledDesktopCapabilities();
    }

    return () => {
      cancelled = true;
      clearToastDismissTimers();
      if (desktopCapabilityRetryTimer !== null) {
        clearTimeout(desktopCapabilityRetryTimer);
        desktopCapabilityRetryTimer = null;
      }
      voice.stop();
    };
  });
  return {
    get toastState() {
      return toastState;
    },
    get desktopCapabilities() {
      return desktopCapabilities;
    },
    get desktopVoice() {
      return desktopVoice;
    },
    get voiceAvailable() {
      return voiceAvailable;
    },
    get voiceStatus() {
      return desktopVoice.status;
    },
    get dismissAppToast() {
      return dismissAppToast;
    },
    get showToast() {
      return showToast;
    },
    get handleStopVoiceRecording() {
      return handleStopVoiceRecording;
    },
  };
}
