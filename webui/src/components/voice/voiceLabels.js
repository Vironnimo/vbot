import { t } from '$lib/i18n.js';

export function liveStateText(state) {
  if (state === 'wakeword_detected') {
    return t('voice.state.wakewordDetected', 'Wakeword detected');
  }
  const key = `voice.state.${state}`;
  return t(key, state);
}

export function liveStateDotColor(state) {
  switch (state) {
    case 'listening':
      return 'voice-dot--listening';
    case 'starting':
    case 'wakeword_detected':
      return 'voice-dot--detected';
    case 'recording':
      return 'voice-dot--recording';
    case 'transcribing':
    case 'sending':
      return 'voice-dot--processing';
    case 'sent':
      return 'voice-dot--listening';
    case 'cancelled':
    case 'no_speech':
    case 'transcription_failed':
    case 'microphone_disconnected':
      return 'voice-dot--warning';
    case 'error':
      return 'voice-dot--error';
    default:
      return 'voice-dot--off';
  }
}

export function errorMessage(code) {
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
      'Configure a Speech-to-text Model under Settings → Models before enabling wakeword listening.',
    ),
    speech_to_text_unavailable: t(
      'settings.voice.error.speechToTextUnavailable',
      'The configured Speech-to-text Model is not currently usable. Check its Provider connection or choose another Model under Settings → Models.',
    ),
    speech_to_text_readiness_failed: t(
      'settings.voice.error.speechToTextReadiness',
      'Voice could not verify the Speech-to-text configuration. Check the Desktop log and try again.',
    ),
    missing_target_agent: t(
      'settings.voice.error.missingTarget',
      'Choose a Personal Agent for this server before enabling wakeword listening.',
    ),
    target_agent_unavailable: t(
      'settings.voice.error.targetUnavailable',
      'The selected Personal Agent no longer exists on this server. Choose another Agent.',
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
      'vBot could not open the target Agent session. Check the server connection and retry.',
    ),
    send_failed: t(
      'settings.voice.error.send',
      'The spoken command could not be sent. Check the server connection and retry.',
    ),
    voice_stack_unavailable: t(
      'settings.voice.error.stackUnavailable',
      'The Desktop Voice components are unavailable. Install the desktop Voice dependencies and restart vBot.',
    ),
  };
  return (
    messages[code] ||
    t(
      'settings.voice.error.unknown',
      'Voice stopped unexpectedly. Retry listening or restart the Desktop app.',
    )
  );
}
