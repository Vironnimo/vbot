// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

vi.mock('svelte', async () => {
  return import('../../../../node_modules/svelte/src/index-client.js');
});

const { default: TabList } = await import('../TabList.svelte');

const ITEMS = [
  { id: 'overview', label: 'Overview' },
  { id: 'usage', label: 'Usage', disabled: true },
  { id: 'runs', label: 'Runs' },
];

describe('TabList', () => {
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

  function render(props = {}) {
    mountedComponent = mount(TabList, {
      target: document.body,
      props: {
        items: ITEMS,
        value: 'overview',
        ariaLabel: 'Statistics',
        idPrefix: 'statistics',
        ...props,
      },
    });
    flushSync();
    return document.querySelector('.tab-list');
  }

  it('links the active tab to its panel with the canonical ARIA contract', () => {
    const tabList = render();
    const tabs = [...tabList.querySelectorAll('[role="tab"]')];

    expect(tabList.getAttribute('role')).toBe('tablist');
    expect(tabList.getAttribute('aria-label')).toBe('Statistics');
    expect(tabs[0].id).toBe('statistics-tab-overview');
    expect(tabs[0].getAttribute('aria-controls')).toBe(
      'statistics-panel-overview',
    );
    expect(tabs[0].getAttribute('aria-selected')).toBe('true');
    expect(tabs[0].tabIndex).toBe(0);
    expect(tabs[1].disabled).toBe(true);
    expect(tabs[2].tabIndex).toBe(-1);
  });

  it('reports pointer selection without duplicating caller-owned state', () => {
    const onChange = vi.fn();
    const tabList = render({ onChange });

    tabList.querySelectorAll('[role="tab"]')[2].click();

    expect(onChange).toHaveBeenCalledWith('runs', ITEMS[2]);
  });

  it('moves and activates with arrows, Home, and End while skipping disabled tabs', () => {
    const onChange = vi.fn();
    const tabList = render({ onChange });
    const tabs = [...tabList.querySelectorAll('[role="tab"]')];

    tabs[0].focus();
    tabs[0].dispatchEvent(
      new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }),
    );
    expect(document.activeElement).toBe(tabs[2]);
    expect(onChange).toHaveBeenLastCalledWith('runs', ITEMS[2]);

    tabs[2].dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Home', bubbles: true }),
    );
    expect(document.activeElement).toBe(tabs[0]);

    tabs[0].dispatchEvent(
      new KeyboardEvent('keydown', { key: 'End', bubbles: true }),
    );
    expect(document.activeElement).toBe(tabs[2]);
  });

  it('supports segmented compact styling and safe fallbacks', () => {
    let tabList = render({
      appearance: 'segmented',
      density: 'compact',
      class: 'extra',
    });

    expect(tabList.classList.contains('tab-list--segmented')).toBe(true);
    expect(tabList.classList.contains('tab-list--compact')).toBe(true);
    expect(tabList.classList.contains('extra')).toBe(true);
  });
});
