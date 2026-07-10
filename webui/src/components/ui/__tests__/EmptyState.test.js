// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createRawSnippet, flushSync, mount, unmount } from 'svelte';

vi.mock('svelte', async () => {
  return import('../../../../node_modules/svelte/src/index-client.js');
});

const { default: EmptyState } = await import('../EmptyState.svelte');

function snippet(markup) {
  return createRawSnippet(() => ({ render: () => markup }));
}

describe('EmptyState', () => {
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
    mountedComponent = mount(EmptyState, { target: document.body, props });
    flushSync();
    return document.body.querySelector('.empty-state');
  }

  it('renders the title, description, icon, and actions', () => {
    const emptyState = render({
      title: 'No messages yet',
      description: 'Send the first message.',
      icon: snippet('<svg data-testid="icon"></svg>'),
      actions: snippet('<button>Start</button>'),
    });

    expect(emptyState.textContent).toContain('No messages yet');
    expect(emptyState.textContent).toContain('Send the first message.');
    expect(emptyState.querySelector('[data-testid="icon"]')).toBeTruthy();
    expect(emptyState.querySelector('button')?.textContent).toBe('Start');
  });

  it('supports compact and fill geometry', () => {
    const emptyState = render({ density: 'compact', fill: true });

    expect(emptyState.classList.contains('empty-state--compact')).toBe(true);
    expect(emptyState.classList.contains('empty-state--fill')).toBe(true);
  });

  it('falls back to default density and forwards attributes and classes', () => {
    const emptyState = render({
      density: 'unknown',
      class: 'extra',
      role: 'status',
    });

    expect(emptyState.classList.contains('empty-state--default')).toBe(true);
    expect(emptyState.classList.contains('extra')).toBe(true);
    expect(emptyState.getAttribute('role')).toBe('status');
  });
});
