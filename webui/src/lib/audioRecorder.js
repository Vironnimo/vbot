import { t } from './i18n.js';
import { claimMicrophone } from './microphoneUse.js';

const AUDIO_MIME_CANDIDATES = Object.freeze([
  'audio/webm;codecs=opus',
  'audio/webm',
  'audio/ogg;codecs=opus',
  'audio/ogg',
  'audio/mp4',
]);

function chooseAudioMimeType(MediaRecorderClass) {
  if (typeof MediaRecorderClass.isTypeSupported !== 'function') {
    return '';
  }

  return (
    AUDIO_MIME_CANDIDATES.find((mimeType) =>
      MediaRecorderClass.isTypeSupported(mimeType),
    ) ?? ''
  );
}

function audioExtensionFromMimeType(mimeType) {
  const container = mimeType.split(';')[0];
  if (container === 'audio/ogg') {
    return 'ogg';
  }
  if (container === 'audio/mp4') {
    return 'm4a';
  }
  return 'webm';
}

export async function createAudioRecorder(options = {}) {
  const navigatorObject = options.navigator ?? globalThis.navigator;
  const MediaRecorderClass = options.MediaRecorder ?? globalThis.MediaRecorder;

  if (
    !navigatorObject?.mediaDevices ||
    typeof navigatorObject.mediaDevices.getUserMedia !== 'function' ||
    typeof MediaRecorderClass !== 'function'
  ) {
    throw new Error(t('audioRecording.unavailable'));
  }

  const stream = await navigatorObject.mediaDevices.getUserMedia({
    audio: true,
  });
  // A running Live call pauses while this recording holds the microphone.
  const releaseMicrophone = claimMicrophone();
  let released = false;
  const stopTracks = () => {
    if (released) return;
    released = true;
    try {
      for (const track of stream.getTracks()) {
        try {
          track.stop();
        } catch {
          // Continue releasing the remaining tracks when one browser track fails.
        }
      }
    } finally {
      releaseMicrophone();
    }
  };

  const chunks = [];
  let started = false;
  let stopPromise = null;
  let recordingError = null;
  let mimeType;
  let nativeMimeType;
  let recorder;
  let settle;
  const finished = new Promise((resolve) => {
    settle = resolve;
  });
  const finish = () => {
    try {
      stopTracks();
    } finally {
      settle();
    }
  };
  // Some recorders choose the format themselves and reset `recorder.mimeType`
  // on stop. Prefer the chunks' type when they identify the actual container.
  const recordedMimeType = () =>
    chunks.find((chunk) => chunk.type)?.type ||
    recorder?.mimeType ||
    nativeMimeType ||
    mimeType ||
    'audio/webm';
  const recordedBlob = () => new Blob(chunks, { type: recordedMimeType() });
  try {
    mimeType = chooseAudioMimeType(MediaRecorderClass);
    const recorderOptions = mimeType ? { mimeType } : {};
    recorder = new MediaRecorderClass(stream, recorderOptions);
    recorder.addEventListener('start', () => {
      nativeMimeType = recorder.mimeType || nativeMimeType;
    });
    recorder.addEventListener('dataavailable', (event) => {
      if (event.data && event.data.size > 0) {
        chunks.push(event.data);
      }
    });
    recorder.addEventListener('stop', finish, { once: true });
    recorder.addEventListener('error', (event) => {
      recordingError = event.error ?? new Error(t('audioRecording.failed'));
      finish();
    });
  } catch (error) {
    stopTracks();
    throw error;
  }

  return {
    get state() {
      return recorder.state;
    },
    start() {
      if (released) return;
      try {
        recorder.start();
        started = true;
        nativeMimeType = recorder.mimeType || nativeMimeType;
      } catch (error) {
        recordingError = error;
        finish();
        throw error;
      }
    },
    cancel() {
      if (released) return;
      try {
        if (recorder.state !== 'inactive') {
          recorder.stop();
        }
      } finally {
        finish();
      }
    },
    stop() {
      if (stopPromise) return stopPromise;
      stopPromise = finished.then(() => {
        if (recordingError) throw recordingError;
        return recordedBlob();
      });
      try {
        if (!started) finish();
        else if (!released && recorder.state !== 'inactive') {
          nativeMimeType = recorder.mimeType || nativeMimeType;
          recorder.stop();
        }
      } catch (error) {
        recordingError = error;
        finish();
      }
      return stopPromise;
    },
    filename() {
      return `recording.${audioExtensionFromMimeType(recordedMimeType())}`;
    },
  };
}
