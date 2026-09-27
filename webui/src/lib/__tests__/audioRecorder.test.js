import { describe, expect, it, vi } from 'vitest';

import {
  audioExtensionFromMimeType,
  chooseAudioMimeType,
  createAudioRecorder,
} from '../audioRecorder.js';

class FakeMediaRecorder {
  static lastInstance = null;

  static isTypeSupported(mimeType) {
    return mimeType === 'audio/webm';
  }

  constructor(_stream, options) {
    this.options = options;
    this.state = 'inactive';
    this.listeners = new Map();
    FakeMediaRecorder.lastInstance = this;
  }

  addEventListener(eventName, listener) {
    this.listeners.set(eventName, listener);
  }

  start() {
    this.state = 'recording';
  }

  stop() {
    this.state = 'inactive';
  }

  emit(eventName, event) {
    this.listeners.get(eventName)?.(event);
  }
}

describe('audioRecorder', () => {
  it('chooses the first supported browser audio MIME type', () => {
    expect(chooseAudioMimeType(FakeMediaRecorder)).toBe('audio/webm');
    expect(chooseAudioMimeType(undefined)).toBe('');
    expect(audioExtensionFromMimeType('audio/ogg;codecs=opus')).toBe('ogg');
  });

  it('records the audio in the chosen format and releases the microphone after stopping', async () => {
    const track = { stop: vi.fn() };
    const recorder = await createAudioRecorder({
      navigator: navigatorWithTrack(track),
      MediaRecorder: FakeMediaRecorder,
    });

    recorder.start();
    expect(recorder.state).toBe('recording');
    const stopped = recorder.stop();
    FakeMediaRecorder.lastInstance.emit('dataavailable', {
      data: new Blob(['abc'], { type: 'audio/webm' }),
    });
    FakeMediaRecorder.lastInstance.emit('stop', {});

    const blob = await stopped;
    expect(blob.type).toBe('audio/webm');
    expect(await blob.text()).toBe('abc');
    expect(recorder.filename()).toBe('recording.webm');
    expect(track.stop).toHaveBeenCalledOnce();
  });

  it.each([
    [
      'creating the recorder',
      class {
        constructor() {
          throw new Error('failed');
        }
      },
    ],
    [
      'starting',
      class extends FakeMediaRecorder {
        start() {
          throw new Error('failed');
        }
      },
    ],
    [
      'stopping',
      class extends FakeMediaRecorder {
        stop() {
          throw new Error('failed');
        }
      },
    ],
  ])('releases the microphone when %s fails', async (step, MediaRecorder) => {
    const track = { stop: vi.fn() };
    const create = () =>
      createAudioRecorder({
        navigator: navigatorWithTrack(track),
        MediaRecorder,
        mimeType: 'audio/webm',
      });

    if (step === 'creating the recorder') {
      await expect(create()).rejects.toThrow('failed');
    } else {
      const recorder = await create();
      if (step === 'starting') {
        expect(() => recorder.start()).toThrow('failed');
      } else {
        recorder.start();
        await expect(recorder.stop()).rejects.toThrow('failed');
      }
    }
    expect(track.stop).toHaveBeenCalledOnce();
  });

  it('keeps releasing the other tracks when one track fails to stop', async () => {
    const failingTrack = {
      stop: vi.fn(() => {
        throw new Error('track stop failed');
      }),
    };
    const remainingTrack = { stop: vi.fn() };
    const recorder = await createAudioRecorder({
      navigator: {
        mediaDevices: {
          getUserMedia: vi.fn().mockResolvedValue({
            getTracks: () => [failingTrack, remainingTrack],
          }),
        },
      },
      MediaRecorder: FakeMediaRecorder,
      mimeType: 'audio/webm',
    });

    expect(() => recorder.cancel()).not.toThrow();
    expect(failingTrack.stop).toHaveBeenCalledOnce();
    expect(remainingTrack.stop).toHaveBeenCalledOnce();
  });
});

function navigatorWithTrack(track) {
  return {
    mediaDevices: {
      getUserMedia: vi.fn().mockResolvedValue({
        getTracks: () => [track],
      }),
    },
  };
}
