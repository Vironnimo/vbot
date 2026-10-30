// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { init, t } from '../../../lib/i18n.js';
import { reactiveProps } from '../../__tests__/reactiveProps.support.svelte.js';

vi.mock(
  'svelte',
  async () => import('../../../../node_modules/svelte/src/index-client.js'),
);
vi.mock('$lib/api.js', async (original) => ({
  ...(await original()),
  debugSpeechList: vi.fn(),
  debugSpeechClear: vi.fn(),
}));
const api = await import('$lib/api.js');
const { default: Panel } = await import('../DebugSpeechRecordings.svelte');
let component;
let props;
let play;
const record = (id, text = 'Licht an.') => ({
  recording_id: id,
  text,
  error: null,
  timestamp: '2026-10-30T16:00:00+00:00',
  audio_url: `/api/debug/speech/${id}/audio`,
});
beforeEach(() => {
  init('en');
  document.body.innerHTML = '';
  api.debugSpeechList
    .mockReset()
    .mockResolvedValue({ recordings: [record('stt_first')] });
  api.debugSpeechClear.mockReset().mockResolvedValue({ cleared: true });
  play = vi.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue();
  vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => {});
  vi.spyOn(HTMLMediaElement.prototype, 'load').mockImplementation(() => {});
});
afterEach(async () => {
  if (component) await unmount(component);
  component = null;
  vi.restoreAllMocks();
});
async function settle() {
  for (let index = 0; index < 5; index++) {
    await Promise.resolve();
    flushSync();
  }
}
async function render(enabled = true) {
  props = reactiveProps({
    active: true,
    settings: { debug: { enabled, speech_recording_limit: 10 } },
  });
  component = mount(Panel, { target: document.body, props });
  flushSync();
  await settle();
}
function button(label, root = document) {
  return [...root.querySelectorAll('button')].find(
    (node) =>
      (node.getAttribute('aria-label') || node.textContent.trim()) === label,
  );
}

it('gates reads on Debug and lazily releases playback on hiding, pruning and disabling', async () => {
  await render(false);
  expect(api.debugSpeechList).not.toHaveBeenCalled();
  props.settings = { debug: { enabled: true, speech_recording_limit: 10 } };
  await settle();
  expect(document.body.textContent).toContain('Licht an.');
  expect(document.querySelector('audio')).toBeNull();
  document.querySelector('summary').click();
  document.querySelector('details').dispatchEvent(new Event('toggle'));
  await settle();
  expect(document.querySelector('audio').getAttribute('src')).toBe(
    '/api/debug/speech/stt_first/audio',
  );
  expect(play).not.toHaveBeenCalled();
  props.active = false;
  await settle();
  expect(document.querySelector('audio')).toBeNull();
  props.active = true;
  api.debugSpeechList.mockResolvedValue({ recordings: [] });
  await settle();
  expect(document.querySelector('details')).toBeNull();
  props.settings = { debug: { enabled: false } };
  await settle();
  expect(document.querySelector('audio')).toBeNull();
});

it('ignores obsolete list replies and allows retrying a failed read', async () => {
  let finishOld;
  api.debugSpeechList.mockImplementationOnce(
    () => new Promise((resolve) => (finishOld = resolve)),
  );
  await render();
  props.debugTracesRefreshToken = 1;
  api.debugSpeechList.mockResolvedValue({
    recordings: [record('stt_new', 'new transcript')],
  });
  await settle();
  finishOld({ recordings: [record('stt_old', 'old transcript')] });
  await settle();
  expect(document.body.textContent).toContain('new transcript');
  expect(document.body.textContent).not.toContain('old transcript');
  api.debugSpeechList.mockRejectedValueOnce(new Error('read sentinel'));
  props.debugTracesRefreshToken = 2;
  await settle();
  expect(document.querySelector('[role="alert"]')).not.toBeNull();
  button(t('common.retry')).click();
  await settle();
  expect(document.querySelector('[role="alert"]')).toBeNull();
});

it('can clear retained recordings while capture is disabled and retries a failed clear', async () => {
  await render(false);
  api.debugSpeechClear.mockRejectedValueOnce(new Error('clear sentinel'));
  button(t('debug.speech.clear')).click();
  await settle();
  const dialog = document.querySelector('[role="dialog"]');
  button(t('common.delete'), dialog).click();
  await settle();
  expect(document.querySelector('[role="dialog"]')).not.toBeNull();
  expect(document.querySelector('[role="dialog"]').textContent).toContain(
    'clear sentinel',
  );
  button(t('common.delete'), dialog).click();
  await settle();
  expect(api.debugSpeechClear).toHaveBeenCalledTimes(2);
  expect(document.querySelector('[role="dialog"]')).toBeNull();
  expect(api.debugSpeechList).not.toHaveBeenCalled();
});
