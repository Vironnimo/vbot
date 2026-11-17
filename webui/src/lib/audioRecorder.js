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
    releaseMicrophone();
    for (const track of stream.getTracks()) {
      try {
        track.stop();
      } catch {
        // Continue releasing the remaining tracks when one browser track fails.
      }
    }
  };

  const chunks = [];
  let stopped = false;
  let recordingError = null;
  let mimeType;
  let recorder;
  // Some recorders choose the format themselves and reset `recorder.mimeType`
  // on stop. The final chunks still identify the actual container.
  const recordedMimeType = () =>
    chunks.find((chunk) => chunk.type)?.type || mimeType || 'audio/webm';
  const recordedBlob = () => new Blob(chunks, { type: recordedMimeType() });
  try {
    mimeType = chooseAudioMimeType(MediaRecorderClass);
    const recorderOptions = mimeType ? { mimeType } : {};
    recorder = new MediaRecorderClass(stream, recorderOptions);
    recorder.addEventListener('dataavailable', (event) => {
      if (event.data && event.data.size > 0) {
        chunks.push(event.data);
      }
    });
    recorder.addEventListener('stop', stopTracks, { once: true });
    recorder.addEventListener('error', (event) => {
      recordingError = event.error ?? new Error(t('audioRecording.failed'));
      stopTracks();
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
      try {
        recorder.start();
      } catch (error) {
        stopped = true;
        stopTracks();
        throw error;
      }
    },
    cancel() {
      if (stopped) {
        return;
      }
      stopped = true;
      try {
        if (recorder.state !== 'inactive') {
          recorder.stop();
        }
      } finally {
        stopTracks();
      }
    },
    stop() {
      if (recordingError) return Promise.reject(recordingError);
      if (stopped) {
        return Promise.resolve(recordedBlob());
      }
      stopped = true;

      return new Promise((resolve, reject) => {
        try {
          recorder.addEventListener('stop', () => resolve(recordedBlob()), {
            once: true,
          });
          recorder.addEventListener('error', () => reject(recordingError), {
            once: true,
          });

          if (recorder.state === 'inactive') {
            stopTracks();
            resolve(recordedBlob());
            return;
          }
          recorder.stop();
        } catch (error) {
          stopTracks();
          reject(error);
        }
      });
    },
    filename() {
      return `recording.${audioExtensionFromMimeType(recordedMimeType())}`;
    },
  };
}
