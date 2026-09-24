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

  it('treats an empty-string option as a real active option', async () => {
    mountedComponent = mount(Dropdown, {
      target: document.body,
      props: {
        id: 'empty-value-dropdown',
        value: '',
        options: [
          { value: '', label: 'All' },
          { value: 'a', label: 'A' },
        ],
      },
    });
    flushSync();

    const trigger = document.querySelector('#empty-value-dropdown');
    trigger.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }),
    );
    await vi.waitFor(() => {
      expect(document.activeElement?.getAttribute('role')).toBe('listbox');
    });
    const listbox = document.activeElement;
    expect(listbox.getAttribute('aria-activedescendant')).toContain(
      '-option-0',
    );

    listbox.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }),
    );
    flushSync();
    const options = [...listbox.querySelectorAll('[role="option"]')];
    expect(options[0].classList.contains('active')).toBe(false);
    expect(options[1].classList.contains('active')).toBe(true);
  });

  it('opens and traverses enabled options with standard listbox keys', async () => {
    const onValueChange = vi.fn();
    mountedComponent = mount(Dropdown, {
      target: document.body,
      props: {
        id: 'keyboard-dropdown',
        value: 'b',
        options: [
          { value: 'a', label: 'A', disabled: true },
          { value: 'b', label: 'B' },
          { value: 'c', label: 'C' },
        ],
        onValueChange,
      },
    });
    flushSync();

    const trigger = document.querySelector('#keyboard-dropdown');
    trigger.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }),
    );
    await vi.waitFor(() => {
      expect(document.activeElement?.getAttribute('role')).toBe('listbox');
    });
    const listbox = document.activeElement;
    expect(listbox.getAttribute('aria-activedescendant')).toContain(
      '-option-1',
    );

    listbox.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }),
    );
    flushSync();
    expect(listbox.getAttribute('aria-activedescendant')).toContain(
      '-option-2',
    );

    listbox.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }),
    );
    flushSync();
    expect(onValueChange).toHaveBeenCalledWith(
      'c',
      expect.objectContaining({ value: 'c' }),
    );
  });

  it('moves the active option with typeahead and cycles on a repeated letter', async () => {
    let now = 1_000;
    const dateNow = vi.spyOn(Date, 'now').mockImplementation(() => now);
    const onValueChange = vi.fn();
    mountedComponent = mount(Dropdown, {
      target: document.body,
      props: {
        id: 'typeahead-dropdown',
        value: 'alpha',
        options: [
          { value: 'alpha', label: 'Alpha' },
          { value: 'beta', label: 'Beta' },
          { value: 'bravo', label: 'Bravo' },
          { value: 'charlie', label: 'Charlie' },
        ],
        onValueChange,
      },
    });
    flushSync();

    document.querySelector('#typeahead-dropdown').click();
    await vi.waitFor(() => {
      expect(document.activeElement?.getAttribute('role')).toBe('listbox');
    });
    const listbox = document.activeElement;
    const activeLabel = () =>
      listbox.querySelector('[role="option"].active')?.textContent.trim();
    const type = (key) => {
      listbox.dispatchEvent(
        new KeyboardEvent('keydown', { key, bubbles: true }),
      );
      flushSync();
    };

    type('b');
    expect(activeLabel()).toBe('Beta');
    now += 100;
    type('b');
    expect(activeLabel()).toBe('Bravo');
    now += 1_000;
    type('c');
    expect(activeLabel()).toBe('Charlie');
    now += 1_000;
    type('b');
    now += 100;
    type('r');
    expect(activeLabel()).toBe('Bravo');

    type('Enter');
    expect(onValueChange).toHaveBeenCalledWith(
      'bravo',
      expect.objectContaining({ value: 'bravo' }),
    );
    dateNow.mockRestore();
  });

  it('continues a search with Space only inside the typeahead window', async () => {
    let now = 1_000;
    const dateNow = vi.spyOn(Date, 'now').mockImplementation(() => now);
    const onValueChange = vi.fn();
    mountedComponent = mount(Dropdown, {
      target: document.body,
      props: {
        id: 'typeahead-space-dropdown',
        value: 'alpha',
        options: [
          { value: 'alpha', label: 'Alpha' },
          { value: 'big deal', label: 'Big deal' },
          { value: 'big', label: 'Bigger' },
        ],
        onValueChange,
      },
    });
    flushSync();

    document.querySelector('#typeahead-space-dropdown').click();
    await vi.waitFor(() => {
      expect(document.activeElement?.getAttribute('role')).toBe('listbox');
    });
    const listbox = document.activeElement;
    const activeLabel = () =>
      listbox.querySelector('[role="option"].active')?.textContent.trim();
    const type = (key) => {
      listbox.dispatchEvent(
        new KeyboardEvent('keydown', { key, bubbles: true }),
      );
      flushSync();
    };

    type('b');
    now += 100;
    type('i');
    now += 100;
    type('g');
    now += 100;
    type(' ');
    expect(activeLabel()).toBe('Big deal');
    expect(onValueChange).not.toHaveBeenCalled();

    now += 1_000;
    type(' ');
    expect(onValueChange).toHaveBeenCalledWith(
      'big deal',
      expect.objectContaining({ value: 'big deal' }),
    );
    dateNow.mockRestore();
  });

  it('renders status dots, count badges and accessible names from option fields', async () => {
    mountedComponent = mount(Dropdown, {
      target: document.body,
      props: {
        id: 'decorated-dropdown',
        value: 'beta',
        options: [
          {
            value: 'beta',
            label: 'Beta',
            statusDot: 'running',
            ariaLabel: 'Beta: Running',
          },
          {
            value: 'gamma',
            label: 'Gamma',
            statusDot: 'unread',
            badge: 2,
            ariaLabel: 'Gamma: 2 unread results',
          },
          { value: 'delta', label: 'Delta', statusDot: 'bogus' },
        ],
      },
    });
    flushSync();

    const trigger = document.querySelector('#decorated-dropdown');
    expect(trigger.querySelector('.tab-indicator--running')).toBeTruthy();
    trigger.click();
    await vi.waitFor(() => {
      expect(document.querySelectorAll('[role="option"]')).toHaveLength(3);
    });
    const [beta, gamma, delta] = document.querySelectorAll('[role="option"]');
    expect(beta.getAttribute('aria-label')).toBe('Beta: Running');
    expect(beta.getAttribute('aria-selected')).toBe('true');
    expect(gamma.getAttribute('aria-label')).toBe('Gamma: 2 unread results');
    expect(gamma.querySelector('.tab-indicator--unread')).toBeTruthy();
    expect(gamma.querySelector('.count-badge')?.textContent).toBe('2');
    expect(delta.hasAttribute('aria-label')).toBe(false);
    expect(delta.querySelector('.tab-indicator')).toBeNull();
    expect(delta.querySelector('.count-badge')).toBeNull();
  });

  it('opens programmatically from a related control', async () => {
    mountedComponent = mount(Dropdown, {
      target: document.body,
      props: {
        id: 'programmatic-dropdown',
        value: 'b',
        options: ['a', 'b'],
      },
    });
    flushSync();

    await mountedComponent.open();
    flushSync();

    expect(
      document
        .querySelector('#programmatic-dropdown')
        .getAttribute('aria-expanded'),
    ).toBe('true');
    expect(document.activeElement?.getAttribute('role')).toBe('listbox');
    expect(
      document.activeElement.getAttribute('aria-activedescendant'),
    ).toContain('-option-1');
  });
});
