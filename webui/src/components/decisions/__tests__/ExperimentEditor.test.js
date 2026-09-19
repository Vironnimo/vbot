// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { init } from '$lib/i18n.js';

const api = vi.hoisted(() => ({
  saveDecisionExperiment: vi.fn(),
  getDecisionHistory: vi.fn(),
  getDecisionResult: vi.fn(),
  startDecisionEvaluation: vi.fn(),
  cancelDecisionEvaluation: vi.fn(),
}));
vi.mock(
  'svelte',
  async () => import('../../../../node_modules/svelte/src/index-client.js'),
);
vi.mock('$lib/api.js', () => api);
const { default: Editor } = await import('../ExperimentEditor.svelte');
let component;
const draft = () => ({
  title: 'Fixture',
  state: 'A message',
  questions: [{ id: 'q', type: 'noul', instructions: 'Is this clear?' }],
});
async function flush() {
  for (let n = 0; n < 15; n++) {
    await Promise.resolve();
    flushSync();
  }
}
function button(label) {
  return [...document.querySelectorAll('button')].find(
    (node) => node.textContent.trim() === label,
  );
}
function input(node, value) {
  node.value = value;
  node.dispatchEvent(new Event('input', { bubbles: true }));
  flushSync();
}
beforeEach(() => {
  init('en');
  vi.useFakeTimers();
  Object.values(api).forEach((mock) => mock.mockReset());
  api.getDecisionHistory.mockResolvedValue({
    evaluations: [],
    next_before: null,
  });
  api.saveDecisionExperiment.mockImplementation(
    async (value, id, revision) => ({
      id,
      revision: revision + 1,
      draft: value,
    }),
  );
});
afterEach(async () => {
  if (component) await unmount(component);
  component = null;
  document.body.innerHTML = '';
  vi.useRealTimers();
});
function render(props = {}) {
  component = mount(Editor, {
    target: document.body,
    props: { experiment: { id: 'exp', revision: 1, draft: draft() }, ...props },
  });
}

it('preserves edits made during a slow save and evaluates the newest revision', async () => {
  let release;
  api.saveDecisionExperiment.mockImplementationOnce(
    (value, id) =>
      new Promise((resolve) => {
        release = () => resolve({ id, revision: 2, draft: value });
      }),
  );
  api.startDecisionEvaluation.mockResolvedValue({
    id: 'evl',
    status: 'running',
    created_at: '2026-09-19T12:00:00Z',
    snapshot: { questions: [] },
  });
  render();
  await flush();
  const title = document.querySelector('input');
  input(title, 'First');
  await vi.advanceTimersByTimeAsync(900);
  await flush();
  input(title, 'Second');
  release();
  await flush();
  button('Evaluate').click();
  await flush();
  expect(api.saveDecisionExperiment.mock.calls[1][0].title).toBe('Second');
  expect(api.saveDecisionExperiment.mock.calls[1][2]).toBe(2);
  expect(api.startDecisionEvaluation.mock.calls[0].slice(0, 2)).toEqual([
    'exp',
    3,
  ]);
  expect(title.value).toBe('Second');
});

it('offers explicit discard and reload after a concurrent revision conflict', async () => {
  const onReload = vi.fn();
  api.saveDecisionExperiment.mockRejectedValue(
    Object.assign(new Error('Changed elsewhere'), { code: 'conflict' }),
  );
  render({ onReload });
  await flush();
  input(document.querySelector('input'), 'My local edit');
  button('Save').click();
  await flush();
  expect(document.querySelector('[role=alert]').textContent).toBe(
    'Changed elsewhere',
  );
  button('Discard local edits and reload saved version').click();
  await flush();
  expect(onReload).toHaveBeenCalledOnce();
  expect(api.startDecisionEvaluation).not.toHaveBeenCalled();
});

it('blocks invalid JSON locally and never starts a request', async () => {
  render();
  await flush();
  button('JSON').click();
  await flush();
  button('Evaluate').click();
  await flush();
  expect(document.querySelector('[role="alert"]')).not.toBeNull();
  expect(api.startDecisionEvaluation).not.toHaveBeenCalled();
  expect(api.saveDecisionExperiment).not.toHaveBeenCalled();
});

it('unmounting an active control removes observers without cancelling execution', async () => {
  const record = {
    id: 'evl',
    status: 'running',
    created_at: '2026-09-19T12:00:00Z',
    snapshot: { questions: [], mode: 'control' },
  };
  api.getDecisionHistory.mockResolvedValue({
    evaluations: [record],
    next_before: null,
  });
  api.getDecisionResult.mockResolvedValue(record);
  render();
  await flush();
  expect(button('Cancel evaluation')).toBeTruthy();
  await unmount(component);
  component = null;
  const calls = api.getDecisionResult.mock.calls.length;
  await vi.advanceTimersByTimeAsync(2000);
  expect(api.cancelDecisionEvaluation).not.toHaveBeenCalled();
  expect(api.getDecisionResult).toHaveBeenCalledTimes(calls);
});

it('starts control explicitly with a saved setup and a distinct mode', async () => {
  render();
  await flush();
  button('Application control').click();
  await flush();
  const goal = document.querySelector('textarea');
  input(goal, 'Keep the job moving');
  api.startDecisionEvaluation.mockRejectedValue(
    new Error('fixture validation'),
  );
  button('Start control').click();
  await flush();
  expect(api.saveDecisionExperiment.mock.calls[0][0].control.instructions).toBe(
    'Keep the job moving',
  );
  expect(api.startDecisionEvaluation.mock.calls[0][3]).toBe('control');
});
