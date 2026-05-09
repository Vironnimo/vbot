// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../lib/i18n.js';

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

const { default: SearchableDropdown } =
  await import('../SearchableDropdown.svelte');

describe('SearchableDropdown', () => {
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

  it('renders the chevron at the design-specified 10 by 10 size', () => {
    mountedComponent = mount(SearchableDropdown, {
      target: document.body,
      props: {
        id: 'test-searchable-dropdown',
        value: 'openai/gpt-5.2',
        options: ['openai/gpt-5.2', 'anthropic/claude-sonnet-4-20250219'],
      },
    });
    flushSync();

    const chevron = document.body.querySelector(
      'button#test-searchable-dropdown .dropdown-chevron',
    );

    expect(chevron).toBeTruthy();
    expect(chevron?.getAttribute('width')).toBe('10');
    expect(chevron?.getAttribute('height')).toBe('10');
    expect(chevron?.getAttribute('viewBox')).toBe('0 0 12 12');
  });
});
