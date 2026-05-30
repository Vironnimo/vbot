const AUDIO_MIME_CANDIDATES = Object.freeze([
  'audio/webm;codecs=opus',
  'audio/webm',
  'audio/ogg;codecs=opus',
  'audio/ogg',
  'audio/mp4',
]);

export function chooseAudioMimeType(
  MediaRecorderClass = globalThis.MediaRecorder,
) {
  if (
    !MediaRecorderClass ||
    typeof MediaRecorderClass.isTypeSupported !== 'function'
  ) {
    return '';
  }

  return (
    AUDIO_MIME_CANDIDATES.find((mimeType) =>
      MediaRecorderClass.isTypeSupported(mimeType),
    ) ?? ''
  );
}

export function audioExtensionFromMimeType(mimeType) {
  const normalized =
    typeof mimeType === 'string' ? mimeType.toLowerCase().split(';')[0] : '';
  if (normalized === 'audio/ogg') {
    return 'ogg';
  }
  if (normalized === 'audio/mp4') {
    return 'm4a';
  }
  if (normalized === 'audio/mpeg') {
    return 'mp3';
  }
  if (normalized === 'audio/wav') {
    return 'wav';
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
  const mimeType = options.mimeType ?? chooseAudioMimeType(MediaRecorderClass);
  const recorderOptions = mimeType ? { mimeType } : {};
  const recorder = new MediaRecorderClass(stream, recorderOptions);
  const chunks = [];
  let stopped = false;

  recorder.addEventListener('dataavailable', (event) => {
    if (event.data && event.data.size > 0) {
      chunks.push(event.data);
    }
  });

  const stopTracks = () => {
    for (const track of stream.getTracks()) {
      track.stop();
    }
  };

  return {
    get state() {
      return recorder.state;
    },
    start() {
      recorder.start();
    },
    cancel() {
      if (stopped) {
        return;
      }
      stopped = true;
      if (recorder.state !== 'inactive') {
        recorder.stop();
      }
      stopTracks();
    },
    stop() {
      if (stopped) {
        return Promise.resolve(
          new Blob(chunks, { type: mimeType || 'audio/webm' }),
        );
      }
      stopped = true;

      return new Promise((resolve, reject) => {
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
      });
    },
    filename() {
      return `recording.${audioExtensionFromMimeType(mimeType)}`;
    },
  };
}
