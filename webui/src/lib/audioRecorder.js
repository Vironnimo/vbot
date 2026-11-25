import { t } from './i18n.js';
import { claimMicrophone } from './microphoneUse.js';

const AUDIO_MIME_CANDIDATES = Object.freeze([
  'audio/webm;codecs=opus',
  'audio/webm',
  'audio/ogg;codecs=opus',
  'audio/ogg',
  'audio/mp4',
]);

// The winning constructor option belongs to this browser implementation/tab.
const preferredTypes = new WeakMap();
const mimeStorageKey = 'vbot:recorder-mime';

function preferredType(Recorder) {
  if (preferredTypes.has(Recorder)) return preferredTypes.get(Recorder);
  if (Recorder === globalThis.MediaRecorder) {
    try {
      return globalThis.sessionStorage?.getItem(mimeStorageKey);
    } catch {
      // Private browsing may disable storage.
    }
  }
}

function confirmType(Recorder, type) {
  if (preferredTypes.get(Recorder) === type) return;
  preferredTypes.set(Recorder, type);
  if (Recorder === globalThis.MediaRecorder) {
    try {
      globalThis.sessionStorage?.setItem(mimeStorageKey, type);
    } catch {
      // The in-memory preference still serves this page.
    }
  }
}

function audioMimeCandidates(Recorder) {
  const choices =
    typeof Recorder.isTypeSupported === 'function'
      ? [
          ...AUDIO_MIME_CANDIDATES.filter((type) =>
            Recorder.isTypeSupported(type),
          ),
          '',
        ]
      : ['', ...AUDIO_MIME_CANDIDATES];
  const preferred = preferredType(Recorder);
  return choices.includes(preferred)
    ? [preferred, ...choices.filter((type) => type !== preferred)]
    : choices;
}

function formatFailure(error) {
  return [
    'NotSupportedError',
    'EncodingError',
    'UnknownError',
    'TypeError',
  ].includes(error?.name);
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
  let candidates;
  function construct() {
    while (candidates.length) {
      mimeType = candidates.shift();
      try {
        recorder = new MediaRecorderClass(stream, mimeType ? { mimeType } : {});
        return;
      } catch (error) {
        if (!candidates.length || !formatFailure(error)) throw error;
      }
    }
  }
  function listen(native) {
    let confirmed = false;
    const confirm = () => {
      if (native !== recorder || released) return;
      confirmed = true;
      nativeMimeType = native.mimeType || nativeMimeType;
      confirmType(MediaRecorderClass, mimeType);
    };
    native.addEventListener('start', confirm);
    native.addEventListener('dataavailable', (event) => {
      if (native === recorder && event.data?.size > 0) {
        confirm();
        chunks.push(event.data);
      }
    });
    native.addEventListener(
      'stop',
      () => {
        if (native === recorder) finish();
      },
      { once: true },
    );
    native.addEventListener('error', (event) => {
      if (native !== recorder || released) return;
      let error = event.error ?? new Error(t('audioRecording.failed'));
      if (
        !confirmed &&
        !stopPromise &&
        formatFailure(error) &&
        candidates.length
      ) {
        try {
          recorder = null;
          if (native.state !== 'inactive') native.stop();
          construct();
          startCandidate();
          return;
        } catch (failure) {
          error = failure;
        }
      }
      recordingError = error;
      finish();
    });
  }
  function startCandidate() {
    while (true) {
      listen(recorder);
      try {
        recorder.start();
        started = true;
        nativeMimeType = recorder.mimeType || nativeMimeType;
        return;
      } catch (error) {
        if (
          !formatFailure(error) ||
          recorder.state !== 'inactive' ||
          !candidates.length
        )
          throw error;
        recorder = null;
        construct();
      }
    }
  }
  try {
    candidates = audioMimeCandidates(MediaRecorderClass);
    construct();
  } catch (error) {
    stopTracks();
    throw error;
  }

  return {
    get state() {
      return recorder?.state ?? 'inactive';
    },
    start() {
      if (released || started) return;
      try {
        startCandidate();
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
