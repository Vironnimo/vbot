// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { init } from '$lib/i18n.js';
import { fileURLToPath } from 'node:url';
import { readStyleSheet } from '../../../__tests__/styles.support.js';

const api = vi.hoisted(() => ({
  listDecisionExperiments: vi.fn(),
  getDecisionExperiment: vi.fn(),
  saveDecisionExperiment: vi.fn(),
  deleteDecisionExperiment: vi.fn(),
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
const { default: View } = await import('../JevView.svelte');
let component;
afterEach(async () => {
  if (component) await unmount(component);
  document.body.innerHTML = '';
});
it('shows the workspace under the real shell cascade and opens a saved example', async () => {
  init('en');
  api.listDecisionExperiments.mockResolvedValue({
    experiments: [],
    available: true,
  });
  api.getDecisionHistory.mockResolvedValue({
    evaluations: [],
    next_before: null,
  });
  api.saveDecisionExperiment.mockImplementation(async (draft) => ({
    id: 'exp',
    revision: 1,
    draft,
  }));
  const style = document.createElement('style');
  style.textContent = ['../../../styles/app.css', '../jev.css']
    .map((path) =>
      readStyleSheet(fileURLToPath(new URL(path, import.meta.url))),
    )
    .join('\n');
  document.body.append(style);
  component = mount(View, { target: document.body });
  async function settle() {
    for (let n = 0; n < 20; n++) {
      await Promise.resolve();
      flushSync();
    }
  }
  await settle();
  expect(getComputedStyle(document.querySelector('.jev-view')).display).toBe(
    'flex',
  );
  const button = [...document.querySelectorAll('button')].find(
    (node) => node.textContent.trim() === 'Open support triage example',
  );
  button.click();
  await settle();
  expect(api.saveDecisionExperiment).toHaveBeenCalledOnce();
  expect(document.querySelectorAll('.jev-question')).toHaveLength(3);
  expect(document.querySelector('.jev-editor')).not.toBeNull();
});
