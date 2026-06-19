// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../lib/i18n.js';

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

const { default: Dropdown } = await import('../Dropdown.svelte');

describe('Dropdown', () => {
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
    mountedComponent = mount(Dropdown, {
      target: document.body,
      props: {
        id: 'test-dropdown',
        value: 'medium',
        options: ['low', 'medium', 'high'],
      },
    });
    flushSync();

    const chevron = document.body.querySelector(
      '#test-dropdown .dropdown-chevron, button#test-dropdown .dropdown-chevron',
    );

    expect(chevron).toBeTruthy();
    expect(chevron?.getAttribute('width')).toBe('10');
    expect(chevron?.getAttribute('height')).toBe('10');
    expect(chevron?.getAttribute('viewBox')).toBe('0 0 12 12');
  });

  it('caps the open list to a viewport-aware max-height', async () => {
    mountedComponent = mount(Dropdown, {
      target: document.body,
      props: {
        id: 'capped-dropdown',
        value: 'a',
        options: ['a', 'b', 'c'],
      },
    });
    flushSync();

    document.querySelector('#capped-dropdown').click();

    // open() awaits a tick before positioning the portaled list, so poll until
    // the inline positioning style (incl. the height cap) lands.
    let list = null;
    for (let attempt = 0; attempt < 20; attempt += 1) {
      await Promise.resolve();
      await new Promise((resolve) => setTimeout(resolve, 0));
      flushSync();
      list = document.querySelector('.dropdown-list');
      if ((list?.getAttribute('style') ?? '').includes('max-height')) {
        break;
      }
    }

    expect(list).toBeTruthy();
    // The list height is constrained inline from the computed available space,
    // not left to the static CSS cap, so it can scroll instead of overflowing.
    expect(list.getAttribute('style') ?? '').toContain('max-height');
  });
});
