// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init, t } from '../../lib/i18n.js';
import {
  FLOATING_HOVER_CLOSE_DELAY_MS,
  HOVER_CARD_SHOW_DELAY_MS,
  TOOLTIP_SHOW_DELAY_MS,
} from '../../lib/tooltip.js';

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

const { default: QueuedMessages } = await import('../QueuedMessages.svelte');

function button(key) {
  const label = t(key);
  return Array.from(document.body.querySelectorAll('button')).find(
    (candidate) => candidate.getAttribute('aria-label') === label,
  );
}

function previews() {
  return document.body.querySelectorAll('.queued-messages__content');
}

function editor() {
  return document.body.querySelector('.queued-messages__editor');
}

function typeInEditor(value) {
  editor().value = value;
  editor().dispatchEvent(new InputEvent('input', { bubbles: true }));
  flushSync();
}

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
    vi.useRealTimers();
  });

  function mountQueue(props) {
    mountedComponent = mount(QueuedMessages, {
      target: document.body,
      props,
    });
    flushSync();
  }

  it('opens the complete text card after hover intent, at once on keyboard focus', () => {
    vi.useFakeTimers();
    const content = `First line\n${'Long message '.repeat(100)}THE END`;
    mountQueue({ queuedMessages: [{ id: 'q', content, editable: true }] });
    const anchor = document.querySelector('.queued-messages__preview');
    const preview = anchor.querySelector('button');
    const card = document.querySelector('.queued-messages__full');

    expect(card.parentElement).toBe(document.body);
    expect(card.classList.contains('floating-card')).toBe(true);
    expect(card.textContent).toBe(content);
    // A brief pass over the preview is no hover intent.
    anchor.dispatchEvent(new MouseEvent('pointerenter'));
    anchor.dispatchEvent(new MouseEvent('pointerleave'));
    vi.advanceTimersByTime(HOVER_CARD_SHOW_DELAY_MS);
    expect(card.dataset.floatingOpen).toBe('false');

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
    expect(card.getAttribute('aria-hidden')).toBe('false');
    expect(preview.getAttribute('aria-describedby')).toBe(card.id);
  });

  it('sends Steer once while the request is pending', async () => {
    let resolveSteer;
    const onSteerQueuedMessage = vi.fn(
      () =>
        new Promise((resolve) => {
          resolveSteer = resolve;
        }),
    );
    mountQueue({
      queuedMessages: [
        { id: 'q', content: 'Steer me', editable: true, steerable: true },
      ],
      canSteer: true,
      onSteerQueuedMessage,
    });

    button('queue.steer').click();
    flushSync();
    expect(button('queue.steer').disabled).toBe(true);
    button('queue.steer').click();
    expect(onSteerQueuedMessage).toHaveBeenCalledTimes(1);
    expect(onSteerQueuedMessage).toHaveBeenCalledWith('q');
    resolveSteer(false);
    await vi.waitFor(() => expect(button('queue.steer').disabled).toBe(false));
  });

  it.each([
    ['without an active Run', { canSteer: false }, 'queue.steerUnavailable'],
    [
      'while the message is being delivered',
      { canSteer: true, steering: true },
      'queue.steeringLocked',
    ],
  ])('says why Steer is unavailable %s', async (_label, state, reason) => {
    vi.useFakeTimers();
    mountQueue({
      queuedMessages: [
        {
          id: 'q',
          content: 'Steer me',
          steerable: true,
          steering: state.steering === true,
        },
      ],
      canSteer: state.canSteer,
    });

    const steer = document.querySelector('.queued-messages__actions button');
    expect(steer.disabled).toBe(true);
    steer.parentElement.dispatchEvent(new Event('pointerenter'));
    await vi.advanceTimersByTimeAsync(TOOLTIP_SHOW_DELAY_MS);
    expect(document.getElementById('app-tooltip').textContent).toBe(t(reason));
  });

  it('opens the editor only from editable previews; attachment items only offer removal', () => {
    mountQueue({
      queuedMessages: [
        { id: 'queue-one', content: 'Original', editable: true },
        { id: 'queue-file', content: '[attachment]', editable: false },
      ],
    });

    const [editable, attachment] = document.querySelectorAll('ol > li');
    const labels = (item) =>
      [...item.querySelectorAll('button[aria-label]')].map((control) =>
        control.getAttribute('aria-label'),
      );
    expect(labels(attachment)).toEqual([t('queue.removeMessage')]);
    expect(labels(editable)).toEqual([
      t('queue.editMessage'),
      t('queue.removeMessage'),
    ]);

    previews()[1].click();
    flushSync();
    expect(editor()).toBeNull();

    previews()[0].click();
    flushSync();
    expect(editor().value).toBe('Original');
  });

  it('keeps a failed Queue edit open with its unsaved content', async () => {
    const onEditQueuedMessage = vi.fn().mockResolvedValue(false);
    mountQueue({
      queuedMessages: [
        { id: 'queue-one', content: 'Original', editable: true },
      ],
      onEditQueuedMessage,
    });

    button('queue.editMessage').click();
    flushSync();
    const openEditor = editor();
    typeInEditor('Unsaved change');
    button('queue.saveEdit').click();

    await vi.waitFor(() => {
      expect(onEditQueuedMessage).toHaveBeenCalledWith(
        'queue-one',
        'Unsaved change',
      );
      expect(
        document.body.querySelector('.queued-messages__error'),
      ).not.toBeNull();
    });
    expect(editor()).toBe(openEditor);
    expect(openEditor.value).toBe('Unsaved change');
  });

  it('focuses the editor it opens and keeps a modified draft open', async () => {
    mountQueue({
      queuedMessages: [
        { id: 'queue-one', content: 'First', editable: true },
        { id: 'queue-two', content: 'Second', editable: true },
      ],
    });

    previews()[0].click();
    await vi.waitFor(() => expect(document.activeElement).toBe(editor()));
    expect(editor().value).toBe('First');

    // An unmodified editor moves to the newly chosen item.
    previews()[0].click();
    await vi.waitFor(() => expect(editor().value).toBe('Second'));
    await vi.waitFor(() => expect(document.activeElement).toBe(editor()));

    typeInEditor('Second, revised');
    previews()[0].click();
    flushSync();
    button('queue.editMessage').click();
    await vi.waitFor(() => expect(document.activeElement).toBe(editor()));
    expect(
      document.body.querySelectorAll('.queued-messages__editor'),
    ).toHaveLength(1);
    expect(editor().value).toBe('Second, revised');
    expect(
      document.body.querySelector('.queued-messages__notice'),
    ).not.toBeNull();

    button('queue.cancelEdit').click();
    flushSync();
    previews()[0].click();
    await vi.waitFor(() => expect(editor().value).toBe('First'));
  });
});
