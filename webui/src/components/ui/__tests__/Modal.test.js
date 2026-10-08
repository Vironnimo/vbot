// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createRawSnippet, flushSync, mount, unmount } from 'svelte';

import { init } from '../../../lib/i18n.js';

vi.mock('svelte', async () => {
  return import('../../../../node_modules/svelte/src/index-client.js');
});

const { default: Modal } = await import('../Modal.svelte');

function snippet(html) {
  return createRawSnippet(() => ({ render: () => html }));
}

describe('Modal', () => {
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

  function render(props) {
    mountedComponent = mount(Modal, {
      target: document.body,
      props: {
        title: 'Create agent',
        labelledById: 'modal-title',
        body: snippet('<div class="modal-body">body content</div>'),
        ...props,
      },
    });
    flushSync();
  }

  it('renders the dialog semantics, title, and body content without a footer', () => {
    render({});

    const dialog = document.body.querySelector('.modal');
    expect(dialog.getAttribute('role')).toBe('dialog');
    expect(dialog.getAttribute('aria-modal')).toBe('true');
    expect(dialog.getAttribute('aria-labelledby')).toBe('modal-title');

    const title = document.body.querySelector('.modal-title');
    expect(title.id).toBe('modal-title');
    expect(title.textContent).toContain('Create agent');

    expect(document.body.querySelector('.modal-body').textContent).toContain(
      'body content',
    );
    expect(document.body.querySelector('.modal-footer')).toBeNull();
  });

  it('always gives the dialog an accessible name from its visible title', () => {
    render({ labelledById: '' });

    const dialog = document.body.querySelector('.modal');
    const title = document.body.querySelector('.modal-title');
    expect(title.id).not.toBe('');
    expect(dialog.getAttribute('aria-labelledby')).toBe(title.id);
  });

  it('renders an optional footer snippet inside .modal-footer', () => {
    render({ footer: snippet('<span class="my-footer">actions</span>') });

    const footer = document.body.querySelector('.modal-footer');
    expect(footer).toBeTruthy();
    expect(footer.querySelector('.my-footer').textContent).toBe('actions');
  });

  it('closes on the × button, Escape, and a backdrop click, but not on a click inside the dialog box', () => {
    const onClose = vi.fn();
    render({ onClose });

    document.body.querySelector('.modal').click();
    expect(onClose).not.toHaveBeenCalled();

    document.body.querySelector('.modal-close').click();
    expect(onClose).toHaveBeenCalledTimes(1);

    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    expect(onClose).toHaveBeenCalledTimes(2);

    document.body.querySelector('.modal-overlay').click();
    expect(onClose).toHaveBeenCalledTimes(3);
  });

  it('leaves Escape consumed by a nested control to that control', () => {
    const onClose = vi.fn();
    render({ onClose });
    const body = document.body.querySelector('.modal-body');
    body.addEventListener('keydown', (event) => event.preventDefault(), {
      once: true,
    });
    const escape = () =>
      new KeyboardEvent('keydown', {
        key: 'Escape',
        bubbles: true,
        cancelable: true,
      });
    body.dispatchEvent(escape());
    expect(onClose).not.toHaveBeenCalled();
    body.dispatchEvent(escape());
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it.each([
    ['isComposing', { isComposing: true }],
    ['legacy keyCode 229', { keyCode: 229 }],
  ])(
    'lets only the newest of stacked dialogs answer shortcuts outside IME (%s)',
    async (_name, composition) => {
      const outerClose = vi.fn();
      const onInnerClose = vi.fn();
      render({ onClose: outerClose });
      const inner = mount(Modal, {
        target: document.body,
        props: {
          title: 'Choose folder',
          body: snippet(
            '<div class="modal-body"><input class="inner-draft" /><button class="inner-action">Pick</button></div>',
          ),
          onClose: onInnerClose,
        },
      });
      flushSync();
      const innerClose = document.body
        .querySelectorAll('.modal')[1]
        .querySelector('.modal-close');
      innerClose.focus();

      // Tab between the inner dialog's own controls stays the browser's move.
      const tab = new KeyboardEvent('keydown', {
        key: 'Tab',
        bubbles: true,
        cancelable: true,
      });
      innerClose.dispatchEvent(tab);
      expect(tab.defaultPrevented).toBe(false);
      expect(document.activeElement).toBe(innerClose);

      const draft = document.body.querySelector('.inner-draft');
      draft.value = 'unfinished input';
      draft.focus();
      const composingEscape = new KeyboardEvent('keydown', {
        key: 'Escape',
        bubbles: true,
        cancelable: true,
        ...composition,
      });
      const backgroundKeydown = vi.fn();
      window.addEventListener('keydown', backgroundKeydown);
      try {
        draft.dispatchEvent(composingEscape);
      } finally {
        window.removeEventListener('keydown', backgroundKeydown);
      }
      expect(backgroundKeydown).not.toHaveBeenCalled();
      expect(composingEscape.defaultPrevented).toBe(false);
      expect(onInnerClose).not.toHaveBeenCalled();
      expect(outerClose).not.toHaveBeenCalled();
      expect(document.body.querySelectorAll('[role="dialog"]')).toHaveLength(2);
      expect(document.activeElement).toBe(draft);
      expect(draft.value).toBe('unfinished input');

      draft.dispatchEvent(
        new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }),
      );
      expect(onInnerClose).toHaveBeenCalledTimes(1);
      expect(outerClose).not.toHaveBeenCalled();

      await unmount(inner);
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
      expect(outerClose).toHaveBeenCalledTimes(1);
    },
  );

  it('blocks every close path while closeDisabled', () => {
    const onClose = vi.fn();
    render({ onClose, closeDisabled: true });

    expect(document.body.querySelector('.modal-close').disabled).toBe(true);

    document.body.querySelector('.modal-close').click();
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    document.body.querySelector('.modal-overlay').click();

    expect(onClose).not.toHaveBeenCalled();
  });

  it('traps Tab navigation, isolates the background, and restores focus', async () => {
    const opener = document.createElement('button');
    opener.textContent = 'Open';
    document.body.append(opener);
    opener.focus();

    render({
      body: snippet(
        '<div class="modal-body"><button class="body-action">Action</button></div>',
      ),
    });

    const dialog = document.body.querySelector('.modal');
    const closeButton = dialog.querySelector('.modal-close');
    const bodyAction = dialog.querySelector('.body-action');
    expect(document.activeElement).toBe(dialog);
    expect(opener.inert).toBe(true);

    document.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Tab', bubbles: true }),
    );
    expect(document.activeElement).toBe(closeButton);

    bodyAction.focus();
    document.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Tab', bubbles: true }),
    );
    expect(document.activeElement).toBe(closeButton);

    closeButton.focus();
    document.dispatchEvent(
      new KeyboardEvent('keydown', {
        key: 'Tab',
        shiftKey: true,
        bubbles: true,
      }),
    );
    expect(document.activeElement).toBe(bodyAction);

    await unmount(mountedComponent);
    mountedComponent = null;
    expect(opener.inert).toBe(false);
    expect(document.activeElement).toBe(opener);
  });
});
