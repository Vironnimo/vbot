// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../lib/i18n.js';
import {
  FLOATING_HOVER_CLOSE_DELAY_MS,
  HOVER_CARD_SHOW_DELAY_MS,
} from '../../lib/tooltip.js';

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

  it('shows the complete queued text on hover and focus and sends Steer once', async () => {
    let resolveSteer;
    const onSteerQueuedMessage = vi.fn(
      () =>
        new Promise((resolve) => {
          resolveSteer = resolve;
        }),
    );
    const content = 'Long message '.repeat(100) + 'THE END';
    mountedComponent = mount(QueuedMessages, {
      target: document.body,
      props: {
        queuedMessages: [{ id: 'q', content, editable: true, steerable: true }],
        canSteer: true,
        onSteerQueuedMessage,
      },
    });
    flushSync();
    const anchor = document.querySelector('.queued-messages__preview');
    anchor.dispatchEvent(new MouseEvent('pointerenter'));
    flushSync();
    const card = document.querySelector('[data-floating-hover-card]');
    expect(card.textContent).toBe(content);
    anchor.querySelector('button').focus();
    flushSync();
    expect(card.getAttribute('aria-hidden')).toBe('false');
    button('Steer').click();
    flushSync();
    expect(button('Steer').disabled).toBe(true);
    button('Steer').click();
    expect(onSteerQueuedMessage).toHaveBeenCalledTimes(1);
    expect(onSteerQueuedMessage).toHaveBeenCalledWith('q');
    resolveSteer(false);
    await vi.waitFor(() => expect(button('Steer').disabled).toBe(false));
  });

  it('opens the full-text card after hover intent, at once on keyboard focus', () => {
    vi.useFakeTimers();
    try {
      const content = 'First line\nSecond line with more detail';
      mountedComponent = mount(QueuedMessages, {
        target: document.body,
        props: { queuedMessages: [{ id: 'q', content, editable: true }] },
      });
      flushSync();
      const anchor = document.querySelector('.queued-messages__preview');
      const preview = anchor.querySelector('button');
      const card = document.querySelector('.queued-messages__full');

      expect(card.parentElement).toBe(document.body);
      expect(card.classList.contains('floating-card')).toBe(true);
      anchor.dispatchEvent(new MouseEvent('pointerenter'));
      vi.advanceTimersByTime(HOVER_CARD_SHOW_DELAY_MS - 1);
      expect(card.dataset.floatingOpen).toBe('false');
      vi.advanceTimersByTime(1);
      expect(card.dataset.floatingOpen).toBe('true');
      expect(card.getAttribute('role')).toBe('tooltip');

      anchor.dispatchEvent(new MouseEvent('pointerleave'));
      vi.advanceTimersByTime(FLOATING_HOVER_CLOSE_DELAY_MS);
      expect(card.dataset.floatingOpen).toBe('false');

      window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab' }));
      preview.focus();
      expect(preview.tabIndex).toBe(0);
      expect(card.dataset.floatingOpen).toBe('true');
      expect(preview.getAttribute('aria-describedby')).toBe(card.id);
      expect(card.textContent).toBe(content);
    } finally {
      vi.useRealTimers();
    }
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

  it('opens the editor when an editable preview is clicked', () => {
    mountedComponent = mount(QueuedMessages, {
      target: document.body,
      props: {
        queuedMessages: [
          { id: 'queue-one', content: 'Original', editable: true },
          { id: 'queue-file', content: '[attachment]', editable: false },
        ],
      },
    });
    flushSync();

    const previews = document.body.querySelectorAll(
      '.queued-messages__content',
    );
    previews[1].click();
    flushSync();
    expect(document.body.querySelector('.queued-messages__editor')).toBeNull();

    previews[0].click();
    flushSync();
    expect(document.body.querySelector('.queued-messages__editor').value).toBe(
      'Original',
    );
  });

  it('focuses the editor it opens and keeps a modified draft open', async () => {
    mountedComponent = mount(QueuedMessages, {
      target: document.body,
      props: {
        queuedMessages: [
          { id: 'queue-one', content: 'First', editable: true },
          { id: 'queue-two', content: 'Second', editable: true },
        ],
      },
    });
    flushSync();

    const previews = () =>
      document.body.querySelectorAll('.queued-messages__content');
    const editor = () =>
      document.body.querySelector('.queued-messages__editor');

    previews()[0].click();
    await vi.waitFor(() => expect(document.activeElement).toBe(editor()));
    expect(editor().value).toBe('First');

    // An unmodified editor moves to the newly chosen item.
    previews()[0].click();
    await vi.waitFor(() => expect(editor().value).toBe('Second'));
    await vi.waitFor(() => expect(document.activeElement).toBe(editor()));

    editor().value = 'Second, revised';
    editor().dispatchEvent(new InputEvent('input', { bubbles: true }));
    flushSync();

    previews()[0].click();
    flushSync();
    button('Edit queued message').click();
    await vi.waitFor(() => expect(document.activeElement).toBe(editor()));
    expect(
      document.body.querySelectorAll('.queued-messages__editor'),
    ).toHaveLength(1);
    expect(editor().value).toBe('Second, revised');
    expect(
      document.body.querySelector('.queued-messages__notice'),
    ).toBeTruthy();

    button('Cancel').click();
    flushSync();
    previews()[0].click();
    await vi.waitFor(() => expect(editor().value).toBe('First'));
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
