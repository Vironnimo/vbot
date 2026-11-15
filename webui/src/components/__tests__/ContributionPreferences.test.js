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
