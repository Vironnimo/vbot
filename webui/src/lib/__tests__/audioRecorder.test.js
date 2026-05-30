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
});

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
