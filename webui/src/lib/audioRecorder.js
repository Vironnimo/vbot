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
    throw new Error('Browser audio recording is not available.');
  }

  const stream = await navigatorObject.mediaDevices.getUserMedia({
    audio: true,
  });
  const stopTracks = () => {
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
  let mimeType;
  let recorder;
  try {
    mimeType = chooseAudioMimeType(MediaRecorderClass);
    const recorderOptions = mimeType ? { mimeType } : {};
    recorder = new MediaRecorderClass(stream, recorderOptions);
    recorder.addEventListener('dataavailable', (event) => {
      if (event.data && event.data.size > 0) {
        chunks.push(event.data);
      }
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
      if (stopped) {
        return Promise.resolve(
          new Blob(chunks, { type: mimeType || 'audio/webm' }),
        );
      }
      stopped = true;

      return new Promise((resolve, reject) => {
        try {
          recorder.addEventListener(
            'stop',
            () => {
              stopTracks();
              resolve(new Blob(chunks, { type: mimeType || 'audio/webm' }));
            },
            { once: true },
          );
          recorder.addEventListener(
            'error',
            (event) => {
              stopTracks();
              reject(event.error ?? new Error('Audio recording failed.'));
            },
            { once: true },
          );

          if (recorder.state === 'inactive') {
            stopTracks();
            resolve(new Blob(chunks, { type: mimeType || 'audio/webm' }));
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
      return `recording.${audioExtensionFromMimeType(mimeType)}`;
    },
  };
}
