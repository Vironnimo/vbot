// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createRawSnippet, flushSync, mount, unmount } from 'svelte';

import { init } from '../../../lib/i18n.js';

vi.mock('svelte', async () => {
  return import('../../../../node_modules/svelte/src/index-client.js');
});

const { default: ConfirmDialog } = await import('../ConfirmDialog.svelte');

function snippet(html) {
  return createRawSnippet(() => ({ render: () => html }));
}

describe('ConfirmDialog', () => {
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

  function render(props = {}) {
    mountedComponent = mount(ConfirmDialog, {
      target: document.body,
      props: {
        title: 'Delete event',
        body: 'This event repeats.',
        confirmLabel: 'Delete',
        ...props,
      },
    });
    flushSync();
  }

  it('renders the message inside the dialog', () => {
    render({});

    const dialog = document.body.querySelector('.modal');
    expect(dialog.getAttribute('role')).toBe('dialog');
    expect(dialog.textContent).toContain('This event repeats.');
  });

  it('renders extra body content inside the dialog body', () => {
    render({
      bodyExtra: snippet('<div class="extra-choice">choice</div>'),
    });

    const dialog = document.body.querySelector('.modal');
    const body = document.body.querySelector('.modal-body');
    const extra = document.body.querySelector('.modal-body .extra-choice');

    // Regression: the delete dialog's choice radio group used to be rendered as
    // a sibling outside the Modal, so it sat behind the overlay and was neither
    // visible nor reachable. Extra body content must land inside the dialog.
    expect(body).toBeTruthy();
    expect(body.querySelector('p').textContent).toContain(
      'This event repeats.',
    );
    expect(extra).toBeTruthy();
    expect(dialog.contains(extra)).toBe(true);
  });

  it('omits extra content when none is provided', () => {
    render({});

    expect(document.body.querySelector('.extra-choice')).toBeNull();
  });
});
