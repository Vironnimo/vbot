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

  it('renders the dialog semantics, title, and body content', () => {
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
  });

  it('renders an optional footer snippet inside .modal-footer', () => {
    render({ footer: snippet('<span class="my-footer">actions</span>') });

    const footer = document.body.querySelector('.modal-footer');
    expect(footer).toBeTruthy();
    expect(footer.querySelector('.my-footer').textContent).toBe('actions');
  });

  it('omits the footer wrapper when no footer snippet is given', () => {
    render({});
    expect(document.body.querySelector('.modal-footer')).toBeNull();
  });

  it('closes on the × button, Escape, and a backdrop click', () => {
    const onClose = vi.fn();
    render({ onClose });

    document.body.querySelector('.modal-close').click();
    expect(onClose).toHaveBeenCalledTimes(1);

    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    expect(onClose).toHaveBeenCalledTimes(2);

    document.body.querySelector('.modal-overlay').click();
    expect(onClose).toHaveBeenCalledTimes(3);
  });

  it('does not close on a click inside the dialog box', () => {
    const onClose = vi.fn();
    render({ onClose });

    document.body.querySelector('.modal').click();
    expect(onClose).not.toHaveBeenCalled();
  });

  it('blocks every close path while closeDisabled', () => {
    const onClose = vi.fn();
    render({ onClose, closeDisabled: true });

    expect(document.body.querySelector('.modal-close').disabled).toBe(true);

    document.body.querySelector('.modal-close').click();
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    document.body.querySelector('.modal-overlay').click();

    expect(onClose).not.toHaveBeenCalled();
  });
});
