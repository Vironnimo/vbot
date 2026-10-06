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

  it('portals a fixed panel sized to its trigger and closes on outside mousedown or page scroll', async () => {
    const host = document.createElement('div');
    document.body.append(host);
    mountedComponent = mount(SearchableDropdown, {
      target: host,
      props: {
        id: 'floating-searchable-dropdown',
        value: 'alpha',
        options: ['alpha', 'beta'],
      },
    });
    flushSync();

    const trigger = document.querySelector('#floating-searchable-dropdown');
    // The chevron keeps the design-specified 10 by 10 size.
    const chevron = trigger.querySelector('.dropdown-chevron');
    expect(chevron.getAttribute('width')).toBe('10');
    expect(chevron.getAttribute('height')).toBe('10');
    expect(chevron.getAttribute('viewBox')).toBe('0 0 12 12');
    trigger.getBoundingClientRect = () => ({
      x: 120,
      y: 180,
      left: 120,
      top: 180,
      right: 464,
      bottom: 212,
      width: 344,
      height: 32,
    });
    const root = trigger.closest('.searchable-dropdown');
    const panel = () => document.querySelector('.searchable-dropdown__panel');
    const open = async () => {
      trigger.click();
      await vi.waitFor(() => {
        expect(panel()?.getAttribute('style')).toContain('width: 344px');
      });
    };

    // A mousedown while closed leaves the dropdown alone.
    document.body.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
    flushSync();
    expect(root.dataset.state).toBe('closed');

    await open();
    expect(root.dataset.state).toBe('open');
    expect(trigger.getAttribute('aria-expanded')).toBe('true');
    // Portaled to <body> so no card or modal ancestor can clip or cover it.
    expect(panel().parentElement).toBe(document.body);
    expect(host.contains(panel())).toBe(false);
    expect(panel().dataset.positioning).toBe('fixed');
    expect(panel().dataset.placement).toBe('bottom');

    document.body.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
    flushSync();
    expect(root.dataset.state).toBe('closed');
    expect(panel()).toBeNull();

    await open();
    panel()
      .querySelector('.searchable-dropdown__options')
      .dispatchEvent(new Event('scroll'));
    flushSync();
    expect(root.dataset.state).toBe('open');

    window.dispatchEvent(new Event('scroll'));
    flushSync();
    expect(root.dataset.state).toBe('closed');
  });

  it('uses combobox/listbox semantics and selects with arrow keys', async () => {
    const onValueChange = vi.fn();
    mountedComponent = mount(SearchableDropdown, {
      target: document.body,
      props: {
        id: 'keyboard-searchable-dropdown',
        value: 'alpha',
        options: ['alpha', 'beta', 'gamma'],
        searchPlaceholder: 'Filter models',
        onValueChange,
      },
    });
    flushSync();

    const trigger = document.querySelector('#keyboard-searchable-dropdown');
    trigger.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }),
    );
    await vi.waitFor(() => {
      expect(document.activeElement?.getAttribute('role')).toBe('combobox');
    });

    const input = document.activeElement;
    const listbox = document.querySelector('[role="listbox"]');
    expect(input.getAttribute('aria-controls')).toBe(listbox.id);
    expect(input.getAttribute('aria-label')).toBe('Filter models');
    expect(input.getAttribute('aria-activedescendant')).toContain('-option-0');

    input.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }),
    );
    await vi.waitFor(() => {
      expect(input.getAttribute('aria-activedescendant')).toContain(
        '-option-1',
      );
    });
    input.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }),
    );

    expect(onValueChange).toHaveBeenCalledWith(
      'beta',
      expect.objectContaining({ value: 'beta' }),
    );
    expect(document.activeElement).toBe(trigger);
  });

  it('renders option decorations and opens programmatically', async () => {
    mountedComponent = mount(SearchableDropdown, {
      target: document.body,
      props: {
        id: 'decorated-searchable-dropdown',
        value: 'gamma',
        options: [
          {
            value: 'beta',
            label: 'Beta',
            statusDot: 'running',
            marker: { label: 'Verified' },
          },
          {
            value: 'gamma',
            label: 'Gamma',
            statusDot: 'unread',
            badge: 3,
            ariaLabel: 'Gamma: 3 unread results',
          },
          {
            value: 'openai/gpt-5.2',
            label: 'openai/gpt-5.2',
            labelLead: 'openai/',
          },
        ],
      },
    });
    flushSync();

    const trigger = document.querySelector('#decorated-searchable-dropdown');
    expect(trigger.querySelector('.tab-indicator--unread')).toBeTruthy();

    await mountedComponent.open();
    flushSync();

    expect(document.activeElement?.getAttribute('role')).toBe('combobox');
    const gamma = document.querySelector(
      '[role="option"][aria-label="Gamma: 3 unread results"]',
    );
    expect(gamma?.getAttribute('aria-selected')).toBe('true');
    expect(gamma?.querySelector('.count-badge')?.textContent).toBe('3');
    expect(gamma?.querySelector('.option-marker')).toBeNull();
    // A marker is an icon named for assistive technology, inside the option
    // and only in the list, never on the trigger.
    const beta = document.querySelector('[role="option"]');
    expect(beta?.textContent).toContain('Beta');
    expect(
      beta?.querySelector('[role="img"][aria-label="Verified"]'),
    ).toBeTruthy();
    expect(trigger.querySelector('.option-marker')).toBeNull();
    // A label lead (a Model id's provider path) is part of the label, muted.
    const model = document.querySelector('[role="option"]:last-of-type');
    expect(model?.textContent.trim()).toBe('openai/gpt-5.2');
    expect(
      model?.querySelector('.searchable-dropdown__label-lead')?.textContent,
    ).toBe('openai/');
  });
});
