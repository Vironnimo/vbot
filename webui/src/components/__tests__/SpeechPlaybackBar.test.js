// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { createSpeechAudio } from '../../lib/speechAudio.svelte.js';
import * as speechPlayback from '../../lib/speechPlayback.js';
import { reactiveProps } from './reactiveProps.support.svelte.js';
import { t } from '../../lib/i18n.js';

vi.mock(
  'svelte',
  async () => import('../../../node_modules/svelte/src/index-client.js'),
);
const { default: Host } = await import('./SpeechPlaybackHost.support.svelte');
let owner;
let component;
let play;
let streams;

function speechEvent(id = 'one', kind = 'speech_playback') {
  return {
    type: 'run_output',
    payload: {
      run_id: `run-${id}`,
      agent_id: `agent-${id}`,
      session_id: `session-${id}`,
      run_event_type: kind,
      output:
        kind === 'speech_playback'
          ? { tool_call_id: `call-${id}`, url: `/api/speech/playback/${id}` }
          : {
              tool_call: { id: `call-${id}`, name: 'generate_speech' },
              result: {
                ok: true,
                data: {
                  artifact: {
                    kind: 'speech',
                    url: `/api/speech/artifacts/${id}`,
                  },
                },
              },
            },
    },
  };
}
async function flush() {
  for (let i = 0; i < 5; i++) {
    await Promise.resolve();
    flushSync();
  }
}
beforeEach(() => {
  streams = [];
  vi.spyOn(HTMLMediaElement.prototype, 'load').mockImplementation(() => {});
  vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(function () {
    this.dispatchEvent(new Event('pause'));
  });
  play = vi
    .spyOn(HTMLMediaElement.prototype, 'play')
    .mockImplementation(function () {
      this.dispatchEvent(new Event('play'));
      this.dispatchEvent(new Event('playing'));
      return Promise.resolve();
    });
  vi.spyOn(speechPlayback, 'createSpeechPlayback').mockImplementation(
    ({ onState }) => {
      const state = {
        paused: true,
        loading: false,
        currentTime: 0,
        duration: 0,
      };
      const report = (patch) => onState(Object.assign(state, patch));
      const stream = {
        report,
        play: vi.fn(() => report({ paused: false })),
        pause: vi.fn(() => report({ paused: true })),
        setVolume: vi.fn(),
        stop: vi.fn(),
      };
      streams.push(stream);
      return stream;
    },
  );
  owner = createSpeechAudio();
});
afterEach(async () => {
  if (component) await unmount(component);
  component = null;
  owner.destroy();
  document.body.innerHTML = '';
  vi.restoreAllMocks();
});

it('starts unseen Session speech, keeps it through navigation and shares controls without replay', async () => {
  const onOpenSession = vi.fn();
  const props = reactiveProps({ owner, visible: false, onOpenSession });
  component = mount(Host, { target: document.body, props });
  owner.handleServerEvent(speechEvent());
  await flush();
  expect(streams[0].play).toHaveBeenCalledOnce();
  expect(
    document.querySelector(`[aria-label="${t('audio.pause')}"]`),
  ).toBeTruthy();
  document.querySelector(`[aria-label="${t('audio.openSession')}"]`).click();
  expect(onOpenSession).toHaveBeenCalledWith(
    expect.objectContaining({ agentId: 'agent-one', sessionId: 'session-one' }),
  );

  streams[0].report({ currentTime: 4 });
  props.visible = true;
  await flush();
  expect(document.querySelector('.audio-player__time').textContent).toContain(
    '0:04',
  );
  expect(streams[0].stop).not.toHaveBeenCalled();
  document.querySelector(`[aria-label="${t('audio.pause')}"]`).click();
  await flush();
  expect(streams[0].pause).toHaveBeenCalledOnce();
  props.visible = false;
  await flush();
  document.querySelector(`[aria-label="${t('audio.play')}"]`).click();
  await flush();
  expect(streams[0].play).toHaveBeenCalledTimes(2);

  owner.handleServerEvent(speechEvent('one', 'tool_call_result'));
  owner.handleServerEvent(speechEvent()); // reconnect replay
  owner.handleServerEvent({
    type: 'run_completed',
    payload: { run_id: 'run-one' },
  });
  props.src = '/api/speech/artifacts/one';
  props.visible = true;
  await flush();
  expect(streams).toHaveLength(1);
  expect(streams[0].play).toHaveBeenCalledTimes(2);
  expect(streams[0].stop).not.toHaveBeenCalled();
  streams[0].report({ ended: true, paused: true, currentTime: 8, duration: 8 });
  await flush();
  expect(owner.active).toBeNull();
  expect(
    document.querySelector(`[aria-label="${t('audio.seek')}"]`).disabled,
  ).toBe(false);
  props.visible = false;
  await flush();
  props.visible = true;
  await flush();
  expect(document.querySelector('.audio-player__time').textContent).toContain(
    '0:08',
  );
  expect(play).not.toHaveBeenCalled();
});

it('plays simultaneous Session outputs in order and uses durable audio after waiting', async () => {
  owner.handleServerEvent(speechEvent('one'));
  owner.handleServerEvent(speechEvent('two'));
  expect(streams[0].play).toHaveBeenCalledOnce();
  expect(streams[1].play).not.toHaveBeenCalled();
  expect(owner.pending).toBe(1);
  owner.handleServerEvent(speechEvent('two', 'tool_call_result'));
  expect(streams[1].stop).toHaveBeenCalledOnce();
  streams[0].report({ ended: true, paused: true, currentTime: 5, duration: 5 });
  await flush();
  expect(owner.active.sessionId).toBe('session-two');
  expect(owner.pending).toBe(0);
  expect(play).toHaveBeenCalledOnce();
  expect(play.mock.instances[0].getAttribute('src')).toBe(
    '/api/speech/artifacts/two',
  );
});

