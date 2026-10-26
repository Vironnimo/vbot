// @vitest-environment jsdom

import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init, t } from '../../lib/i18n.js';

const api = vi.hoisted(() => ({ removeComputer: vi.fn() }));
vi.mock(
  'svelte',
  async () => import('../../../node_modules/svelte/src/index-client.js'),
);
vi.mock('$lib/api.js', () => api);
const { default: Panel } =
  await import('../settings/SettingsComputersPanel.svelte');
const { default: Host } = await import('./ComputerContextHost.support.svelte');

let component;
let listed;

async function waitForCondition(check, attempts = 50) {
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    flushSync();
    if (check()) return;
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
  throw new Error('Condition was not met');
}

beforeEach(() => {
  init('en');
  listed = [
    { id: 'server', name: 'Server', online: true, capabilities: ['terminal'] },
    {
      id: 'pc_laptop',
      name: 'Laptop',
      online: true,
      capabilities: ['terminal'],
    },
    { id: 'pc_old', name: 'Old PC', online: false, capabilities: [] },
  ];
  api.removeComputer.mockReset();
});

afterEach(() => {
  if (component) unmount(component);
  component = null;
  document.body.innerHTML = '';
});

const removeButton = (name) =>
  document.querySelector(
    `button[aria-label="${t('computers.remove.aria', { name })}"]`,
  );

it('removes only offline Desktop computers after confirmation', async () => {
  const onToast = vi.fn();
  api.removeComputer.mockImplementation(async (id) => {
    listed = listed.filter((computer) => computer.id !== id);
    return { removed: id };
  });
  component = mount(Host, {
    target: document.body,
    props: {
      component: Panel,
      componentProps: { onToast },
      readComputers: async () => ({ computers: listed }),
    },
  });
  await waitForCondition(() => document.body.textContent.includes('Old PC'));
  expect(document.body.textContent).not.toContain('Server');
  expect(removeButton('Laptop')).toBeNull();

  removeButton('Old PC').click();
  flushSync();
  [...document.querySelectorAll('button')]
    .find(
      (button) => button.textContent.trim() === t('computers.remove.confirm'),
    )
    .click();
  await waitForCondition(() => !document.body.textContent.includes('Old PC'));
  expect(api.removeComputer).toHaveBeenCalledWith('pc_old');
  expect(onToast).toHaveBeenCalledWith({
    title: t('computers.remove.done', { name: 'Old PC' }),
    variant: 'success',
  });
  expect(document.body.textContent).toContain('Laptop');
});
