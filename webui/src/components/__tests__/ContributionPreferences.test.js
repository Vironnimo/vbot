// @vitest-environment jsdom

import { afterEach, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { init, t } from '../../lib/i18n.js';

vi.mock(
  'svelte',
  async () => import('../../../node_modules/svelte/src/index-client.js'),
);
const { default: ContributionContextHost } =
  await import('./ContributionContextHost.support.svelte');
const { default: ContributionPreferences } =
  await import('../extensions/ContributionPreferences.svelte');
let component;

afterEach(async () => {
  if (component) await unmount(component);
  component = null;
  localStorage.clear();
  document.body.innerHTML = '';
});

it('configures native composer controls before Chat has ever mounted and refines mounted labels', async () => {
  init('en');
  localStorage.clear();
  let registry;
  component = mount(ContributionContextHost, {
    target: document.body,
    props: {
      component: ContributionPreferences,
      source: {
        settings: { appearance: { ui_layout: {} } },
        writeSettings: async (params) => ({ appearance: params.appearance }),
      },
      onRegistry: (value) => {
        registry = value;
      },
    },
  });
  flushSync();
  expect(registry.mounted).toEqual({});
  const rows = () => [...document.querySelectorAll('[data-sortable-key]')];
  expect(rows().map((row) => row.dataset.sortableKey)).toEqual([
    'core:composer.microphone',
    'core:composer.attachment',
    'core:composer.stop',
    'core:composer.send',
  ]);
  const send = rows().at(-1);
  expect(send.textContent).toContain(t('chat.sendMessage'));
  send.querySelector('[role="switch"]').click();
  flushSync();
  expect(registry.layout('chat.composer.actions').hidden).toEqual([
    'core:composer.send',
  ]);
  send.dispatchEvent(
    new KeyboardEvent('keydown', {
      key: 'ArrowUp',
      altKey: true,
      bubbles: true,
    }),
  );
  await Promise.resolve();
  flushSync();
  expect(registry.layout('chat.composer.actions').order).toEqual([
    'core:composer.microphone',
    'core:composer.attachment',
    'core:composer.send',
    'core:composer.stop',
  ]);
  const unregister = registry.register('chat.composer.actions', [
    { id: 'core:composer.send', title: 'Queue this message', order: 100 },
  ]);
  flushSync();
  expect(rows()).toHaveLength(4);
  expect(rows()[2].textContent).toContain('Queue this message');
  unregister();
});

it('offers explicit browser migration and keeps failed shared saves recoverable', async () => {
  init('en');
  localStorage.setItem(
    'vbot.ui-layout.v1',
    JSON.stringify({
      'chat.composer.actions': { hidden: ['core:composer.send'] },
    }),
  );
  const writeSettings = vi
    .fn()
    .mockRejectedValueOnce(new Error('offline'))
    .mockImplementation(async (params) => ({ appearance: params.appearance }));
  let registry;
  component = mount(ContributionContextHost, {
    target: document.body,
    props: {
      component: ContributionPreferences,
      source: { settings: { appearance: { ui_layout: null } }, writeSettings },
      onRegistry: (value) => {
        registry = value;
      },
    },
  });
  flushSync();
  expect(document.querySelector('fieldset').disabled).toBe(true);
  const button = (label) =>
    [...document.querySelectorAll('button')].find(
      (item) => item.textContent.trim() === label,
    );
  button(t('contributions.importLayout')).click();
  await vi.waitFor(() => expect(registry.error).toContain('offline'));
  flushSync();
  expect(document.body.textContent).toContain('offline');
  expect(localStorage.getItem('vbot.ui-layout.v1')).not.toBeNull();
  button(t('common.save')).click();
  await vi.waitFor(() => expect(registry.pending).toBe(false));
  flushSync();
  expect(document.querySelector('fieldset').disabled).toBe(false);
  expect(localStorage.getItem('vbot.ui-layout.v1')).toBeNull();
  expect(
    writeSettings.mock.calls[1][0].appearance.ui_layout['chat.composer.actions']
      .hidden,
  ).toEqual(['core:composer.send']);
});