it('removes active and waiting speech on cancellation and ignores replay after it', async () => {
  owner.handleServerEvent(speechEvent());
  const second = speechEvent('two');
  second.payload.run_id = 'run-one';
  owner.handleServerEvent(second);
  owner.handleServerEvent({
    type: 'run_cancelled',
    payload: { run_id: 'run-one' },
  });
  owner.handleServerEvent(speechEvent('one', 'tool_call_result'));
  await flush();
  expect(owner.active).toBeNull();
  expect(owner.pending).toBe(0);
  expect(streams[0].stop).toHaveBeenCalledOnce();
  expect(streams[1].stop).toHaveBeenCalledOnce();
  expect(play).not.toHaveBeenCalled();
});

it.each(['run_failed', 'run_interrupted'])(
  'plays active and waiting speech to the end after %s',
  async (type) => {
    owner.handleServerEvent(speechEvent());
    const second = speechEvent('two');
    second.payload.run_id = 'run-one';
    owner.handleServerEvent(second);
    owner.handleServerEvent({ type, payload: { run_id: 'run-one' } });
    await flush();
    expect(streams[0].stop).not.toHaveBeenCalled();
    expect(owner.pending).toBe(1);
    streams[0].report({
      ended: true,
      paused: true,
      currentTime: 5,
      duration: 5,
    });
    await flush();
    expect(owner.active.key).toBe('run-one:call-two');
    expect(owner.pending).toBe(0);
    expect(streams[1].play).toHaveBeenCalled();
  },
);

it('keeps historical speech paused and stops all background speech from the shared control', async () => {
  component = mount(Host, {
    target: document.body,
    props: {
      owner,
      visible: true,
      src: '/api/speech/artifacts/one',
    },
  });
  await flush();
  expect(play).not.toHaveBeenCalled();
  await unmount(component);
  component = mount(Host, { target: document.body, props: { owner } });
  owner.handleServerEvent(speechEvent('two'));
  owner.handleServerEvent(speechEvent('three'));
  await flush();
  document.querySelector(`[aria-label="${t('audio.stopSpeech')}"]`).click();
  await flush();
  expect(owner.active).toBeNull();
  expect(owner.pending).toBe(0);
  expect(streams[0].stop).toHaveBeenCalledOnce();
  owner.handleServerEvent(speechEvent('three', 'tool_call_result'));
  expect(play).not.toHaveBeenCalled();
});

it('names the shown Session as the origin of speech replayed from history', async () => {
  component = mount(Host, {
    target: document.body,
    props: {
      owner,
      visible: true,
      src: '/api/speech/artifacts/one',
      origin: { agentId: 'agent-one', sessionId: 'session-one' },
    },
  });
  await flush();
  document.querySelector(`[aria-label="${t('audio.play')}"]`).click();
  await flush();
  expect(owner.active.agentId).toBe('agent-one');
  expect(owner.active.sessionId).toBe('session-one');
});

it('lets a fresh speech take over from one the user paused part of the way', async () => {
  owner.handleServerEvent(speechEvent('one'));
  streams[0].report({ paused: true, currentTime: 2, duration: 5 });
  owner.handleServerEvent(speechEvent('two'));
  await flush();
  expect(owner.active.sessionId).toBe('session-two');
  expect(owner.pending).toBe(0);
  expect(streams[1].play).toHaveBeenCalledOnce();
  expect(streams[0].stop).not.toHaveBeenCalled();
});

it('releases speech on a server replacement and on App teardown', () => {
  owner.handleServerEvent({ type: 'connection_ready', epoch: 'first' });
  owner.handleServerEvent(speechEvent());
  owner.handleServerEvent({ type: 'connection_ready', epoch: 'second' });
  expect(streams[0].stop).toHaveBeenCalledOnce();
  expect(owner.active).toBeNull();
  owner.handleServerEvent(speechEvent('two'));
  owner.destroy();
  expect(streams[1].stop).toHaveBeenCalledOnce();
  expect(owner.active).toBeNull();
});

it('keeps a failed background output visible for retry without starting the waiting output', async () => {
  play.mockRejectedValueOnce(new Error('Audio output unavailable'));
  component = mount(Host, { target: document.body, props: { owner } });
  owner.handleServerEvent(speechEvent('one', 'tool_call_result'));
  owner.handleServerEvent(speechEvent('two', 'tool_call_result'));
  await flush();
  expect(document.body.textContent).toContain(t('audio.playFailed'));
  expect(owner.active.sessionId).toBe('session-one');
  expect(owner.pending).toBe(1);
  expect(play).toHaveBeenCalledOnce();
  document.querySelector(`[aria-label="${t('audio.play')}"]`).click();
  await flush();
  expect(document.body.textContent).not.toContain(t('audio.playFailed'));
  expect(play).toHaveBeenCalledTimes(2);
  owner.active.resource.audio.dispatchEvent(new Event('ended'));
  await flush();
  expect(owner.active.sessionId).toBe('session-two');
  expect(owner.pending).toBe(0);
  expect(play).toHaveBeenCalledTimes(3);
});
