// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { init, t } from '$lib/i18n.js';
import { fileURLToPath } from 'node:url';
import { readStyleSheet } from '../../../__tests__/styles.support.js';
import { createStandaloneNavigation } from '$lib/navigation.svelte.js';

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
it('shows the workspace under the real shell cascade, opens a saved example and follows the place', async () => {
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
  const navigation = createStandaloneNavigation();
  component = mount(View, { target: document.body, props: { navigation } });
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
    (node) => node.textContent.trim() === t('jev.example.triage'),
  );
  button.click();
  await settle();
  expect(api.saveDecisionExperiment).toHaveBeenCalledOnce();
  expect(document.querySelectorAll('.jev-question')).toHaveLength(3);
  expect(document.querySelector('.jev-editor')).not.toBeNull();
  expect(document.querySelector('.jev-library')).toBeNull();
  expect(document.querySelector('[aria-label="State"]').value).toBe(
    api.saveDecisionExperiment.mock.calls[0][0].state,
  );
  expect(document.querySelector('[aria-label="Answer ID"]')).toBeNull();
  expect(['', 'static']).toContain(
    getComputedStyle(document.querySelector('.jev-submit')).position,
  );
  expect(document.querySelector('.jev-history').hidden).toBe(true);
  // Creating opens the experiment as a step; its shown page names the entry.
  expect(navigation.place).toEqual(['exp', 'setup']);
  [...document.querySelectorAll('button')]
    .find((node) => node.textContent.includes('←'))
    .click();
  await settle();
  expect(navigation.place).toEqual([]);
  expect(document.querySelector('.jev-library')).not.toBeNull();
  expect(document.querySelector('.jev-editor')).toBeNull();

  api.getDecisionExperiment.mockResolvedValue({
    id: 'exp',
    revision: 1,
    draft: api.saveDecisionExperiment.mock.calls[0][0],
  });
  navigation.navigate(['exp', 'results']);
  await settle();
  expect(api.getDecisionExperiment).toHaveBeenCalledWith('exp');
  expect(document.querySelector('.jev-history').hidden).toBe(false);
});
