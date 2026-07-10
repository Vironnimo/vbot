// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  TOOLTIP_SHOW_DELAY_MS as SHOW_DELAY_MS,
  positionFloating,
  tooltip,
} from '../tooltip.js';

function tooltipElement() {
  return document.getElementById('app-tooltip');
}

function isVisible() {
  const element = tooltipElement();
  return element !== null && element.classList.contains('app-tooltip--visible');
}

function hover(node) {
  node.dispatchEvent(new Event('pointerenter'));
  vi.advanceTimersByTime(SHOW_DELAY_MS);
}

describe('tooltip action', () => {
  let node;
  let action;

  beforeEach(() => {
    vi.useFakeTimers();
    node = document.createElement('button');
    document.body.appendChild(node);
  });

  afterEach(() => {
    action?.destroy();
    action = null;
    node.remove();
    tooltipElement()?.remove();
    vi.useRealTimers();
  });

  it('shows the text after the hover delay and links it via aria-describedby', () => {
    action = tooltip(node, 'Copy to clipboard');

    node.dispatchEvent(new Event('pointerenter'));
    expect(isVisible()).toBe(false);

    vi.advanceTimersByTime(SHOW_DELAY_MS);
    expect(isVisible()).toBe(true);
    expect(tooltipElement().textContent).toBe('Copy to clipboard');
    expect(node.getAttribute('aria-describedby')).toBe('app-tooltip');
  });

  it('hides on pointer leave and clears the aria link', () => {
    action = tooltip(node, 'Copy to clipboard');
    hover(node);

    node.dispatchEvent(new Event('pointerleave'));
    expect(isVisible()).toBe(false);
    expect(node.hasAttribute('aria-describedby')).toBe(false);
  });

  it('cancels a pending show when the pointer leaves within the delay', () => {
    action = tooltip(node, 'Copy to clipboard');

    node.dispatchEvent(new Event('pointerenter'));
    node.dispatchEvent(new Event('pointerleave'));
    vi.advanceTimersByTime(SHOW_DELAY_MS);

    expect(isVisible()).toBe(false);
  });

  it('hides on Escape and on pointerdown (activating the control)', () => {
    action = tooltip(node, 'Copy to clipboard');

    hover(node);
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    expect(isVisible()).toBe(false);

    hover(node);
    node.dispatchEvent(new Event('pointerdown'));
    expect(isVisible()).toBe(false);
  });

  it('shows on keyboard focus and hides on blur', () => {
    action = tooltip(node, 'Copy to clipboard');

    node.dispatchEvent(new Event('focus'));
    vi.advanceTimersByTime(SHOW_DELAY_MS);
    expect(isVisible()).toBe(true);

    node.dispatchEvent(new Event('blur'));
    expect(isVisible()).toBe(false);
  });

  it('never shows for empty text, and update() can disable a visible tooltip', () => {
    action = tooltip(node, '');
    hover(node);
    expect(isVisible()).toBe(false);

    action.update('Now populated');
    hover(node);
    expect(isVisible()).toBe(true);

    action.update('');
    expect(isVisible()).toBe(false);
  });

  it('update() swaps the text of a visible tooltip in place', () => {
    action = tooltip(node, 'Before');
    hover(node);

    action.update('After');
    expect(tooltipElement().textContent).toBe('After');
    expect(isVisible()).toBe(true);
  });

  it('cleans up on destroy', () => {
    action = tooltip(node, 'Copy to clipboard');
    hover(node);

    action.destroy();
    action = null;
    expect(isVisible()).toBe(false);

    node.dispatchEvent(new Event('pointerenter'));
    vi.advanceTimersByTime(SHOW_DELAY_MS);
    expect(isVisible()).toBe(false);
  });
});

describe('positionFloating', () => {
  const originalHeight = window.innerHeight;
  const originalWidth = window.innerWidth;

  beforeEach(() => {
    window.innerHeight = 800;
    window.innerWidth = 1200;
  });

  afterEach(() => {
    window.innerHeight = originalHeight;
    window.innerWidth = originalWidth;
  });

  function anchorAt({ top, bottom, left = 500, width = 40 }) {
    return {
      getBoundingClientRect: () => ({
        top,
        bottom,
        left,
        width,
        right: left + width,
        height: bottom - top,
        x: left,
        y: top,
      }),
    };
  }

  function floatingOfSize(width, height) {
    const element = document.createElement('div');
    Object.defineProperty(element, 'offsetWidth', { value: width });
    Object.defineProperty(element, 'offsetHeight', { value: height });
    return element;
  }

  it('centers above the anchor when there is room', () => {
    const element = floatingOfSize(100, 24);
    positionFloating(anchorAt({ top: 300, bottom: 320 }), element);

    // centered: 500 + 20 - 50 = 470; above: 300 - 6 - 24 = 270.
    expect(element.style.left).toBe('470px');
    expect(element.style.top).toBe('270px');
  });

  it('falls below the anchor when there is no room above', () => {
    const element = floatingOfSize(100, 24);
    positionFloating(anchorAt({ top: 10, bottom: 30 }), element);

    expect(element.style.top).toBe('36px');
  });

  it('clamps to the viewport edges horizontally', () => {
    const element = floatingOfSize(200, 24);
    positionFloating(
      anchorAt({ top: 300, bottom: 320, left: 0, width: 20 }),
      element,
    );
    expect(element.style.left).toBe('8px');

    positionFloating(
      anchorAt({ top: 300, bottom: 320, left: 1180, width: 20 }),
      element,
    );
    expect(element.style.left).toBe(`${1200 - 200 - 8}px`);
  });
});
