import {
  TERMINAL_STREAM_CONNECTED,
  visibleTerminals,
  terminalIsFinished,
} from './state.js';

export function createTerminalSpeech({
  state,
  onTranscript,
  onSpeechError,
  createRecorder,
  transcribe,
  isDestroyed,
  isUnavailable,
  streamView,
  selectTerminal,
}) {
  let speechRequest = null;

  function releaseRecorder(recorder) {
    try {
      recorder?.cancel();
    } catch {
      // The recorder releases tracks in its own finally block.
    }
  }

  function cancelSpeech(terminalId = state.speechTerminalId) {
    if (!speechRequest || speechRequest.terminalId !== terminalId) {
      return;
    }
    const request = speechRequest;
    speechRequest = null;
    request.abort.abort();
    releaseRecorder(request.recorder);
    state.speechTerminalId = '';
    state.speechState = 'idle';
  }

  function speechTargetAvailable(terminalId) {
    return (
      !isDestroyed() &&
      !isUnavailable() &&
      visibleTerminals(state).some(
        (item) => item.terminal_id === terminalId && !terminalIsFinished(item),
      ) &&
      streamView(terminalId).status === TERMINAL_STREAM_CONNECTED
    );
  }

  async function toggleSpeech(terminalId) {
    if (!speechTargetAvailable(terminalId)) {
      return;
    }
    if (speechRequest) {
      if (
        speechRequest.terminalId !== terminalId ||
        state.speechState !== 'recording'
      ) {
        return;
      }
      const request = speechRequest;
      state.speechState = 'transcribing';
      try {
        const audio = await request.recorder.stop();
        if (speechRequest !== request || !speechTargetAvailable(terminalId)) {
          return;
        }
        const result = await transcribe(audio, {
          filename: request.recorder.filename(),
          signal: request.abort.signal,
        });
        if (speechRequest !== request || !speechTargetAvailable(terminalId)) {
          return;
        }
        // Speech is editable terminal input. Never let recognized newlines or
        // control characters become Enter, shortcuts, or terminal escapes.
        const text =
          typeof result.text === 'string'
            ? result.text
                // eslint-disable-next-line no-control-regex -- Recognized text must not inject terminal controls.
                .replace(/[\x00-\x1f\x7f-\x9f\u2028\u2029]+/g, ' ')
                .trim()
            : '';
        if (text) {
          onTranscript(terminalId, text);
        }
      } catch (error) {
        if (speechRequest === request) {
          onSpeechError(error, 'transcribing');
        }
      } finally {
        if (speechRequest === request) {
          cancelSpeech(terminalId);
        }
      }
      return;
    }
    const request = {
      terminalId,
      recorder: null,
      abort: new AbortController(),
    };
    speechRequest = request;
    state.speechTerminalId = terminalId;
    state.speechState = 'requesting';
    selectTerminal(terminalId);
    try {
      const recorder = await createRecorder();
      if (speechRequest !== request || !speechTargetAvailable(terminalId)) {
        releaseRecorder(recorder);
        if (speechRequest === request) cancelSpeech(terminalId);
        return;
      }
      request.recorder = recorder;
      recorder.start();
      state.speechState = 'recording';
    } catch (error) {
      if (speechRequest === request) {
        cancelSpeech(terminalId);
        onSpeechError(error, 'requesting');
      }
    }
  }

  return { cancelSpeech, toggleSpeech };
}
