// @vitest-environment jsdom

import { expect, it, vi } from 'vitest';
import { createRawSnippet, flushSync, mount, tick, unmount } from 'svelte';
import { init, t } from '../../../lib/i18n.js';

vi.mock(
  'svelte',
  async () => import('../../../../node_modules/svelte/src/index-client.js'),
);
const { default: SessionPopupWindow } =
  await import('../SessionPopupWindow.svelte');

it('docks a moved chat and restores its position, contents and focus', async () => {
  init('en');
  const onClose = vi.fn();
  const component = mount(SessionPopupWindow, {
    target: document.body,
    props: {
      title: 'Test chat',
      active: true,
      onClose,
      children: createRawSnippet(() => ({
        render: () => '<textarea></textarea>',
      })),
    },
  });
  try {
    flushSync();
    const panel = document.querySelector('[role="dialog"]');
    const header = panel.querySelector('header');
    panel.setPointerCapture = vi.fn();
    const movePointer = (type, x, y) => {
      const event = new MouseEvent(type, {
        clientX: x,
        clientY: y,
        bubbles: true,
      });
      Object.assign(event, { isPrimary: true, pointerId: 2 });
      header.dispatchEvent(event);
    };
    movePointer('pointerdown', 8, 10);
    movePointer('pointermove', 40, 60);
    movePointer('pointerup', 40, 60);
    flushSync();
    expect(panel.setPointerCapture).toHaveBeenCalledWith(2);
    expect(panel.style.getPropertyValue('--popup-x')).toBe('32px');
    expect(panel.style.getPropertyValue('--popup-y')).toBe('50px');

    const input = panel.querySelector('textarea');
    input.focus();
    input.value = 'Unsent question';
    panel
      .querySelector(`button[aria-label="${t('appChat.minimize')}"]`)
      .click();
    await tick();
    const task = document.querySelector('.session-popup-task');
    expect(task.getAttribute('aria-label')).toBe(
      t('appChat.restore', { title: 'Test chat' }),
    );
    await vi.waitFor(() => expect(document.activeElement).toBe(task));
    expect(panel.hidden).toBe(true);
    expect(panel.inert).toBe(true);
    task.click();
    await tick();
    expect(panel.hidden).toBe(false);
    await vi.waitFor(() => expect(document.activeElement).toBe(input));
    expect(input.value).toBe('Unsent question');
    expect(panel.style.getPropertyValue('--popup-x')).toBe('32px');
    expect(onClose).not.toHaveBeenCalled();
  } finally {
    await unmount(component);
  }
});
