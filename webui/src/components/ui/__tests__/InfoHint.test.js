// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../../lib/i18n.js';
import {
  FLOATING_HOVER_CLOSE_DELAY_MS,
  HOVER_CARD_SHOW_DELAY_MS,
  TOOLTIP_SHOW_DELAY_MS,
  tooltip,
} from '../../../lib/tooltip.js';

vi.mock('svelte', async () => {
  return import('../../../../node_modules/svelte/src/index-client.js');
});

const { default: InfoHint } = await import('../InfoHint.svelte');

function dots() {
  return [...document.body.querySelectorAll('.info-hint')];
}

function dot() {
  return dots()[0];
}

function popovers() {
  return [...document.body.querySelectorAll('.info-popover')];
}

function popover() {
  return popovers()[0] ?? null;
}

describe('InfoHint', () => {
  let mountedComponents;

  beforeEach(() => {
    vi.useFakeTimers();
    document.body.innerHTML = '';
    init('en');
    mountedComponents = [];
  });

  afterEach(async () => {
    for (const component of mountedComponents) {
      await unmount(component);
    }
    mountedComponents = [];
    document.body.innerHTML = '';
    vi.useRealTimers();
  });

  function mountHint(props = {}) {
    mountedComponents.push(
      mount(InfoHint, {
        target: document.body,
        props: { text: 'First paragraph.\n\nSecond paragraph.', ...props },
      }),
    );
    flushSync();
  }

  function hover(element) {
    element.dispatchEvent(new Event('pointerenter'));
    vi.advanceTimersByTime(HOVER_CARD_SHOW_DELAY_MS);
    flushSync();
  }

  it('renders a closed "?" dot with an accessible default label', () => {
    mountHint();

    expect(dot()).toBeTruthy();
    expect(dot().getAttribute('aria-label')).toBe('More information');
    expect(dot().getAttribute('aria-expanded')).toBe('false');
    expect(popover()).toBeNull();
  });

  it('previews after the shared hover intent and splits blank-line text into paragraphs', () => {
    mountHint();

    dot().dispatchEvent(new Event('pointerenter'));
    vi.advanceTimersByTime(HOVER_CARD_SHOW_DELAY_MS - 1);
    flushSync();
    expect(popover()).toBeNull();

    vi.advanceTimersByTime(1);
    flushSync();
    expect(dot().getAttribute('aria-expanded')).toBe('true');
    expect(popover().parentElement).toBe(document.body);
    expect(popover().classList.contains('floating-card')).toBe(true);
    const paragraphs = [...popover().querySelectorAll('p')].map(
      (element) => element.textContent,
    );
    expect(paragraphs).toEqual(['First paragraph.', 'Second paragraph.']);
  });

  it('previews at once on keyboard focus and closes after blur', () => {
    mountHint();

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab' }));
    dot().focus();
    flushSync();
    expect(popover()).toBeTruthy();
    expect(dot().getAttribute('aria-describedby')).toBe(popover().id);

    dot().blur();
    vi.advanceTimersByTime(FLOATING_HOVER_CLOSE_DELAY_MS);
    flushSync();
    expect(popover()).toBeNull();
  });

  it('stays open while the pointer travels onto the popover, then closes after the grace', () => {
    mountHint();
    hover(dot());

    dot().dispatchEvent(new Event('pointerleave'));
    popover().dispatchEvent(new Event('pointerenter'));
    vi.advanceTimersByTime(FLOATING_HOVER_CLOSE_DELAY_MS * 2);
    flushSync();
    expect(popover()).toBeTruthy();

    popover().dispatchEvent(new Event('pointerleave'));
    vi.advanceTimersByTime(FLOATING_HOVER_CLOSE_DELAY_MS);
    flushSync();
    expect(popover()).toBeNull();
  });

  it('stays open when pinned by click, and a second click closes it', () => {
    mountHint();

    dot().click();
    flushSync();
    dot().dispatchEvent(new Event('pointerleave'));
    vi.advanceTimersByTime(500);
    flushSync();
    expect(popover()).toBeTruthy();

    dot().click();
    flushSync();
    expect(popover()).toBeNull();
  });

  it('closes on Escape (consumed while pinned) and on outside pointerdown', () => {
    mountHint();

    dot().click();
    flushSync();
    const escape = new KeyboardEvent('keydown', {
      key: 'Escape',
      cancelable: true,
    });
    window.dispatchEvent(escape);
    flushSync();
    expect(popover()).toBeNull();
    expect(escape.defaultPrevented).toBe(true);

    dot().click();
    flushSync();
    window.dispatchEvent(new Event('pointerdown'));
    flushSync();
    expect(popover()).toBeNull();
  });

  it('gives every instance its own popover id', () => {
    mountHint({ text: 'One' });
    mountHint({ text: 'Two' });
    const [first, second] = dots();

    first.click();
    flushSync();
    const firstId = first.getAttribute('aria-describedby');
    first.click();
    second.click();
    flushSync();
    const secondId = second.getAttribute('aria-describedby');

    expect(firstId).toMatch(/^info-hint-popover-\d+$/);
    expect(secondId).toMatch(/^info-hint-popover-\d+$/);
    expect(firstId).not.toBe(secondId);
    expect(document.getElementById(secondId).textContent).toContain('Two');
  });

  it('repositions on scroll while its dot is visible and closes once it is not', () => {
    mountHint();
    let rect = { top: 300, bottom: 316, left: 500, right: 516 };
    dot().getBoundingClientRect = () => ({
      ...rect,
      width: rect.right - rect.left,
      height: rect.bottom - rect.top,
    });
    window.innerHeight = 800;
    window.innerWidth = 1200;

    dot().click();
    flushSync();
    const initialLeft = popover().style.left;
    rect = { top: 400, bottom: 416, left: 600, right: 616 };
    window.dispatchEvent(new Event('scroll'));
    flushSync();
    expect(popover()).toBeTruthy();
    expect(popover().style.left).not.toBe(initialLeft);

    rect = { top: -200, bottom: -184, left: 600, right: 616 };
    window.dispatchEvent(new Event('scroll'));
    flushSync();
    expect(popover()).toBeNull();
  });

  it('keeps a pinned popover while a quick tooltip shows, but yields to another InfoHint', () => {
    mountHint({ text: 'Pinned explanation' });
    mountHint({ text: 'Other explanation' });
    const hinted = document.createElement('button');
    document.body.appendChild(hinted);
    const hintAction = tooltip(hinted, 'Quick hint');
    const [first, second] = dots();

    first.click();
    flushSync();
    hinted.dispatchEvent(new Event('pointerenter'));
    vi.advanceTimersByTime(TOOLTIP_SHOW_DELAY_MS);
    flushSync();
    expect(document.getElementById('app-tooltip').dataset.floatingOpen).toBe(
      'true',
    );
    expect(popovers().map((element) => element.textContent.trim())).toEqual([
      'Pinned explanation',
    ]);

    hover(second);
    expect(popovers().map((element) => element.textContent.trim())).toEqual([
      'Other explanation',
    ]);
    expect(first.getAttribute('aria-expanded')).toBe('false');

    hintAction.destroy();
  });

  it('renders nothing when the text is empty', () => {
    mountHint({ text: '' });

    hover(dot());

    expect(popover()).toBeNull();
  });
});
