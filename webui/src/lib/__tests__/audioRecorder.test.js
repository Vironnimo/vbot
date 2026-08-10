import { describe, expect, it, vi } from 'vitest';

import {
  audioExtensionFromMimeType,
  chooseAudioMimeType,
  createAudioRecorder,
} from '../audioRecorder.js';

describe('audioRecorder helpers', () => {
  it('chooses the first supported browser audio MIME type', () => {
    class FakeMediaRecorder {
      static isTypeSupported(mimeType) {
        return mimeType === 'audio/webm';
      }
    }

    expect(chooseAudioMimeType(FakeMediaRecorder)).toBe('audio/webm');
    expect(audioExtensionFromMimeType('audio/ogg;codecs=opus')).toBe('ogg');
  });

  it('stops tracks after recording is stopped', async () => {
    const track = { stop: vi.fn() };
    const navigatorObject = {
      mediaDevices: {
        getUserMedia: vi.fn().mockResolvedValue({
          getTracks: () => [track],
        }),
      },
    };

    const recorder = await createAudioRecorder({
      navigator: navigatorObject,
      MediaRecorder: FakeMediaRecorder,
      mimeType: 'audio/webm',
    });

    recorder.start();
    const stopPromise = recorder.stop();
    FakeMediaRecorder.lastInstance.emit('dataavailable', {
      data: new Blob(['abc'], { type: 'audio/webm' }),
    });
    FakeMediaRecorder.lastInstance.emit('stop', {});

    const blob = await stopPromise;

    expect(blob.type).toBe('audio/webm');
    expect(track.stop).toHaveBeenCalledOnce();
  });

  it('stops tracks when MediaRecorder construction fails', async () => {
    const track = { stop: vi.fn() };
    class BrokenMediaRecorder {
      constructor() {
        throw new Error('constructor failed');
      }
    }

    await expect(
      createAudioRecorder({
        navigator: navigatorWithTrack(track),
        MediaRecorder: BrokenMediaRecorder,
        mimeType: 'audio/webm',
      }),
    ).rejects.toThrow('constructor failed');
    expect(track.stop).toHaveBeenCalledOnce();
  });

  it('stops tracks when MIME type detection fails', async () => {
    const track = { stop: vi.fn() };

    await expect(
      createAudioRecorder({
        navigator: navigatorWithTrack(track),
        MediaRecorder: class extends FakeMediaRecorder {
          static isTypeSupported() {
            throw new Error('MIME detection failed');
          }
        },
      }),
    ).rejects.toThrow('MIME detection failed');

    expect(track.stop).toHaveBeenCalledOnce();
  });

  it('continues stopping tracks when one track throws', async () => {
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

  it('stops tracks when recorder event setup fails', async () => {
    const constructionTrack = { stop: vi.fn() };
    await expect(
      createAudioRecorder({
        navigator: navigatorWithTrack(constructionTrack),
        MediaRecorder: class extends FakeMediaRecorder {
          addEventListener() {
            throw new Error('listener setup failed');
          }
        },
        mimeType: 'audio/webm',
      }),
    ).rejects.toThrow('listener setup failed');
    expect(constructionTrack.stop).toHaveBeenCalledOnce();

    const stopTrack = { stop: vi.fn() };
    const recorder = await createAudioRecorder({
      navigator: navigatorWithTrack(stopTrack),
      MediaRecorder: class extends FakeMediaRecorder {
        addEventListener(eventName, listener) {
          if (eventName === 'stop') {
            throw new Error('stop listener failed');
          }
          super.addEventListener(eventName, listener);
        }
      },
      mimeType: 'audio/webm',
    });
    recorder.start();
    await expect(recorder.stop()).rejects.toThrow('stop listener failed');
    expect(stopTrack.stop).toHaveBeenCalledOnce();
  });

  it('stops tracks when recording start or stop throws', async () => {
    const startTrack = { stop: vi.fn() };
    const startRecorder = await createAudioRecorder({
      navigator: navigatorWithTrack(startTrack),
      MediaRecorder: class extends FakeMediaRecorder {
        start() {
          throw new Error('start failed');
        }
      },
      mimeType: 'audio/webm',
    });
    expect(() => startRecorder.start()).toThrow('start failed');
    expect(startTrack.stop).toHaveBeenCalledOnce();

    const stopTrack = { stop: vi.fn() };
    const stopRecorder = await createAudioRecorder({
      navigator: navigatorWithTrack(stopTrack),
      MediaRecorder: class extends FakeMediaRecorder {
        stop() {
          throw new Error('stop failed');
        }
      },
      mimeType: 'audio/webm',
    });
    stopRecorder.start();
    await expect(stopRecorder.stop()).rejects.toThrow('stop failed');
    expect(stopTrack.stop).toHaveBeenCalledOnce();
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

class FakeMediaRecorder {
  static lastInstance = null;

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
