// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createRawSnippet, flushSync, mount, unmount } from 'svelte';

vi.mock('svelte', async () => {
  return import('../../../../node_modules/svelte/src/index-client.js');
});

const { default: StatusChip } = await import('../StatusChip.svelte');

function label(text) {
  return createRawSnippet(() => ({ render: () => `<span>${text}</span>` }));
}

describe('StatusChip', () => {
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
    mountedComponent = mount(StatusChip, { target: document.body, props });
    flushSync();
    return document.body.querySelector('.chip');
  }

  it('emits the canonical chip + variant classes', () => {
    for (const variant of ['success', 'warn', 'info', 'error', 'neutral']) {
      const chip = render({ variant });
      expect(chip.classList.contains('chip')).toBe(true);
      expect(chip.classList.contains(variant)).toBe(true);

      unmount(mountedComponent);
      mountedComponent = null;
      document.body.innerHTML = '';
    }
  });

  it('falls back to neutral for an unknown variant', () => {
    const chip = render({ variant: 'nonsense' });
    expect(chip.classList.contains('neutral')).toBe(true);
  });

  it('renders the label content and appends a passthrough class', () => {
    const chip = render({
      variant: 'info',
      class: 'extra',
      children: label('Placeholder'),
    });
    expect(chip.classList.contains('info')).toBe(true);
    expect(chip.classList.contains('extra')).toBe(true);
    expect(chip.textContent).toContain('Placeholder');
  });
});
