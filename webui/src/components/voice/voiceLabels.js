import { t } from '$lib/i18n.js';
import { desktopErrorCode } from '$lib/desktopBridge.js';

/**
 * Presentation of one Voice status snapshot, shared by the sidebar indicator
 * and the Voice settings status row.
 *
 * `tone` is one of off | listening | recording | processing | warning | error
 * (the CSS modifier of both indicators); `recording` tells whether a command
 * recording runs that the user can stop. Priority: an error, then a running
 * recording, a lost microphone, commands being transcribed or sent, starting,
 * listening, off.
 */
export function voiceIndicator(status) {
  if (status?.state === 'error') {
    return indicator(
      'error',
      t('voice.state.error'),
      t('voice.mic.tooltip.error'),
    );
  }
  if (!status?.enabled || status.state === 'off') {
    return indicator('off', t('voice.state.off'), t('voice.mic.tooltip.off'));
  }
  if (status.recording) {
    return {
      ...indicator(
        'recording',
        t('voice.state.recording'),
        t('voice.mic.tooltip.recording'),
      ),
      recording: true,
    };
  }
  if (status.state === 'microphone_disconnected') {
    return indicator(
      'warning',
      t('voice.state.microphone_disconnected'),
      t('voice.mic.tooltip.microphoneDisconnected'),
    );
  }
  const command = status.commands?.at(-1);
  if (command) {
    return indicator(
      'processing',
      command.stage === 'transcribing'
        ? t('voice.state.transcribing')
        : command.stage === 'sending'
          ? t('voice.state.sending')
          : t('voice.state.processing'),
      t('voice.mic.tooltip.processing'),
    );
  }
  if (status.state === 'starting') {
    return indicator(
      'processing',
      t('voice.state.starting'),
      t('voice.mic.tooltip.starting'),
    );
  }
  return indicator(
    'listening',
    t('voice.state.listening'),
    t('voice.mic.tooltip.listening'),
  );
}

function indicator(tone, label, tooltip) {
  return { tone, label, tooltip, recording: false };
}

/**
 * Explanation of one Voice error or phrase problem code; `fallback` (or a
 * generic text) for codes without their own explanation.
 */
export function errorMessage(code, fallback = null) {
  return (
    knownErrorMessage(code) || fallback || t('settings.voice.error.unknown')
  );
}

/**
 * Toast message of a rejected Desktop bridge call: the explanation of its
 * error code, else its own message. A code without an explanation leaves the
 * toast with its generic title only.
 */
export function bridgeErrorMessage(error) {
  const code = desktopErrorCode(error);
  if (code === null) return error?.message || '';
  return knownErrorMessage(code) ?? '';
}

function knownErrorMessage(code) {
  const messages = {
    no_server: t('settings.voice.error.noServer'),
    server_unreachable: t('settings.voice.error.serverUnreachable'),
    speech_to_text_unconfigured: t(
      'settings.voice.error.speechToTextUnconfigured',
    ),
    speech_to_text_unavailable: t(
      'settings.voice.error.speechToTextUnavailable',
    ),
    speech_to_text_readiness_failed: t(
      'settings.voice.error.speechToTextReadiness',
    ),
    missing_target_agent: t('settings.voice.error.missingTarget'),
    target_agent_unavailable: t('settings.voice.error.targetUnavailable'),
    engine_start_failed: t('settings.voice.error.engine'),
    wakeword_model_unavailable: t('settings.voice.error.modelUnavailable'),
    wakeword_model_invalid: t('settings.voice.error.modelInvalid'),
    wakeword_model_active: t('settings.voice.error.modelActive'),
    wakeword_model_delete_failed: t('settings.voice.error.modelDeleteFailed'),
    calibration_unavailable: t('settings.voice.error.calibrationUnavailable'),
    calibration_inactive: t('settings.voice.error.calibrationInactive'),
    microphone_unavailable: t('settings.voice.error.microphone'),
    microphone_read_failed: t('settings.voice.error.microphoneRead'),
    recording_interrupted: t('settings.voice.error.recordingInterrupted'),
    detection_failed: t('settings.voice.error.detection'),
    pipeline_failed: t('settings.voice.error.pipeline'),
    session_resolution_failed: t('settings.voice.error.session'),
    send_failed: t('settings.voice.error.send'),
    voice_config_invalid: t('settings.voice.error.configInvalid'),
    voice_stack_unavailable: t('settings.voice.error.stackUnavailable'),
  };
  return Object.hasOwn(messages, code) ? messages[code] : null;
}

/** Explanation of a failed voice command, by its `command_failed` code. */
export function commandFailureMessage(code) {
  return errorMessage(code, t('voice.toast.commandFailedMessage'));
}
