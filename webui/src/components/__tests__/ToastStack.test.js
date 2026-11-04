// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../lib/i18n.js';
import { keepClearOfToasts } from '../../lib/toastClearance.js';

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

  it('runs a notice action and closes only a closable notice', () => {
    const onAction = vi.fn();
    const onClose = vi.fn();

    mountedComponent = mount(ToastStack, {
      target: document.body,
      props: {
        notices: [
          {
            id: 'closable',
            message: 'Update ready',
            actions: [{ label: 'Reload', onClick: onAction }],
            onDismiss: onClose,
          },
          { id: 'fixed', message: 'Waiting for an answer' },
        ],
      },
    });
    flushSync();

    const notice = (id) => document.querySelector(`[data-toast-id="${id}"]`);
    notice('closable').querySelector('.toast-actions button').click();
    notice('closable').querySelector('.toast-close').click();
    expect(onAction).toHaveBeenCalledOnce();
    expect(onClose).toHaveBeenCalledOnce();
    expect(notice('fixed').querySelector('.toast-close')).toBeNull();
  });

  describe('with an area to keep clear', () => {
    let measure;

    beforeEach(() => {
      vi.stubGlobal(
        'ResizeObserver',
        class {
          constructor(callback) {
            measure = callback;
          }
          observe() {}
          unobserve() {}
          disconnect() {}
        },
      );
      vi.stubGlobal('innerHeight', 800);
    });

    afterEach(() => vi.unstubAllGlobals());

    // The stack spans x 700-1000 in an 800 tall window.
    function mountStack(areaRect) {
      const area = document.createElement('div');
      area.getBoundingClientRect = () => areaRect;
      document.body.append(area);
      const registration = keepClearOfToasts(area);
      mountedComponent = mount(ToastStack, { target: document.body });
      flushSync();
      const stack = document.querySelector('.toast-stack');
      stack.getBoundingClientRect = () => ({ left: 700, right: 1000 });
      measure();
      flushSync();
      return { stack, registration };
    }

    it.each([
      [
        'rises above an area it overlaps',
        { left: 600, right: 900, top: 650, width: 300, height: 150 },
        '158px',
      ],
      [
        'stays down beside an area',
        { left: 100, right: 700, top: 650, width: 600, height: 150 },
        '',
      ],
      [
        'ignores an area hidden with its view',
        { left: 0, right: 0, top: 0, width: 0, height: 0 },
        '',
      ],
    ])('%s', (_name, areaRect, bottom) => {
      const { stack, registration } = mountStack(areaRect);
      expect(stack.style.bottom).toBe(bottom);
      registration.destroy();
    });

    it('returns once the area is gone', () => {
      const { stack, registration } = mountStack({
        left: 600,
        right: 900,
        top: 650,
        width: 300,
        height: 150,
      });

      registration.destroy();
      flushSync();

      expect(stack.style.bottom).toBe('');
    });
  });
});
