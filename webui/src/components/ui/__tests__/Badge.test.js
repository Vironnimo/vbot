// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createRawSnippet, flushSync, mount, unmount } from 'svelte';

vi.mock('svelte', async () => {
  return import('../../../../node_modules/svelte/src/index-client.js');
});

const { default: Badge } = await import('../Badge.svelte');

function label(text) {
  return createRawSnippet(() => ({ render: () => `<span>${text}</span>` }));
}

describe('Badge', () => {
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
    mountedComponent = mount(Badge, { target: document.body, props });
    flushSync();
    return document.body.querySelector('.badge');
  }

  it('emits the canonical badge + variant modifier classes', () => {
    for (const variant of ['neutral', 'info', 'success', 'warn', 'error']) {
      const badge = render({ variant });
      expect(badge.classList.contains('badge')).toBe(true);
      expect(badge.classList.contains(`badge--${variant}`)).toBe(true);

      unmount(mountedComponent);
      mountedComponent = null;
      document.body.innerHTML = '';
    }
  });

  it('falls back to neutral for an unknown variant', () => {
    const badge = render({ variant: 'nonsense' });
    expect(badge.classList.contains('badge--neutral')).toBe(true);
  });

  it('renders the label content and appends a passthrough class', () => {
    const badge = render({
      variant: 'info',
      class: 'extra',
      children: label('v1.2.3'),
    });
    expect(badge.classList.contains('badge--info')).toBe(true);
    expect(badge.classList.contains('extra')).toBe(true);
    expect(badge.textContent).toContain('v1.2.3');
  });
});
