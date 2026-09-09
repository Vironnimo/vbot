// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { t } from '../../lib/i18n.js';
import { reactiveProps } from './reactiveProps.svelte.js';

vi.mock(
  'svelte',
  async () => import('../../../node_modules/svelte/src/index-client.js'),
);
const { default: AudioPlayer } = await import('../ui/AudioPlayer.svelte');
const { default: ChatAssistantRun } =
  await import('../chat/ChatAssistantRun.svelte');
const { default: ChatTimelineEntry } =
  await import('../chat/ChatTimelineEntry.svelte');
let components = [];
let play;
let pause;
let load;

beforeEach(() => {
  play = vi
    .spyOn(HTMLMediaElement.prototype, 'play')
    .mockImplementation(function () {
      Object.defineProperty(this, 'paused', {
        configurable: true,
        value: false,
      });
      this.dispatchEvent(new Event('play'));
      this.dispatchEvent(new Event('playing'));
      return Promise.resolve();
    });
  pause = vi
    .spyOn(HTMLMediaElement.prototype, 'pause')
    .mockImplementation(function () {
      Object.defineProperty(this, 'paused', {
        configurable: true,
        value: true,
      });
      this.dispatchEvent(new Event('pause'));
    });
  load = vi
    .spyOn(HTMLMediaElement.prototype, 'load')
    .mockImplementation(() => {});
});
afterEach(async () => {
  for (const component of components) await unmount(component);
  components = [];
  document.body.innerHTML = '';
  vi.restoreAllMocks();
});
async function flush() {
  for (let i = 0; i < 5; i++) {
    await Promise.resolve();
    flushSync();
  }
}
function render(props = {}, Component = AudioPlayer) {
  const component = mount(Component, { target: document.body, props });
  components.push(component);
  flushSync();
  return component;
}
function control(key, root = document) {
  return root.querySelector(`[aria-label="${t(key)}"]`);
}
function ready(audio = document.querySelector('audio'), seconds = 125) {
  Object.defineProperty(audio, 'duration', {
    configurable: true,
    value: seconds,
  });
  audio.dispatchEvent(new Event('loadedmetadata'));
  audio.dispatchEvent(new Event('canplay'));
  flushSync();
  return audio;
}

it('keeps native chrome hidden and plays, pauses, seeks and replays through media state', async () => {
  render({ src: '/test.wav' });
  const audio = ready();
  expect(audio.controls).toBe(false);
  expect(play).not.toHaveBeenCalled();
  control('audio.play').click();
  await flush();
  expect(control('audio.pause')).toBeTruthy();
  const seek = control('audio.seek');
  seek.value = '65';
  seek.dispatchEvent(new Event('input', { bubbles: true }));
  await flush();
  expect(audio.currentTime).toBe(65);
  expect(seek.getAttribute('aria-valuetext')).toBe('1:05 of 2:05');
  control('audio.pause').click();
  await flush();
  audio.dispatchEvent(new Event('canplay'));
  await flush();
  expect(play).toHaveBeenCalledTimes(1);
  expect(control('audio.play')).toBeTruthy();
  Object.defineProperty(audio, 'ended', { configurable: true, value: true });
  audio.dispatchEvent(new Event('ended'));
  control('audio.play').click();
  await flush();
  expect(audio.currentTime).toBe(0);
  expect(play).toHaveBeenCalledTimes(2);
});

it('attempts autoplay only once and respects a pause after subsequent canplay events', async () => {
  render({ src: '/test.wav', autoplay: true });
  const audio = ready();
  await flush();
  control('audio.pause').click();
  await flush();
  audio.dispatchEvent(new Event('canplay'));
  audio.dispatchEvent(new Event('canplay'));
  await flush();
  expect(play).toHaveBeenCalledTimes(1);
  expect(control('audio.play')).toBeTruthy();
});

it('leaves blocked autoplay playable and exposes actual load failures with retry', async () => {
  play.mockRejectedValueOnce(new DOMException('blocked', 'NotAllowedError'));
  render({ src: '/test.wav', autoplay: true });
  const audio = ready();
  await flush();
  expect(document.querySelector('[role="alert"]')).toBeNull();
  expect(control('audio.play')).toBeTruthy();
  Object.defineProperty(audio, 'error', {
    configurable: true,
    value: { code: 4 },
  });
  audio.dispatchEvent(new Event('error'));
  await flush();
  expect(document.querySelector('[role="alert"]')).toBeTruthy();
  expect(control('audio.seek').disabled).toBe(true);
  const previousLoads = load.mock.calls.length;
  control('audio.play').click();
  await flush();
  expect(load.mock.calls.length).toBe(previousLoads + 1);
  expect(control('audio.pause')).toBeTruthy();
});

