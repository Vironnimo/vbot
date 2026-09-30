// Shared harness for the SettingsSpecializedModelsPanel suites: `api` holds
// one mock per task-model and local-speech API function the panel calls, plus
// the Settings read a refused save rebases onto.

import { expect, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../lib/i18n.js';

export const api = {
  getSettings: vi.fn(),
  listTaskModelTargets: vi.fn(),
  getTaskModelOptions: vi.fn(),
  updateTaskModelSettings: vi.fn(),
  getLocalSpeechSetup: vi.fn(),
  getLocalSpeechMemory: vi.fn(),
  unloadLocalSpeech: vi.fn(),
  installLocalSpeechSupport: vi.fn(),
  restartAfterLocalSpeechSetup: vi.fn(),
};

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

vi.mock('$lib/api.js', () =>
  Object.fromEntries(
    Object.keys(api).map((name) => [name, (...args) => api[name](...args)]),
  ),
);

const { default: SettingsSpecializedModelsPanel } =
  await import('../settings/SettingsSpecializedModelsPanel.svelte');

let mounted = null;

export function resetSpecializedModelsHarness() {
  document.body.innerHTML = '';
  init('en');
  api.getSettings.mockReset().mockResolvedValue({ model_tasks: {} });
  api.listTaskModelTargets.mockReset().mockResolvedValue({ targets: [] });
  api.getTaskModelOptions.mockReset().mockResolvedValue({ fields: [] });
  api.updateTaskModelSettings
    .mockReset()
    .mockResolvedValue({ model_tasks: {} });
  api.getLocalSpeechMemory.mockReset().mockResolvedValue({ models: [] });
  api.unloadLocalSpeech.mockReset().mockResolvedValue({
    models: [
      {
        target: 'local/chatterbox',
        label: 'Test loaded voice',
        loaded: false,
        busy: false,
      },
    ],
    released: true,
  });
  api.getLocalSpeechSetup
    .mockReset()
    .mockResolvedValue({ state: 'ready', restart_available: true });
  api.installLocalSpeechSupport.mockReset().mockResolvedValue({
    state: 'installing',
    phase: 'downloading',
    restart_available: true,
  });
  api.restartAfterLocalSpeechSetup
    .mockReset()
    .mockResolvedValue({ state: 'restarting' });
}

export function mountPanel(props = {}) {
  mounted = mount(SettingsSpecializedModelsPanel, {
    target: document.body,
    props,
  });
  flushSync();
  return mounted;
}

export async function unmountPanel() {
  if (mounted) {
    await unmount(mounted);
    mounted = null;
  }
  document.body.innerHTML = '';
}

export async function cleanupSpecializedModelsHarness() {
  await unmountPanel();
  vi.useRealTimers();
}

// Targets offered for one task type; every other task type lists none.
export function targetsFor(taskType, targets) {
  api.listTaskModelTargets.mockImplementation(async (requested) => ({
    targets: requested === taskType ? targets : [],
  }));
}

export async function waitForCondition(check, attempts = 20, delayMs = 0) {
  for (let index = 0; index < attempts; index += 1) {
    await Promise.resolve();
    await new Promise((resolve) => setTimeout(resolve, delayMs));
    flushSync();
    if (check()) {
      return;
    }
  }
  throw new Error('Timed out waiting for condition.');
}

export async function settle() {
  for (let index = 0; index < 20; index += 1) {
    await Promise.resolve();
    flushSync();
  }
}

export function selectTarget(taskType, label) {
  document
    .getElementById(`settings-specialized-${taskType}`)
    .dispatchEvent(new MouseEvent('click', { bubbles: true }));
  flushSync();
  const option = Array.from(
    document.body.querySelectorAll('.searchable-dropdown__option'),
  ).find((candidate) => candidate.textContent.includes(label));
  expect(option).toBeTruthy();
  option.dispatchEvent(new MouseEvent('click', { bubbles: true }));
  flushSync();
}

export function optionLabels() {
  return [...document.querySelectorAll('[role="option"]')].map((option) =>
    option.textContent.trim(),
  );
}

export function button(label) {
  return [...document.querySelectorAll('button')].find(
    (element) => element.textContent.trim() === label,
  );
}

export function deferred() {
  let resolve;
  const promise = new Promise((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}
