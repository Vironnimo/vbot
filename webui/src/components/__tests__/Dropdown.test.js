// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../lib/i18n.js';

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

const { default: Dropdown } = await import('../Dropdown.svelte');
const { TOOLTIP_SHOW_DELAY_MS } = await import('../../lib/tooltip.js');

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

  it('portals the open list with a viewport-aware height cap and closes on outside mousedown or page scroll', async () => {
    const host = document.createElement('div');
    document.body.append(host);
    mountedComponent = mount(Dropdown, {
      target: host,
      props: { id: 'floating-dropdown', value: 'a', options: ['a', 'b'] },
    });
    flushSync();

    const trigger = document.querySelector('#floating-dropdown');
    const root = trigger.closest('.dropdown-primitive');

    // The chevron keeps the design-specified 10 by 10 size.
    const chevron = trigger.querySelector('.dropdown-chevron');
    expect(chevron.getAttribute('width')).toBe('10');
    expect(chevron.getAttribute('height')).toBe('10');
    expect(chevron.getAttribute('viewBox')).toBe('0 0 12 12');
    const list = () => document.querySelector('.dropdown-primitive__list');
    const open = async () => {
      trigger.click();
      await vi.waitFor(() => {
        expect(root.dataset.state).toBe('open');
      });
    };

    // A mousedown while closed leaves the dropdown alone.
    document.body.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
    flushSync();
    expect(root.dataset.state).toBe('closed');

    await open();
    expect(trigger.getAttribute('aria-expanded')).toBe('true');
    // Portaled to <body> so no card or modal ancestor can clip or cover it.
    expect(list().parentElement).toBe(document.body);
    expect(host.contains(list())).toBe(false);
    // The list height is constrained inline from the computed available space,
    // not left to the static CSS cap, so it can scroll instead of overflowing.
    await vi.waitFor(() => {
      expect(list().getAttribute('style') ?? '').toContain('max-height');
    });
    list().dispatchEvent(new Event('scroll'));
    flushSync();
    expect(root.dataset.state).toBe('open');

    window.dispatchEvent(new Event('scroll'));
    flushSync();
    expect(root.dataset.state).toBe('closed');

    await open();
    document.body.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
    flushSync();
    expect(root.dataset.state).toBe('closed');
    expect(list()).toBeNull();
  });

  it('opens and traverses enabled options with standard listbox keys, treating an empty string as a real value', async () => {
    const onValueChange = vi.fn();
    mountedComponent = mount(Dropdown, {
      target: document.body,
      props: {
        id: 'keyboard-dropdown',
        value: '',
        options: [
          { value: '', label: 'All' },
          { value: 'a', label: 'A', disabled: true },
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
      '-option-0',
    );

    listbox.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }),
    );
    flushSync();
    expect(listbox.getAttribute('aria-activedescendant')).toContain(
      '-option-2',
    );
    const options = [...listbox.querySelectorAll('[role="option"]')];
    expect(options[0].classList.contains('active')).toBe(false);
    expect(options[2].classList.contains('active')).toBe(true);

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

  it('shows a clipped selection or option in full on hover', async () => {
    mountedComponent = mount(Dropdown, {
      target: document.body,
      props: {
        id: 'model-dropdown',
        value: 'long',
        options: [
          {
            value: 'long',
            label: 'anthropic/claude-sonnet-4-5-20250929',
            secondaryLabel: 'Work account',
          },
          { value: 'short', label: 'gpt-5' },
        ],
      },
    });
    flushSync();
    const setWidths = (element, clientWidth, scrollWidth) => {
      Object.defineProperty(element, 'clientWidth', { value: clientWidth });
      Object.defineProperty(element, 'scrollWidth', { value: scrollWidth });
    };
    const hoverText = (anchor) => {
      anchor.dispatchEvent(new Event('pointerenter'));
      vi.advanceTimersByTime(TOOLTIP_SHOW_DELAY_MS);
      const bubble = document.getElementById('app-tooltip');
      const text =
        bubble?.dataset.floatingOpen === 'true' ? bubble.textContent : '';
      anchor.dispatchEvent(new Event('pointerleave'));
      vi.runAllTimers();
      return text;
    };

    const trigger = document.querySelector('#model-dropdown');
    setWidths(
      trigger.querySelector('.dropdown-primitive__trigger-label'),
      120,
      260,
    );
    trigger.click();
    await vi.waitFor(() => {
      expect(document.querySelectorAll('[role="option"]')).toHaveLength(2);
    });
    const [long, short] = document.querySelectorAll('[role="option"]');
    setWidths(
      long.querySelector('.dropdown-primitive__option-label'),
      120,
      260,
    );
    setWidths(
      short.querySelector('.dropdown-primitive__option-label'),
      120,
      40,
    );

    vi.useFakeTimers();
    try {
      const full = 'anthropic/claude-sonnet-4-5-20250929\nWork account';
      expect(hoverText(trigger)).toBe(full);
      expect(hoverText(long)).toBe(full);
      expect(hoverText(short)).toBe('');
    } finally {
      vi.useRealTimers();
    }
  });

  it('renders consecutive options of a group under its label and keeps one keyboard order', async () => {
    mountedComponent = mount(Dropdown, {
      target: document.body,
      props: {
        id: 'grouped-dropdown',
        value: 'a',
        options: [
          { value: 'a', label: 'A', group: 'Library' },
          { value: 'b', label: 'B', group: 'Library' },
          { value: 'c', label: 'C', group: 'Agents', secondaryLabel: '3' },
          { value: 'd', label: 'D' },
        ],
      },
    });
    flushSync();

    document.querySelector('#grouped-dropdown').click();
    await vi.waitFor(() => {
      expect(document.activeElement?.getAttribute('role')).toBe('listbox');
    });
    const listbox = document.activeElement;
    const groups = [...listbox.querySelectorAll('[role="group"]')].map(
      (group) => [
        document
          .getElementById(group.getAttribute('aria-labelledby'))
          .textContent.trim(),
        [...group.querySelectorAll('[role="option"]')].map((option) =>
          option.textContent.trim(),
        ),
      ],
    );
    expect(groups).toEqual([
      ['Library', ['A', 'B']],
      ['Agents', ['C 3']],
    ]);
    expect(
      listbox.querySelector(':scope > [role="option"]').textContent.trim(),
    ).toBe('D');

    for (let step = 0; step < 3; step++)
      listbox.dispatchEvent(
        new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }),
      );
    flushSync();
    expect(listbox.getAttribute('aria-activedescendant')).toContain(
      '-option-3',
    );
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
