// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../../lib/i18n.js';

vi.mock('svelte', async () => {
  return import('../../../../node_modules/svelte/src/index-client.js');
});

const { default: InfoHint } = await import('../InfoHint.svelte');

function dot() {
  return document.body.querySelector('.info-hint');
}

function popover() {
  return document.body.querySelector('.info-popover');
}

describe('InfoHint', () => {
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

  function mountHint(props = {}) {
    mountedComponent = mount(InfoHint, {
      target: document.body,
      props: { text: 'First paragraph.\n\nSecond paragraph.', ...props },
    });
    flushSync();
  }

  it('renders a closed "?" dot with an accessible default label', () => {
    mountHint();

    expect(dot()).toBeTruthy();
    expect(dot().getAttribute('aria-label')).toBe('More information');
    expect(dot().getAttribute('aria-expanded')).toBe('false');
    expect(popover()).toBeNull();
  });

  it('opens on hover and splits blank-line text into paragraphs', () => {
    mountHint();

    dot().dispatchEvent(new Event('pointerenter'));
    flushSync();

    expect(dot().getAttribute('aria-expanded')).toBe('true');
    const paragraphs = [...popover().querySelectorAll('p')].map(
      (element) => element.textContent,
    );
    expect(paragraphs).toEqual(['First paragraph.', 'Second paragraph.']);
  });

  it('closes after the grace delay when the pointer leaves unpinned', () => {
    vi.useFakeTimers();
    mountHint();

    dot().dispatchEvent(new Event('pointerenter'));
    flushSync();
    dot().dispatchEvent(new Event('pointerleave'));
    vi.advanceTimersByTime(150);
    flushSync();

    expect(popover()).toBeNull();
    vi.useRealTimers();
  });

  it('stays open when pinned by click, and a second click closes it', () => {
    vi.useFakeTimers();
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
    vi.useRealTimers();
  });

  it('closes on Escape and on outside pointerdown', () => {
    mountHint();

    dot().click();
    flushSync();
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    flushSync();
    expect(popover()).toBeNull();

    dot().click();
    flushSync();
    window.dispatchEvent(new Event('pointerdown'));
    flushSync();
    expect(popover()).toBeNull();
  });

  it('renders nothing when the text is empty', () => {
    mountHint({ text: '' });

    dot().dispatchEvent(new Event('pointerenter'));
    flushSync();

    expect(popover()).toBeNull();
  });
});
