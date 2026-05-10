// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../lib/i18n.js';

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

const { default: ToastStack } = await import('../ToastStack.svelte');

describe('ToastStack', () => {
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

  it('renders error toasts and dismisses by toast id', () => {
    const onDismiss = vi.fn();

    mountedComponent = mount(ToastStack, {
      target: document.body,
      props: {
        toasts: [
          {
            id: 'toast-error',
            title: 'Error',
            message: 'Provider failed',
            variant: 'error',
          },
        ],
        onDismiss,
      },
    });
    flushSync();

    const toast = document.querySelector('.toast.error');
    expect(toast).toBeTruthy();
    expect(toast.textContent).toContain('Error');
    expect(toast.textContent).toContain('Provider failed');

    document.querySelector('.toast-close').click();
    flushSync();

    expect(onDismiss).toHaveBeenCalledWith('toast-error');
  });
});
