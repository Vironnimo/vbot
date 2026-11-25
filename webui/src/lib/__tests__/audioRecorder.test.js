import { describe, expect, it, vi } from 'vitest';

import { createAudioRecorder } from '../audioRecorder.js';
import { microphoneInUse, onMicrophoneUse } from '../microphoneUse.js';

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
    const listeners = this.listeners.get(eventName) ?? [];
    this.listeners.set(eventName, [...listeners, listener]);
  }

  start() {
    this.state = 'recording';
  }

  stop() {
    this.state = 'inactive';
  }

  emit(eventName, event) {
    for (const listener of this.listeners.get(eventName) ?? []) listener(event);
  }
}

describe('audioRecorder', () => {
  it('restores a confirmed browser default after a reload and respects microphone refusal', async () => {
    const attempts = [];
    class Recorder extends FakeMediaRecorder {
      constructor(stream, options) {
        super(stream, options);
        attempts.push(options);
      }
      start() {
        throw new DOMException('denied', 'NotAllowedError');
      }
    }
    const storage = { getItem: vi.fn(() => ''), setItem: vi.fn() };
    vi.stubGlobal('MediaRecorder', Recorder);
    vi.stubGlobal('sessionStorage', storage);
    const track = { stop: vi.fn() };
    try {
      const handle = await createAudioRecorder({
        navigator: navigatorWithTrack(track),
      });
      expect(() => handle.start()).toThrow('denied');
      expect(attempts).toEqual([{}]);
      expect(storage.setItem).not.toHaveBeenCalled();
      expect(track.stop).toHaveBeenCalledOnce();
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it.each(['construct', 'start', 'before start event'])(
    'tries the browser default after a codec fails at %s and remembers its confirmed start',
    async (phase) => {
      const attempts = [];
      let rejected;
      class Recorder extends FakeMediaRecorder {
        constructor(stream, options) {
          super(stream, options);
          attempts.push(options.mimeType ?? 'default');
          if (options.mimeType) {
            rejected = this;
            if (phase === 'construct')
              throw new DOMException('codec', 'NotSupportedError');
          }
        }
        start() {
          if (this.options.mimeType && phase === 'start')
            throw new DOMException('codec', 'NotSupportedError');
          super.start();
        }
      }
      const track = { stop: vi.fn() };
      const navigator = navigatorWithTrack(track);
      for (let i = 0; i < 2; i++) {
        const handle = await createAudioRecorder({
          navigator,
          MediaRecorder: Recorder,
        });
        handle.start();
        if (phase === 'before start event' && i === 0)
          rejected.emit('error', {
            error: new DOMException('codec', 'NotSupportedError'),
          });
        const current = FakeMediaRecorder.lastInstance;
        current.emit('start', {});
        rejected?.emit('stop', {});
        expect(track.stop).toHaveBeenCalledTimes(i);
        handle.cancel();
      }
      expect(attempts).toEqual(['audio/webm', 'default', 'default']);
      expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(2);
      expect(track.stop).toHaveBeenCalledTimes(2);
    },
  );

  it.each(['chunk', 'recorder'])(
    'records in a supported format and names the file after the %s MIME type',
    async (source) => {
      class OggRecorder extends FakeMediaRecorder {
        static isTypeSupported(mimeType) {
          return mimeType.startsWith('audio/ogg');
        }
      }
      const ogg = await createAudioRecorder({
        navigator: navigatorWithTrack({ stop: vi.fn() }),
        MediaRecorder: OggRecorder,
      });
      expect(FakeMediaRecorder.lastInstance.options).toEqual({
        mimeType: 'audio/ogg;codecs=opus',
      });
      expect(ogg.filename()).toBe('recording.ogg');

      // A browser that cannot report its formats records in its default one.
      class UntypedRecorder extends FakeMediaRecorder {
        static isTypeSupported = undefined;

        start() {
          super.start();
          this.mimeType = source === 'recorder' ? 'audio/mp4' : 'audio/ogg';
        }

        stop() {
          super.stop();
          this.mimeType = '';
        }
      }
      const untyped = await createAudioRecorder({
        navigator: navigatorWithTrack({ stop: vi.fn() }),
        MediaRecorder: UntypedRecorder,
      });
      expect(FakeMediaRecorder.lastInstance.options).toEqual({});
      untyped.start();
      const recorded = untyped.stop();
      FakeMediaRecorder.lastInstance.emit('dataavailable', {
        data: new Blob(['aac'], {
          type: source === 'chunk' ? 'audio/mp4' : '',
        }),
      });
      FakeMediaRecorder.lastInstance.emit('stop', {});
      expect((await recorded).type).toBe('audio/mp4');
      expect(untyped.filename()).toBe('recording.m4a');
      ogg.cancel();
      untyped.cancel();
    },
  );

  it.each(['requested', 'simultaneous', 'native'])(
    'collects final audio and releases the microphone after a %s stop',
    async (mode) => {
      const track = { stop: vi.fn() };
      const uses = [];
      const unsubscribe = onMicrophoneUse((inUse) =>
        uses.push([inUse, track.stop.mock.calls.length]),
      );
      const recorder = await createAudioRecorder({
        navigator: navigatorWithTrack(track),
        MediaRecorder: FakeMediaRecorder,
      });
      // The page knows a recording holds the microphone, so Live voice pauses.
      expect(microphoneInUse()).toBe(true);

      recorder.start();
      expect(recorder.state).toBe('recording');
      if (mode === 'native') FakeMediaRecorder.lastInstance.stop();
      const stopped = recorder.stop();
      const simultaneous = mode === 'simultaneous' ? recorder.stop() : stopped;
      FakeMediaRecorder.lastInstance.emit('dataavailable', {
        data: new Blob(['abc'], { type: 'audio/webm' }),
      });
      FakeMediaRecorder.lastInstance.emit('stop', {});

      const blob = await stopped;
      expect(blob.type).toBe('audio/webm');
      expect(await blob.text()).toBe('abc');
      expect(await (await simultaneous).text()).toBe('abc');
      expect(recorder.filename()).toBe('recording.webm');
      expect(track.stop).toHaveBeenCalledOnce();
      expect(microphoneInUse()).toBe(false);
      unsubscribe();
      expect(uses).toEqual([
        [true, 0],
        [false, 1],
      ]);
    },
  );

  it.each(['stop', 'error'])(
    'releases the microphone when the browser sends %s on its own',
    async (event) => {
      const track = { stop: vi.fn() };
      const recorder = await createAudioRecorder({
        navigator: navigatorWithTrack(track),
        MediaRecorder: FakeMediaRecorder,
      });
      recorder.start();
      const native = FakeMediaRecorder.lastInstance;
      native.state = 'inactive';
      const error = new Error('Microphone disconnected');
      native.emit(event, { error });

      expect(track.stop).toHaveBeenCalledOnce();
      expect(microphoneInUse()).toBe(false);
      if (event === 'error') await expect(recorder.stop()).rejects.toBe(error);
      else await expect(recorder.stop()).resolves.toBeInstanceOf(Blob);
      expect(track.stop).toHaveBeenCalledOnce();
    },
  );

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

  it.each(['before starting', 'while a stop is pending'])(
    'releases every possible track after cancellation %s',
    async (when) => {
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
      });

      let stopped;
      if (when === 'while a stop is pending') {
        recorder.start();
        stopped = recorder.stop();
      }
      expect(() => recorder.cancel()).not.toThrow();
      expect(failingTrack.stop).toHaveBeenCalledOnce();
      expect(remainingTrack.stop).toHaveBeenCalledOnce();
      await stopped;
    },
  );
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
