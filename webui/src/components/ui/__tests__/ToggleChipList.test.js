// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../../lib/i18n.js';
import { HOVER_CARD_SHOW_DELAY_MS } from '../../../lib/tooltip.js';

vi.mock('svelte', async () => {
  return import('../../../../node_modules/svelte/src/index-client.js');
});

const { default: ToggleChipList } = await import('../ToggleChipList.svelte');

describe('ToggleChipList', () => {
  let mountedComponent;
  let clippedHost;

  beforeEach(() => {
    document.body.innerHTML = '';
    init('en');
    clippedHost = document.createElement('div');
    clippedHost.style.overflow = 'hidden';
    document.body.appendChild(clippedHost);
    mountedComponent = null;
  });

  afterEach(async () => {
    if (mountedComponent) {
      await unmount(mountedComponent);
      mountedComponent = null;
    }
    document.body.innerHTML = '';
  });

  it('portals the skill hover card outside a clipping container', () => {
    vi.useFakeTimers();
    mountedComponent = mount(ToggleChipList, {
      target: clippedHost,
      props: {
        items: [
          {
            name: 'coding-agents',
            allowed: true,
            description: 'Coordinate coding work.',
          },
        ],
      },
    });
    flushSync();

    const anchor = clippedHost.querySelector('.access-chip-wrap');
    const card = document.body.querySelector('.access-chip__tip');

    expect(anchor).toBeTruthy();
    expect(card).toBeTruthy();
    expect(card.parentElement).toBe(document.body);
    expect(card.dataset.floatingOpen).toBe('false');

    anchor.dispatchEvent(new Event('pointerenter'));
    vi.advanceTimersByTime(HOVER_CARD_SHOW_DELAY_MS);

    expect(card.dataset.floatingOpen).toBe('true');
    expect(card.classList.contains('floating-card')).toBe(true);
    expect(card.getAttribute('role')).toBe('tooltip');
    expect(card.textContent).toContain('Coordinate coding work.');
    expect(card.style.left).not.toBe('');
    expect(card.style.top).not.toBe('');
    vi.useRealTimers();
  });

  it('lets keyboard users reach the Open Extensions action inside a readiness card', () => {
    const onOpenExtensions = vi.fn();
    mountedComponent = mount(ToggleChipList, {
      target: clippedHost,
      props: {
        items: [
          {
            name: 'ha_get_state',
            allowed: true,
            description: 'Read a Home Assistant entity.',
            ready: false,
            readiness_hint: 'Connect Home Assistant first.',
            extension: 'homeassistant',
          },
        ],
        onOpenExtensions,
      },
    });
    flushSync();
    const chip = clippedHost.querySelector('button.access-chip');
    const card = document.body.querySelector('.access-chip__tip');

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab' }));
    chip.focus();
    expect(card.dataset.floatingOpen).toBe('true');
    // A card with an action is not a tooltip.
    expect(card.hasAttribute('role')).toBe(false);
    expect(chip.getAttribute('aria-describedby')).toBe(card.id);

    chip.dispatchEvent(
      new KeyboardEvent('keydown', {
        key: 'Tab',
        bubbles: true,
        cancelable: true,
      }),
    );
    const action = card.querySelector('button');
    expect(document.activeElement).toBe(action);
    action.click();
    expect(onOpenExtensions).toHaveBeenCalledWith('homeassistant');

    window.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Escape', cancelable: true }),
    );
    expect(card.dataset.floatingOpen).toBe('false');
    expect(document.activeElement).toBe(chip);
  });

  it('makes a locked chip with details keyboard-focusable', () => {
    mountedComponent = mount(ToggleChipList, {
      target: clippedHost,
      props: {
        items: [
          {
            name: 'memory',
            allowed: true,
            locked: true,
            lockedNote: 'Controlled by the Memory setting.',
          },
        ],
      },
    });
    flushSync();
    const chip = clippedHost.querySelector('.access-chip--locked');
    const card = document.body.querySelector('.access-chip__tip');

    expect(chip.tabIndex).toBe(0);
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab' }));
    chip.focus();
    expect(card.dataset.floatingOpen).toBe('true');
    expect(card.textContent).toContain('Controlled by the Memory setting.');
  });

  it('uses Extension family labels and folds singleton families into Individual Tools', () => {
    mountedComponent = mount(ToggleChipList, {
      target: clippedHost,
      props: {
        grouped: true,
        items: [
          {
            name: 'ha_get_state',
            family: 'extension:homeassistant:home_assistant',
            family_label: 'Home Assistant',
          },
          {
            name: 'ha_call_service',
            family: 'extension:homeassistant:home_assistant',
            family_label: 'Home Assistant',
          },
          {
            name: 'weather_today',
            family: 'extension:weather:forecast',
            family_label: 'Weather Forecast',
          },
        ],
        groupLabel: (family, items) =>
          family ? items[0].family_label : 'Individual Tools',
      },
    });
    flushSync();

    const headings = Array.from(
      document.querySelectorAll('.access-chips__group-title'),
    ).map((heading) => heading.textContent.trim());
    expect(headings).toEqual(['Home Assistant', 'Individual Tools']);

    const search = document.querySelector('.access-chips__search-input');
    search.value = 'get_state';
    search.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();

    const filteredHeadings = Array.from(
      document.querySelectorAll('.access-chips__group-title'),
    ).map((heading) => heading.textContent.trim());
    expect(filteredHeadings).toEqual(['Home Assistant']);
  });
});
