// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createRawSnippet, flushSync, mount, unmount } from 'svelte';

vi.mock('svelte', async () => {
  return import('../../../../node_modules/svelte/src/index-client.js');
});

const { default: Banner } = await import('../Banner.svelte');

function content(text) {
  return createRawSnippet(() => ({ render: () => `<span>${text}</span>` }));
}

describe('Banner', () => {
  let mountedComponent;

  beforeEach(() => {
    document.body.innerHTML = '';
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
    mountedComponent = mount(Banner, { target: document.body, props });
    flushSync();
    return document.body.querySelector('.banner');
  }

  it('emits the canonical banner + variant classes', () => {
    for (const variant of ['neutral', 'info', 'success', 'warn', 'error']) {
      const banner = render({ variant });
      expect(banner.classList.contains('banner')).toBe(true);
      expect(banner.classList.contains(`banner--${variant}`)).toBe(true);

      unmount(mountedComponent);
      mountedComponent = null;
      document.body.innerHTML = '';
    }
  });

  it('falls back to neutral for an unknown variant', () => {
    const banner = render({ variant: 'nonsense' });
    expect(banner.classList.contains('banner--neutral')).toBe(true);
  });

  it('renders content and forwards classes and accessibility attributes', () => {
    const banner = render({
      variant: 'error',
      class: 'extra',
      role: 'alert',
      children: content('Something went wrong'),
    });
    expect(banner.classList.contains('extra')).toBe(true);
    expect(banner.getAttribute('role')).toBe('alert');
    expect(banner.textContent).toContain('Something went wrong');
  });
});
