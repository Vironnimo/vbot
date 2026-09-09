// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { init, t } from '../../lib/i18n.js';

const status = vi.fn();
const install = vi.fn();
const preview = vi.fn();
vi.mock(
  'svelte',
  async () => import('../../../node_modules/svelte/src/index-client.js'),
);
vi.mock('$lib/api.js', () => ({
  getLocalSpeechSetup: (...args) => status(...args),
  installLocalSpeechSupport: (...args) => install(...args),
  restartAfterLocalSpeechSetup: vi.fn(),
  previewSpeech: (...args) => preview(...args),
}));
const { default: LocalSpeechSupport } =
  await import('../settings/LocalSpeechSupport.svelte');
let components = [];
beforeEach(() => {
  init('en');
  status.mockReset().mockResolvedValue({ state: 'ready' });
  install
    .mockReset()
    .mockResolvedValue({ state: 'installing', phase: 'python' });
  preview.mockReset();
});
afterEach(async () => {
  for (const component of components) await unmount(component);
  components = [];
  document.body.innerHTML = '';
  vi.useRealTimers();
});
async function flush() {
  for (let n = 0; n < 10; n++) {
    await Promise.resolve();
    flushSync();
  }
}
function render(target = 'local/qwen3-tts', extra = {}) {
  const component = mount(LocalSpeechSupport, {
    target: document.body,
    props: { target, tts: true, ...extra },
  });
  components.push(component);
  return component;
}
function button(key) {
  return [...document.querySelectorAll('button')].find(
    (node) => node.textContent.trim() === t(key),
  );
}

it('streams preview status and exposes a playable result, then unlocks retry', async () => {
  let finish;
  preview.mockImplementation(
    () => new Promise((resolve) => (finish = resolve)),
  );
  render();
  await flush();
  button('settings.localSpeech.previewButton').click();
  await flush();
  const options = preview.mock.calls[0][1];
  expect(options.signal.aborted).toBe(false);
  expect(button('settings.localSpeech.previewButton').disabled).toBe(true);
  options.onProgress({ phase: 'downloading', elapsed_seconds: 42 });
  await flush();
  expect(
    [...document.querySelectorAll('[role="status"]')].some((node) =>
      node.textContent.includes('42s'),
    ),
  ).toBe(true);
  options.onProgress({ phase: 'loading', elapsed_seconds: 48 });
  await flush();
  expect(document.body.textContent).toContain(t('chat.voice.progress.loading'));
  finish({ url: '/api/speech/artifacts/aud_test' });
  await flush();
  expect(document.querySelector('audio').getAttribute('src')).toBe(
    '/api/speech/artifacts/aud_test',
  );
  expect(button('settings.localSpeech.previewButton').disabled).toBe(false);
});

it('recovers after a generation error and aborts a preview when leaving', async () => {
  preview.mockRejectedValueOnce(new Error('test-owned failure'));
  const component = render();
  await flush();
  button('settings.localSpeech.previewButton').click();
  await flush();
  expect(document.querySelector('[role="alert"]').textContent).toBe(
    'test-owned failure',
  );
  expect(button('settings.localSpeech.previewButton').disabled).toBe(false);
  preview.mockImplementation(() => new Promise(() => {}));
  button('settings.localSpeech.previewButton').click();
  await flush();
  const signal = preview.mock.calls.at(-1)[1].signal;
  await unmount(component);
  components = [];
  expect(signal.aborted).toBe(true);
});

it('installs only the selected engine and refreshes availability without restarting', async () => {
  vi.useFakeTimers();
  status.mockResolvedValue({ state: 'missing' });
  const onReady = vi.fn();
  render('local/chatterbox', { onReady });
  await flush();
  expect(status).toHaveBeenCalledWith({ target: 'local/chatterbox' });
  button('settings.localSpeech.installButton').click();
  await flush();
  expect(install).toHaveBeenCalledExactlyOnceWith({
    target: 'local/chatterbox',
  });
  expect(button('settings.localSpeech.installingButton').disabled).toBe(true);
  status.mockResolvedValue({ state: 'ready' });
  await vi.advanceTimersByTimeAsync(1500);
  await flush();
  expect(onReady).toHaveBeenCalledOnce();
  expect(button('settings.localSpeech.previewButton')).toBeTruthy();
  expect(button('settings.localSpeech.restartButton')).toBeUndefined();
});
