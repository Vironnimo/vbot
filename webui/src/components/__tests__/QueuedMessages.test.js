// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../lib/i18n.js';

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

const { default: QueuedMessages } = await import('../QueuedMessages.svelte');

describe('QueuedMessages', () => {
  let mountedComponent;

  beforeEach(() => {
    document.body.innerHTML = '';
    init('en');
    mountedComponent = null;
  });

  afterEach(async () => {
    if (mountedComponent) {
      await unmount(mountedComponent);
      mountedComponent = null;
    }
    document.body.innerHTML = '';
  });

  it('keeps a failed Queue edit open with its unsaved content', async () => {
    const onEditQueuedMessage = vi.fn().mockResolvedValue(false);
    mountedComponent = mount(QueuedMessages, {
      target: document.body,
      props: {
        queuedMessages: [
          { id: 'queue-one', content: 'Original', editable: true },
        ],
        onEditQueuedMessage,
      },
    });
    flushSync();

    button('Edit queued message').click();
    flushSync();
    const editor = document.body.querySelector('.queued-messages__editor');
    editor.value = 'Unsaved change';
    editor.dispatchEvent(new InputEvent('input', { bubbles: true }));
    flushSync();
    button('Save').click();

    await vi.waitFor(() => {
      expect(onEditQueuedMessage).toHaveBeenCalledWith(
        'queue-one',
        'Unsaved change',
      );
      expect(document.body.querySelector('.queued-messages__editor')).toBe(
        editor,
      );
      expect(
        document.body.querySelector('.queued-messages__error'),
      ).toBeTruthy();
    });
    expect(editor.value).toBe('Unsaved change');
  });

  it('does not offer text editing for a Queue item with attachments', () => {
    mountedComponent = mount(QueuedMessages, {
      target: document.body,
      props: {
        queuedMessages: [
          { id: 'queue-file', content: '[attachment]', editable: false },
        ],
      },
    });
    flushSync();

    expect(button('Edit queued message')).toBeUndefined();
    expect(button('Remove queued message')).toBeTruthy();
    expect(document.body.querySelector('.queued-messages__editor')).toBeNull();
  });
});

function button(ariaLabel) {
  return Array.from(document.body.querySelectorAll('button')).find(
    (candidate) => candidate.getAttribute('aria-label') === ariaLabel,
  );
}
