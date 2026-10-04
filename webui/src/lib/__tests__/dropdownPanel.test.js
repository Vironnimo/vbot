// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { computePanelPosition, optionDecorations } from '../dropdownPanel.js';

// OFFSET (4) + EDGE_PADDING (8) is the gap the helper subtracts on each side.
const GAP = 12;

function triggerAt({ top, bottom, left = 100, width = 200 }) {
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

describe('computePanelPosition', () => {
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

  it('opens below when there is room beneath the trigger and flips above near the viewport bottom', () => {
    const below = computePanelPosition(triggerAt({ top: 100, bottom: 130 }));
    expect(below.placement).toBe('bottom');
    expect(below.verticalRule).toContain('top:');

    // availableBelow = 800 - 760 - 12 = 28; availableAbove = 730 - 12 = 718.
    const above = computePanelPosition(triggerAt({ top: 730, bottom: 760 }));
    expect(above.placement).toBe('top');
    expect(above.verticalRule).toContain('bottom:');
  });

  it('caps a panel that stays below and flips earlier for a measured content height', () => {
    // availableBelow = 800 - 568 - 12 = 220: above the 200 flip threshold (so
    // it stays below) but under MAX_HEIGHT, so the cap binds to 220, not 240.
    // A 300px-tall list does not fit there; availableAbove = 538 - 12 = 526.
    const trigger = triggerAt({ top: 538, bottom: 568 });
    const fixed = computePanelPosition(trigger);
    expect(fixed.placement).toBe('bottom');
    expect(fixed.optionsMaxHeight).toBe(window.innerHeight - 568 - GAP);

    expect(
      computePanelPosition(trigger, { contentHeight: 300 }).placement,
    ).toBe('top');
  });

  it('right-aligns a wider floating menu and widens a narrow panel to its minimum within the viewport', () => {
    const menu = computePanelPosition(
      triggerAt({ top: 100, bottom: 124, left: 300, width: 24 }),
      {
        panelWidth: 160,
        horizontalAlign: 'end',
      },
    );
    expect(menu.width).toBe(160);
    expect(menu.left).toBe(164);

    const narrow = triggerAt({ top: 100, bottom: 130, width: 150 });
    const wide = triggerAt({ top: 100, bottom: 130, width: 300 });
    expect(computePanelPosition(narrow, { minWidth: 240 }).width).toBe(240);
    expect(computePanelPosition(wide, { minWidth: 240 }).width).toBe(300);
    window.innerWidth = 200;
    expect(computePanelPosition(narrow, { minWidth: 240 }).width).toBe(184);
  });
});

describe('optionDecorations', () => {
  it('accepts known status dots, count badges, accessible names, tooltips and markers only', () => {
    expect(
      optionDecorations({
        statusDot: 'unread',
        badge: 3,
        ariaLabel: 'Gamma: 3 unread results',
        tooltip: { title: 'Gamma', rows: [] },
        marker: { label: 'Verified' },
      }),
    ).toEqual({
      statusDot: 'unread',
      badge: '3',
      ariaLabel: 'Gamma: 3 unread results',
      tooltip: { title: 'Gamma', rows: [], placement: 'right' },
      marker: {
        label: 'Verified',
        tooltip: { text: 'Verified', placement: 'right' },
      },
    });
    expect(
      optionDecorations({
        statusDot: 'blinking',
        badge: '',
        ariaLabel: 7,
        tooltip: 'Gamma',
        marker: { label: '' },
      }),
    ).toEqual({
      statusDot: '',
      badge: '',
      ariaLabel: '',
      tooltip: { text: 'Gamma', placement: 'right' },
      marker: null,
    });
    expect(optionDecorations(null)).toEqual({
      statusDot: '',
      badge: '',
      ariaLabel: '',
      tooltip: '',
      marker: null,
    });
  });
});