it('shows manual playback failures and ignores old failures after replacing a source', async () => {
  play.mockRejectedValueOnce(
    new DOMException('unsupported', 'NotSupportedError'),
  );
  const props = reactiveProps({ src: '/first.wav' });
  render(props);
  ready();
  control('audio.play').click();
  await flush();
  expect(document.querySelector('[role="alert"]')).toBeTruthy();
  let reject;
  play.mockImplementationOnce(
    () =>
      new Promise((_, fail) => {
        reject = fail;
      }),
  );
  control('audio.play').click();
  await flush();
  props.src = '/second.wav';
  await flush();
  reject(new DOMException('late failure', 'NotSupportedError'));
  await flush();
  expect(document.querySelector('audio').getAttribute('src')).toBe(
    '/second.wav',
  );
  expect(document.querySelector('[role="alert"]')).toBeNull();
  expect(control('audio.seek').disabled).toBe(true);
});

it('supports volume, mute, zero-volume recovery, speed selection and download', async () => {
  render({ src: '/test.wav' });
  const audio = ready();
  const slider = control('audio.volume');
  slider.value = '0.4';
  slider.dispatchEvent(new Event('input', { bubbles: true }));
  await flush();
  expect(audio.volume).toBe(0.4);
  control('audio.mute').click();
  await flush();
  expect(audio.muted).toBe(true);
  control('audio.unmute').click();
  await flush();
  expect(audio.volume).toBe(0.4);
  slider.value = '0';
  slider.dispatchEvent(new Event('input', { bubbles: true }));
  await flush();
  control('audio.unmute').click();
  await flush();
  expect(audio.volume).toBe(1);
  control('audio.speed').click();
  await flush();
  [...document.querySelectorAll('[role="option"]')]
    .find((node) => node.textContent.trim() === '1.5×')
    .click();
  await flush();
  expect(audio.playbackRate).toBe(1.5);
  const click = vi
    .spyOn(HTMLAnchorElement.prototype, 'click')
    .mockImplementation(function () {
      expect(this.getAttribute('href')).toBe('/test.wav');
      expect(this.hasAttribute('download')).toBe(true);
    });
  control('audio.download').click();
  expect(click).toHaveBeenCalledOnce();
});

it('keeps unknown and nonfinite durations out of the seek range', async () => {
  render({ src: '/test.wav' });
  const audio = ready(undefined, Infinity);
  expect(control('audio.seek').disabled).toBe(true);
  expect(document.body.textContent).not.toMatch(/NaN|Infinity/);
  ready(audio, 3661);
  expect(control('audio.seek').max).toBe('3661');
  expect(control('audio.seek').getAttribute('aria-valuetext')).toBe(
    '0:00 of 1:01:01',
  );
});

it('pauses the previous player and releases its media and listener on unmount', async () => {
  const removeListener = vi.spyOn(document, 'removeEventListener');
  const first = render({ src: '/first.wav' });
  render({ src: '/second.wav' });
  const players = document.querySelectorAll('.audio-player');
  const audios = document.querySelectorAll('audio');
  ready(audios[0]);
  ready(audios[1]);
  control('audio.play', players[0]).click();
  await flush();
  control('audio.play', players[1]).click();
  await flush();
  expect(control('audio.play', players[0])).toBeTruthy();
  expect(control('audio.pause', players[1])).toBeTruthy();
  const previousPauses = pause.mock.calls.length;
  await unmount(first);
  components.shift();
  expect(pause.mock.calls.length).toBeGreaterThan(previousPauses);
  expect(audios[0].hasAttribute('src')).toBe(false);
  expect(removeListener).toHaveBeenCalledWith(
    'play',
    expect.any(Function),
    true,
  );
});

it.each(['run', 'event'])(
  'renders the same controllable player in the %s Chat representation',
  async (kind) => {
    const result = {
      ok: true,
      data: { artifact: { kind: 'speech', url: '/chat.wav' } },
    };
    const toolCall = {
      id: 'call-speech',
      name: 'text_to_speech',
      arguments: { text: 'test-owned speech' },
    };
    const event = {
      type: 'tool_call_result',
      payload: { tool_call: toolCall, result },
    };
    const item =
      kind === 'run'
        ? {
            type: 'assistant_run',
            id: 'run-speech',
            runId: 'run-speech',
            status: 'completed',
            items: [
              {
                ...toolCall,
                type: 'tool_call',
                toolCallId: toolCall.id,
                status: 'success',
                result,
                resultEvent: event,
              },
            ],
          }
        : { type: 'event', id: 'event-speech', event };
    render({ item }, kind === 'run' ? ChatAssistantRun : ChatTimelineEntry);
    const audio = document.querySelector('audio');
    expect(audio?.getAttribute('src')).toBe('/chat.wav');
    expect(audio.controls).toBe(false);
    ready(audio);
    await flush();
    expect(control('audio.pause')).toBeTruthy();
    expect(control('audio.download')).toBeTruthy();
  },
);
