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
      t('voice.state.error', 'Voice error'),
      t('voice.mic.tooltip.error', 'Voice error'),
    );
  }
  if (!status?.enabled || status.state === 'off') {
    return indicator(
      'off',
      t('voice.state.off', 'Disabled'),
      t('voice.mic.tooltip.off', 'Wakeword disabled'),
    );
  }
  if (status.recording) {
    return {
      ...indicator(
        'recording',
        t('voice.state.recording', 'Recording'),
        t('voice.mic.tooltip.recording', 'Recording — click to stop and send'),
      ),
      recording: true,
    };
  }
  if (status.state === 'microphone_disconnected') {
    return indicator(
      'warning',
      t('voice.state.microphone_disconnected', 'Microphone disconnected'),
      t('voice.mic.tooltip.microphoneDisconnected', 'Microphone disconnected'),
    );
  }
  const command = status.commands?.at(-1);
  if (command) {
    return indicator(
      'processing',
      command.stage === 'transcribing'
        ? t('voice.state.transcribing', 'Transcribing')
        : command.stage === 'sending'
          ? t('voice.state.sending', 'Sending')
          : t('voice.state.processing', 'Processing'),
      t('voice.mic.tooltip.processing', 'Processing voice command'),
    );
  }
  if (status.state === 'starting') {
    return indicator(
      'processing',
      t('voice.state.starting', 'Starting'),
      t('voice.mic.tooltip.starting', 'Starting wakeword listening'),
    );
  }
  return indicator(
    'listening',
    t('voice.state.listening', 'Listening'),
    t('voice.mic.tooltip.listening', 'Listening for wake phrases'),
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
    knownErrorMessage(code) ||
    fallback ||
    t(
      'settings.voice.error.unknown',
      'Voice stopped unexpectedly. Retry listening or restart the Desktop app.',
    )
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
    no_server: t(
      'settings.voice.error.noServer',
      'Voice has no active server. Connect the Desktop app to a server and try again.',
    ),
    server_unreachable: t(
      'settings.voice.error.serverUnreachable',
      'Voice could not reach the active server. Check the Desktop connection and try again.',
    ),
    speech_to_text_unconfigured: t(
      'settings.voice.error.speechToTextUnconfigured',
      'Configure a Speech-to-text Model under Settings → Voice to send voice commands.',
    ),
    speech_to_text_unavailable: t(
      'settings.voice.error.speechToTextUnavailable',
      'The configured Speech-to-text Model is not currently usable. Check its Provider connection or choose another Model under Settings → Voice.',
    ),
    speech_to_text_readiness_failed: t(
      'settings.voice.error.speechToTextReadiness',
      'Voice could not verify the Speech-to-text configuration. Check the Desktop log and try again.',
    ),
    missing_target_agent: t(
      'settings.voice.error.missingTarget',
      'Choose an Agent for this phrase or a default Agent for this server.',
    ),
    target_agent_unavailable: t(
      'settings.voice.error.targetUnavailable',
      'The chosen Agent no longer exists on this server. Choose another Agent.',
    ),
    engine_start_failed: t(
      'settings.voice.error.engine',
      'The on-device wakeword model could not start. Restart the Desktop app and try again.',
    ),
    wakeword_model_unavailable: t(
      'settings.voice.error.modelUnavailable',
      'The selected wakeword model is no longer available. Choose another model or import it again.',
    ),
    wakeword_model_invalid: t(
      'settings.voice.error.modelInvalid',
      'The wakeword model is not a compatible pyopen-wakeword TFLite model.',
    ),
    wakeword_model_active: t(
      'settings.voice.error.modelActive',
      'This wake phrase is active. Deactivate it before removing its model.',
    ),
    wakeword_model_delete_failed: t(
      'settings.voice.error.modelDeleteFailed',
      'The Desktop could not remove this wakeword model. Check the Desktop log and try again.',
    ),
    calibration_unavailable: t(
      'settings.voice.error.calibrationUnavailable',
      'Calibration needs Voice listening with this wake phrase active. Wait until Voice is listening, then try again.',
    ),
    calibration_inactive: t(
      'settings.voice.error.calibrationInactive',
      'No calibration is running anymore. Start the calibration again.',
    ),
    microphone_unavailable: t(
      'settings.voice.error.microphone',
      'No compatible microphone is available. Connect a microphone or choose another input device, then retry.',
    ),
    microphone_read_failed: t(
      'settings.voice.error.microphoneRead',
      'The microphone stopped responding. Check the device connection and retry.',
    ),
    detection_failed: t(
      'settings.voice.error.detection',
      'Wakeword detection stopped unexpectedly. Retry listening.',
    ),
    pipeline_failed: t(
      'settings.voice.error.pipeline',
      'The Voice pipeline stopped unexpectedly. Retry listening or restart the Desktop app.',
    ),
    session_resolution_failed: t(
      'settings.voice.error.session',
      'vBot could not open the target Agent Session. Check the server connection and retry.',
    ),
    send_failed: t(
      'settings.voice.error.send',
      'The spoken command could not be sent. Check the server connection and retry.',
    ),
    voice_config_invalid: t(
      'settings.voice.error.configInvalid',
      'The Desktop rejected this Voice setting. Reload Voice settings and try again.',
    ),
    voice_stack_unavailable: t(
      'settings.voice.error.stackUnavailable',
      'The Desktop Voice components are unavailable. Install the desktop Voice dependencies and restart vBot.',
    ),
  };
  return Object.hasOwn(messages, code) ? messages[code] : null;
}

/** Explanation of a failed voice command, by its `command_failed` code. */
export function commandFailureMessage(code) {
  return errorMessage(
    code,
    t(
      'voice.toast.commandFailedMessage',
      'The voice command could not be sent. The failure was written to the Desktop log.',
    ),
  );
}
